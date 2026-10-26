"""Common provider contracts: bar frames, tick rows, rate limiting.

History providers return bars as a DataFrame with columns ts (IST), open, high, low, close, volume, oi.
Live feeds deliver *normalised tick rows* (the same flat shape `mmm.data.ticks.normalise_tick` produces for
Kite) keyed by our internal instrument token, so the collector's writer, candle aggregator and quote cache are
vendor-agnostic. Fields a vendor does not supply stay None - never fabricated.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import pandas as pd

from mmm.timeutil import IST

BAR_COLS = ["ts", "open", "high", "low", "close", "volume", "oi"]

# our interval names -> minutes (None = daily)
INTERVAL_MINUTES: dict[str, int | None] = {
    "minute": 1, "2minute": 2, "3minute": 3, "5minute": 5, "10minute": 10, "15minute": 15, "30minute": 30,
    "60minute": 60, "day": None,
}

TickHandler = Callable[[list[dict[str, Any]]], None]
StatusHandler = Callable[[str, dict[str, Any]], None]


class HistoryProvider(Protocol):
    name: str

    def bars(self, symbol: str, interval: str, start: datetime, end: datetime) -> pd.DataFrame: ...


class LiveFeed(Protocol):
    name: str

    def start(self, on_ticks: TickHandler, on_status: StatusHandler | None = None) -> None: ...

    def stop(self) -> None: ...

    @property
    def connected(self) -> bool: ...


class RateLimiter:
    """Sliding-window limiter for several windows at once (per second / minute / hour). Thread-safe."""

    def __init__(self, per_second: float = 0, per_minute: float = 0, per_hour: float = 0):
        self.windows: list[tuple[float, int, deque[float]]] = []
        for secs, n in ((1.0, per_second), (60.0, per_minute), (3600.0, per_hour)):
            if n and n > 0:
                self.windows.append((secs, int(n), deque()))
        self._lock = threading.Lock()

    def wait_time(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        wait = 0.0
        for secs, n, q in self.windows:
            while q and now - q[0] >= secs:
                q.popleft()
            if len(q) >= n:
                wait = max(wait, secs - (now - q[0]))
        return wait

    def acquire(self, sleep: Callable[[float], None] = time.sleep) -> float:
        """Block until a request is allowed; returns the seconds waited."""
        waited = 0.0
        with self._lock:
            while True:
                w = self.wait_time()
                if w <= 0:
                    now = time.monotonic()
                    for _secs, _n, q in self.windows:
                        q.append(now)
                    return waited
                sleep(w)
                waited += w


@dataclass
class ProviderStatus:
    name: str
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def empty_bars() -> pd.DataFrame:
    return pd.DataFrame(columns=BAR_COLS)


def normalise_bars(df: pd.DataFrame, rename: dict[str, str] | None = None) -> pd.DataFrame:
    """Coerce a vendor bar frame to BAR_COLS with IST timestamps (naive vendor stamps are IST)."""
    if df is None or df.empty:
        return empty_bars()
    df = df.rename(columns={c: c.strip().lower() for c in df.columns})
    if rename:
        df = df.rename(columns=rename)
    if "ts" not in df.columns:
        for c in ("timestamp", "time", "date", "td", "datetime"):
            if c in df.columns:
                df = df.rename(columns={c: "ts"})
                break
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    df = df[df["ts"].notna()]
    if df["ts"].dt.tz is None:
        df["ts"] = df["ts"].dt.tz_localize(IST)
    else:
        df["ts"] = df["ts"].dt.tz_convert(IST)
    for c in ("open", "high", "low", "close"):
        if c not in df.columns:
            raise ValueError(f"bar frame missing '{c}' column: {list(df.columns)}")
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    for c in ("volume", "oi"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype("int64") if c in df.columns else 0
    df = df.dropna(subset=["open", "high", "low", "close"])
    return df[BAR_COLS].drop_duplicates("ts", keep="last").sort_values("ts").reset_index(drop=True)


def tick_row(token: int, *, last_price: float | None, ts: datetime | None, received: datetime, mode: str,
             last_qty: float | None = None, atp: float | None = None, volume: float | None = None,
             buy_qty: float | None = None, sell_qty: float | None = None, open_: float | None = None,
             high: float | None = None, low: float | None = None, close: float | None = None, oi: float | None = None,
             bid: float | None = None, bid_qty: float | None = None, ask: float | None = None,
             ask_qty: float | None = None, change: float | None = None) -> dict[str, Any]:
    """Build a normalised tick row in the same shape as `mmm.data.ticks.normalise_tick`."""

    def _i(x: Any) -> int | None:
        return None if x is None or (isinstance(x, float) and math.isnan(x)) else int(x)

    def _f(x: Any) -> float | None:
        return None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)

    return {
        "instrument_token": int(token), "tradable": True, "mode": mode,
        "last_price": _f(last_price), "last_traded_quantity": _i(last_qty), "average_traded_price": _f(atp),
        "volume_traded": _i(volume), "total_buy_quantity": _i(buy_qty), "total_sell_quantity": _i(sell_qty),
        "open": _f(open_), "high": _f(high), "low": _f(low), "close": _f(close), "change": _f(change),
        "last_trade_time": ts, "oi": _i(oi), "oi_day_high": None, "oi_day_low": None,
        "exchange_timestamp": ts, "received_ts": received,
        "bid1": _f(bid), "bid1_qty": _i(bid_qty), "bid1_orders": None, "ask1": _f(ask), "ask1_qty": _i(ask_qty), "ask1_orders": None,
    }
