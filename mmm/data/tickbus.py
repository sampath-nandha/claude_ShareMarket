"""Local tick bus: the collector publishes every normalised tick on a ZeroMQ PUB socket; the web server subscribes.

Loopback only (127.0.0.1), no broker process. Publishing is best-effort: if pyzmq is missing or the port is
taken, the collector logs once and carries on (the web server then falls back to polling the state DB).
Wire format: one JSON message per batch: {"ticks": [compact tick, ...]} with compact keys
  t token · p last price · q last qty · v cumulative volume · oi · b bid · bq bid qty · a ask · aq ask qty
  o h l c day OHLC (c = previous close) · x exchange time (epoch ms) · r receipt time (epoch ms)
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

import structlog

log = structlog.get_logger(__name__)

ADDRESS = os.environ.get("MMM_TICKBUS", "tcp://127.0.0.1:5557")


def _ms(ts: Any) -> int | None:
    return int(ts.timestamp() * 1000) if isinstance(ts, datetime) else None


def compact(row: dict[str, Any]) -> dict[str, Any]:
    return {"t": row["instrument_token"], "p": row.get("last_price"), "q": row.get("last_traded_quantity"), "v": row.get("volume_traded"),
            "oi": row.get("oi"), "b": row.get("bid1"), "bq": row.get("bid1_qty"), "a": row.get("ask1"), "aq": row.get("ask1_qty"),
            "o": row.get("open"), "h": row.get("high"), "l": row.get("low"), "c": row.get("close"),
            "x": _ms(row.get("exchange_timestamp")), "r": _ms(row.get("received_ts"))}


class TickPublisher:
    """Owned by one thread (ZeroMQ sockets are not thread-safe)."""

    def __init__(self, address: str = ADDRESS):
        self.address = address
        self._sock = None
        self._disabled = False

    def _ensure(self) -> bool:
        if self._sock is not None:
            return True
        if self._disabled:
            return False
        try:
            import zmq

            ctx = zmq.Context.instance()
            s = ctx.socket(zmq.PUB)
            s.setsockopt(zmq.SNDHWM, 10_000)
            s.setsockopt(zmq.LINGER, 0)
            s.bind(self.address)
            self._sock = s
            log.info("tickbus_bound", address=self.address)
            return True
        except Exception as exc:  # noqa: BLE001 - publishing is optional
            self._disabled = True
            log.warning("tickbus_disabled", address=self.address, error=str(exc))
            return False

    def publish(self, rows: list[dict[str, Any]]) -> None:
        if not rows or not self._ensure():
            return
        try:
            import zmq

            self._sock.send(json.dumps({"ticks": [compact(r) for r in rows]}, default=str).encode(), flags=zmq.NOBLOCK)
        except Exception as exc:  # noqa: BLE001 - a full queue or no subscriber is fine
            log.debug("tickbus_send_skipped", error=str(exc))

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close(0)
            self._sock = None
