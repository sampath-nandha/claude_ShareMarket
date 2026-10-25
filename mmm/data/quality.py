"""Data-quality statuses and checks. Every feature inherits the worst status of its inputs."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import IntEnum

import pandas as pd

from mmm.timeutil import is_market_open, now_ist


class DQ(IntEnum):
    """Ordered so that min() over inputs gives the worst status."""

    MISSING = 0
    INVALID = 1
    SUSPECT = 2
    STALE = 3
    PARTIAL = 4
    VALID = 5

    @classmethod
    def parse(cls, s: str) -> "DQ":
        return cls[s.upper()]

    def at_least(self, other: "DQ | str") -> bool:
        other = DQ.parse(other) if isinstance(other, str) else other
        return self >= other


def combine(*statuses: DQ) -> DQ:
    return min(statuses) if statuses else DQ.MISSING


def quote_quality(exchange_ts: datetime | None, received_ts: datetime | None, stale_after_s: int = 15) -> DQ:
    """Freshness-based status for a live quote. Off-market, a quote from the last close counts as VALID."""
    if exchange_ts is None and received_ts is None:
        return DQ.MISSING
    ref = received_ts or exchange_ts
    assert ref is not None
    age = (now_ist() - ref).total_seconds()
    if not is_market_open():
        return DQ.VALID
    if age > stale_after_s * 8:
        return DQ.SUSPECT
    if age > stale_after_s:
        return DQ.STALE
    return DQ.VALID


def candle_quality(df: pd.DataFrame, interval_minutes: int | None, require_volume: bool = False) -> tuple[DQ, list[str]]:
    """Validate an OHLC frame. Returns (status, list of problems)."""
    problems: list[str] = []
    if df is None or df.empty:
        return DQ.MISSING, ["empty"]
    if (df[["open", "high", "low", "close"]] <= 0).any().any():
        problems.append("non-positive price")
    bad_ohlc = (df["high"] < df[["open", "close"]].max(axis=1)) | (df["low"] > df[["open", "close"]].min(axis=1))
    if bad_ohlc.any():
        problems.append(f"impossible OHLC in {int(bad_ohlc.sum())} rows")
    if df["ts"].duplicated().any():
        problems.append("duplicate timestamps")
    if not df["ts"].is_monotonic_increasing:
        problems.append("out-of-order timestamps")
    if require_volume and (df["volume"] < 0).any():
        problems.append("negative volume")
    if interval_minutes and len(df) > 2:
        gaps = df["ts"].diff().dropna()
        intraday = gaps[gaps < timedelta(hours=16)]  # ignore overnight gaps
        n_gaps = int((intraday > timedelta(minutes=interval_minutes * 1.5)).sum())
        if n_gaps:
            problems.append(f"{n_gaps} missing-interval gaps")
    if any("impossible" in p or "non-positive" in p for p in problems):
        return DQ.INVALID, problems
    if any("gap" in p or "duplicate" in p or "out-of-order" in p for p in problems):
        return DQ.PARTIAL, problems
    return DQ.VALID, problems


def chain_coverage_quality(n_strikes_present: int, n_strikes_expected: int) -> DQ:
    if n_strikes_expected == 0 or n_strikes_present == 0:
        return DQ.MISSING
    ratio = n_strikes_present / n_strikes_expected
    if ratio >= 0.9:
        return DQ.VALID
    if ratio >= 0.6:
        return DQ.PARTIAL
    return DQ.SUSPECT
