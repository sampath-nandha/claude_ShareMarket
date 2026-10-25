"""Archive completeness check and repair.

For every tracked instrument and interval: which trading sessions are missing entirely, which are partial
(fewer bars than a full NSE session), and optional re-fetch of exactly those sessions from Kite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

from mmm.analytics.candles import MINUTES
from mmm.data import parquet_store
from mmm.data.historical import backfill
from mmm.timeutil import is_trading_day, trading_days_between

FULL_SESSION_MINUTES = 375           # 09:15-15:29 inclusive
PARTIAL_TOLERANCE = 0.97             # a session with < 97% of expected bars is flagged


@dataclass
class Coverage:
    token: int
    name: str
    interval: str
    first: date | None
    last: date | None
    rows: int
    sessions_present: int
    sessions_expected: int
    missing: list[date] = field(default_factory=list)
    partial: list[tuple[date, int, int]] = field(default_factory=list)   # (date, bars, expected)

    @property
    def ok(self) -> bool:
        return self.rows > 0 and not self.missing and not self.partial


def expected_bars(interval: str) -> int | None:
    m = MINUTES.get(interval)
    if m is None:
        return 1
    return -(-FULL_SESSION_MINUTES // m)  # ceil


def check(token: int, name: str, interval: str, start: date | None = None, end: date | None = None) -> Coverage:
    df = parquet_store.read_candles(interval, token)
    if df.empty:
        return Coverage(token, name, interval, None, None, 0, 0, 0)
    dates = df["ts"].dt.date
    first, last = dates.min(), dates.max()
    lo = max(start, first) if start else first
    hi = min(end, last) if end else last
    expected = trading_days_between(lo, hi)
    present = set(dates.unique())
    missing = [d for d in expected if d not in present]
    partial: list[tuple[date, int, int]] = []
    exp_bars = expected_bars(interval)
    if exp_bars and exp_bars > 1:
        counts = dates.value_counts()
        for d in expected:
            n = int(counts.get(d, 0))
            if 0 < n < exp_bars * PARTIAL_TOLERANCE:
                partial.append((d, n, exp_bars))
    return Coverage(token, name, interval, first, last, len(df), len(present), len(expected), missing, partial)


def repair(client, cov: Coverage, oi: bool = True) -> int:
    """Re-fetch the missing/partial sessions (grouped into contiguous ranges). Returns rows written."""
    days = sorted(set(cov.missing) | {d for d, _n, _e in cov.partial})
    if not days:
        return 0
    ranges: list[tuple[date, date]] = []
    s = p = days[0]
    for d in days[1:]:
        if (d - p).days <= 4:
            p = d
        else:
            ranges.append((s, p))
            s = p = d
    ranges.append((s, p))
    total = 0
    for a, b in ranges:
        total += backfill(client, cov.token, cov.interval, a - timedelta(days=1), b + timedelta(days=1), oi=oi, archive_raw=False)
    return total


def summarise(covs: list[Coverage]) -> pd.DataFrame:
    return pd.DataFrame([{
        "instrument": c.name, "interval": c.interval, "first": c.first, "last": c.last, "rows": c.rows,
        "sessions": f"{c.sessions_present}/{c.sessions_expected}", "missing": len(c.missing), "partial": len(c.partial),
        "status": "OK" if c.ok else ("EMPTY" if c.rows == 0 else "GAPS"),
    } for c in covs])


def unlisted_holiday_hint(missing: list[date]) -> list[date]:
    """Dates missing across *every* instrument are probably exchange holidays absent from market_holidays.yaml."""
    return [d for d in missing if is_trading_day(d)]
