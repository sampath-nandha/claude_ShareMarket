"""Live WebSocket collector: KiteTicker -> normalised ticks -> SQLite latest quotes + Parquet archive + 1m candles.

Runs as a Windows service (deploy/windows). Reconnects with backoff, keeps a subscription inventory,
spools to disk if Parquet writes fail, and heartbeats to the state DB for the health page.
"""

from __future__ import annotations

import queue
import threading
import time
from datetime import datetime
from typing import Any

import pandas as pd
import structlog

from mmm.broker.auth import TokenStore
from mmm.data import parquet_store
from mmm.data.instruments import Universe, build_universe, load_instruments
from mmm.data.quality import quote_quality
from mmm.data.state_db import StateDB
from mmm.data.ticks import (
    CandleAggregator,
    depth_rows,
    depth_to_frame,
    normalise_tick,
    quote_row_from_tick,
    ticks_to_frame,
)
from mmm.timeutil import now_ist

log = structlog.get_logger(__name__)

MAX_PER_CONNECTION = 3000  # documented KiteTicker limit


class Collector:
    def __init__(self, universe: Universe, state: StateDB | None = None, flush_every_s: float = 5.0):
        self.universe = universe
        self.meta = universe.token_map()
        self.state = state or StateDB()
        self.q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=200_000)
        self.agg = CandleAggregator()
        self.flush_every_s = flush_every_s
        self._stop = threading.Event()
        self._ticker = None
        self._connected = False
        self._n_ticks = 0
        self._last_tick_at: datetime | None = None
        self._tokens = universe.all_tokens()[:MAX_PER_CONNECTION]
        if len(universe.all_tokens()) > MAX_PER_CONNECTION:
            log.warning("universe_truncated_to_connection_limit", requested=len(universe.all_tokens()), kept=MAX_PER_CONNECTION)

    # --- KiteTicker callbacks ------------------------------------------------------------------
    def _on_ticks(self, _ws, ticks: list[dict[str, Any]]) -> None:
        recv = now_ist()
        for t in ticks:
            try:
                self.q.put_nowait({"tick": t, "received": recv})
            except queue.Full:
                self.state.dq_incident(None, "queue_full", "SUSPECT", "tick queue full; dropping tick")
        self._n_ticks += len(ticks)
        self._last_tick_at = recv

    def _on_connect(self, ws, _resp) -> None:
        self._connected = True
        log.info("ws_connected", tokens=len(self._tokens))
        ws.subscribe(self._tokens)
        ws.set_mode(ws.MODE_FULL, self._tokens)
        self.state.log_subscription("subscribe", self._tokens, "FULL")
        self.state.audit("collector", "ws_connected", tokens=len(self._tokens))

    def _on_close(self, _ws, code, reason) -> None:
        self._connected = False
        log.warning("ws_closed", code=code, reason=reason)
        self.state.audit("collector", "ws_closed", code=code, reason=str(reason))

    def _on_error(self, _ws, code, reason) -> None:
        log.error("ws_error", code=code, reason=reason)
        self.state.audit("collector", "ws_error", code=code, reason=str(reason))

    def _on_reconnect(self, _ws, attempts) -> None:
        log.warning("ws_reconnecting", attempts=attempts)

    def _on_noreconnect(self, _ws) -> None:
        log.error("ws_gave_up_reconnecting")
        self.state.heartbeat("collector", "DOWN", reason="noreconnect")

    # --- writer thread -------------------------------------------------------------------------
    def _writer(self) -> None:
        tick_buf: list[dict[str, Any]] = []
        depth_buf: list[dict[str, Any]] = []
        candle_buf: list[dict[str, Any]] = []
        quotes: dict[int, dict[str, Any]] = {}
        last_flush = time.monotonic()
        while not self._stop.is_set() or not self.q.empty():
            try:
                item = self.q.get(timeout=0.5)
            except queue.Empty:
                item = None
            if item is not None:
                t, recv = item["tick"], item["received"]
                row = normalise_tick(t, recv)
                tick_buf.append(row)
                if t.get("depth"):
                    depth_buf.extend(depth_rows(t, recv))
                bar = self.agg.on_tick(row)
                if bar:
                    candle_buf.append(bar)
                q = quote_quality(row["exchange_timestamp"], recv).name
                quotes[row["instrument_token"]] = quote_row_from_tick(row, self.meta.get(row["instrument_token"]), q)
            if time.monotonic() - last_flush >= self.flush_every_s:
                self._flush(tick_buf, depth_buf, candle_buf, quotes)
                tick_buf, depth_buf, candle_buf, quotes = [], [], [], {}
                last_flush = time.monotonic()
        candle_buf.extend(self.agg.flush())
        self._flush(tick_buf, depth_buf, candle_buf, quotes)

    def _flush(self, ticks: list, depth: list, candles: list, quotes: dict[int, dict[str, Any]]) -> None:
        today = now_ist().date()
        try:
            if ticks:
                parquet_store.append_partition("ticks", today, ticks_to_frame(ticks))
            if depth:
                parquet_store.append_partition("depth", today, depth_to_frame(depth))
            if candles:
                df = pd.DataFrame(candles)
                for tok, part in df.groupby("instrument_token"):
                    parquet_store.write_candles(part, "minute", int(tok))
            if quotes:
                self.state.upsert_quotes(list(quotes.values()))
                self._update_chain(quotes)
        except Exception as exc:
            log.error("flush_failed_spooling", error=str(exc))
            self._spool(ticks, depth)
        self.state.heartbeat(
            "collector", "UP" if self._connected else "DISCONNECTED",
            ticks_total=self._n_ticks, last_tick_at=self._last_tick_at, queue=self.q.qsize(),
            subscribed=len(self._tokens),
        )

    def _update_chain(self, quotes: dict[int, dict[str, Any]]) -> None:
        if self.universe.options.empty:
            return
        opt_tokens = set(self.universe.options["instrument_token"].astype(int))
        rows = []
        for tok, qrow in quotes.items():
            if tok not in opt_tokens:
                continue
            m = self.meta[tok]
            rows.append(
                {
                    "tradingsymbol": m["tradingsymbol"], "instrument_token": tok,
                    "expiry": pd.Timestamp(m["expiry"]).date().isoformat(), "strike": float(m["strike"]),
                    "option_type": m["instrument_type"], "last_price": qrow["last_price"], "bid": qrow["bid"],
                    "ask": qrow["ask"], "volume": qrow["volume"], "oi": qrow["oi"], "prev_oi": None,
                    "exchange_ts": qrow["exchange_ts"], "received_ts": qrow["received_ts"], "quality": qrow["quality"],
                }
            )
        self.state.upsert_chain(rows)

    def _spool(self, ticks: list, depth: list) -> None:
        from mmm.data.paths import spool_dir

        stamp = now_ist().strftime("%Y%m%d_%H%M%S_%f")
        if ticks:
            ticks_to_frame(ticks).to_parquet(spool_dir() / f"ticks_{stamp}.parquet", index=False)
        if depth:
            depth_to_frame(depth).to_parquet(spool_dir() / f"depth_{stamp}.parquet", index=False)

    # --- lifecycle -----------------------------------------------------------------------------
    def run(self, api_key: str, access_token: str) -> None:
        from kiteconnect import KiteTicker  # lazy: tests don't need it

        self._ticker = KiteTicker(api_key, access_token, reconnect=True, reconnect_max_tries=50, reconnect_max_delay=60)
        kws = self._ticker
        kws.on_ticks = self._on_ticks
        kws.on_connect = self._on_connect
        kws.on_close = self._on_close
        kws.on_error = self._on_error
        kws.on_reconnect = self._on_reconnect
        kws.on_noreconnect = self._on_noreconnect
        writer = threading.Thread(target=self._writer, name="tick-writer", daemon=True)
        writer.start()
        self.state.heartbeat("collector", "STARTING", subscribed=len(self._tokens))
        try:
            kws.connect(threaded=True)
            while not self._stop.is_set():
                time.sleep(1)
        finally:
            self.stop()
            writer.join(timeout=30)

    def stop(self) -> None:
        self._stop.set()
        if self._ticker is not None:
            try:
                self._ticker.close()
            except Exception:
                pass
        self.state.heartbeat("collector", "STOPPED", ticks_total=self._n_ticks)


def run_collector(spot_hint: float | None = None) -> None:
    """Entry point used by the CLI / Windows service."""
    store = TokenStore()
    token = store.access_token()
    if not token:
        raise RuntimeError("No Kite access token for today; run `mmm auth login` first")
    inst = load_instruments()
    if inst.empty:
        raise RuntimeError("No instrument snapshot; run `mmm data instruments` first")
    uni = build_universe(inst, spot_hint)
    log.info("universe", futures=len(uni.futures), options=len(uni.options), constituents=len(uni.constituents),
             expiries=[str(e) for e in uni.expiries], atm=uni.atm_strike)
    Collector(uni).run(store.api_key(), token)
