"""Live push: ticks from the collector (ZeroMQ) to every browser over one WebSocket.

Sources, in order of preference:
  1. ZeroMQ SUB on the collector's tick bus: each tick is forwarded as soon as it arrives (sub-millisecond hop).
  2. State-DB poll every 500 ms (fallback when the collector runs an older build or the bus is unavailable).
  3. Demo ticks (only when the archive is empty, labelled SYNTHETIC in every message).
Besides ticks the hub sends {"type": "bar"} at every minute boundary (pages refetch analytics then) and a
status message every 5 s. Messages carry the server send time so the browser can show tick-to-screen latency.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from typing import Any

import structlog
from fastapi import WebSocket, WebSocketDisconnect

log = structlog.get_logger(__name__)


def _ms() -> int:
    return int(time.time() * 1000)


class TickHub:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.last: dict[int, dict[str, Any]] = {}
        self.last_zmq = 0.0
        self.counts = {"zmq": 0, "db": 0, "demo": 0}
        self._tasks: list[asyncio.Task] = []
        self._seen_db: dict[int, str] = {}

    # --- lifecycle -------------------------------------------------------------------------------------
    def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._tasks = [loop.create_task(c()) for c in (self._zmq_loop, self._poll_loop, self._demo_loop, self._clock_loop)]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    # --- fan-out -----------------------------------------------------------------------------------------
    async def broadcast(self, msg: dict[str, Any]) -> None:
        if not self.clients:
            return
        msg["srv"] = _ms()
        data = json.dumps(msg, default=str)
        dead = []
        for ws in list(self.clients):
            try:
                await asyncio.wait_for(ws.send_text(data), timeout=2)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def push_ticks(self, ticks: list[dict[str, Any]], src: str) -> None:
        for t in ticks:
            self.last[int(t["t"])] = t
        self.counts[src] = self.counts.get(src, 0) + len(ticks)
        await self.broadcast({"type": "ticks", "src": src, "d": ticks})

    async def handle(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)
        try:
            await ws.send_text(json.dumps({"type": "hello", "srv": _ms(), "snapshot": list(self.last.values())}, default=str))
            while True:
                msg = await ws.receive_text()
                if msg == "ping":
                    await ws.send_text(json.dumps({"type": "pong", "srv": _ms()}))
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # a broken client must not affect the others  # noqa: BLE001
            log.debug("ws_client_error", error=str(exc))
        finally:
            self.clients.discard(ws)

    # --- sources -------------------------------------------------------------------------------------------
    async def _zmq_loop(self) -> None:
        try:
            import zmq
            import zmq.asyncio

            from mmm.data.tickbus import ADDRESS
        except Exception as exc:  # noqa: BLE001
            log.warning("web_zmq_unavailable", error=str(exc))
            return
        ctx = zmq.asyncio.Context.instance()
        sock = ctx.socket(zmq.SUB)
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.SUBSCRIBE, b"")
        sock.connect(ADDRESS)
        try:
            while True:
                raw = await sock.recv()
                try:
                    ticks = json.loads(raw).get("ticks", [])
                except ValueError:
                    continue
                if ticks:
                    self.last_zmq = time.monotonic()
                    await self.push_ticks(ticks, "zmq")
        finally:
            sock.close(0)

    async def _poll_loop(self) -> None:
        from mmm.web.data import bundle, state_db

        while True:
            await asyncio.sleep(0.5)
            if time.monotonic() - self.last_zmq < 5 or not self.clients:
                continue
            try:
                b = await asyncio.to_thread(bundle)
                if b.is_demo:
                    continue
                rows = await asyncio.to_thread(state_db().quotes)
            except Exception as exc:  # noqa: BLE001
                log.debug("web_poll_failed", error=str(exc))
                continue
            ticks = []
            for r in rows:
                tok, stamp = int(r["instrument_token"]), str(r.get("received_ts"))
                if self._seen_db.get(tok) == stamp:
                    continue
                self._seen_db[tok] = stamp
                ticks.append({"t": tok, "p": r.get("last_price"), "v": r.get("volume"), "oi": r.get("oi"), "b": r.get("bid"), "a": r.get("ask"),
                              "bq": r.get("bid_qty"), "aq": r.get("ask_qty"), "o": r.get("open"), "h": r.get("high"), "l": r.get("low"),
                              "c": r.get("close"), "x": _iso_ms(r.get("exchange_ts")), "r": _iso_ms(r.get("received_ts"))})
            if ticks:
                await self.push_ticks(ticks, "db")

    async def _demo_loop(self) -> None:
        from mmm.web.data import bundle

        px: dict[int, float] = {}
        while True:
            await asyncio.sleep(0.25)
            if not self.clients:
                continue
            try:
                b = await asyncio.to_thread(bundle)
            except Exception as exc:  # noqa: BLE001
                log.debug("web_demo_bundle_failed", error=str(exc))
                continue
            if not b.is_demo:
                continue
            toks = [b.index_token, 1] + list(b.tokens.get("options", {}))[:40]
            ticks = []
            now = _ms()
            idx = px.get(b.index_token, b.spot) * (1 + random.gauss(0, 0.00015))
            px[b.index_token] = idx
            for tok in toks:
                if tok is None:
                    continue
                if tok == b.index_token:
                    p = idx
                elif tok == 1:                       # demo futures track the index with a small, noisy basis
                    p = idx + 45 + random.gauss(0, 1.5)
                else:
                    base = float(b.chain.loc[b.chain["instrument_token"] == tok, "last_price"].iloc[0]) \
                        if "instrument_token" in b.chain and (b.chain["instrument_token"] == tok).any() else 100.0
                    p = max(0.05, px.get(tok, base) * (1 + random.gauss(0, 0.002)))
                px[tok] = p
                ticks.append({"t": int(tok), "p": round(p, 2), "x": now, "r": now, "demo": True})
            await self.push_ticks(ticks, "demo")

    async def _clock_loop(self) -> None:
        last_min = None
        k = 0
        while True:
            await asyncio.sleep(1)
            k += 1
            m = int(time.time() // 60)
            if last_min is not None and m != last_min:
                await asyncio.sleep(2)          # let the collector flush the closed minute
                await self.broadcast({"type": "bar", "minute": m})
            last_min = m
            if k % 5 == 0:
                await self.broadcast({"type": "status", "clients": len(self.clients), "counts": self.counts,
                                      "zmq_live": time.monotonic() - self.last_zmq < 5})


def _iso_ms(s: Any) -> int | None:
    if not s:
        return None
    try:
        import pandas as pd

        return int(pd.Timestamp(s).timestamp() * 1000)
    except (ValueError, TypeError):
        return None
