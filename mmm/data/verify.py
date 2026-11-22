"""Archive completeness check and repair.

For every tracked instrument and interval: which trading sessions are missing entirely, which are partial
(fewer bars than the session ran for), and optional re-fetch of exactly those sessions from Kite.

Two things keep the report honest and the repair short:

* The session calendar comes from the source itself: a day is a session when Kite has a NIFTY 50 daily candle
  for it (see `SessionCalendar`). Unlisted holidays, special weekend sessions and Muhurat trading are therefore
  handled without the hand-kept holiday file, which is only used after the last stored daily candle.
* A day that repair re-fetched successfully but that is still short is recorded as *source-confirmed* in the
  state DB (thin option trading, short sessions). It is reported as OK* and never re-fetched again, unless bars
  are later lost from the archive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd

from mmm.analytics.candles import MINUTES
from mmm.data import parquet_store
from mmm.data.historical import backfill
from mmm.timeutil import is_trading_day, now_ist, session_close, trading_days_between

FULL_SESSION_MINUTES = 375           # 09:15-15:29 inclusive
PARTIAL_TOLERANCE = 0.97             # a session with < 97% of expected bars is flagged
FINAL_AFTER = timedelta(minutes=30)  # a session's candles are treated as final this long after the close


@dataclass
class SessionCalendar:
    """Trading sessions and their length, taken from the NIFTY 50 candles in the archive.

    `days` are the dates with a stored index daily candle (fetched from Kite, so they are the exchange's real
    sessions). Dates after `last_confirmed` fall back to the holiday-file calendar so recently missed days are
    still reported. `index_minutes` is the index's own 1-minute bar count per day; other instruments are expected
    to have at most that many minutes, so a short session never makes them look partial.
    """

    days: frozenset[date] = frozenset()
    last_confirmed: date | None = None
    index_minutes: dict[date, int] = field(default_factory=dict)

    @classmethod
    def from_archive(cls, index_token: int | None) -> SessionCalendar:
        if index_token is None:
            return cls()
        day = parquet_store.read_candles("day", index_token)
        days = frozenset(day["ts"].dt.date) if not day.empty else frozenset()
        mins = parquet_store.read_candles("minute", index_token)
        counts = {d: int(n) for d, n in mins["ts"].dt.date.value_counts().items()} if not mins.empty else {}
        return cls(days, max(days) if days else None, counts)

    def sessions(self, lo: date, hi: date) -> list[date]:
        out = {d for d in self.days if lo <= d <= hi}
        tail = lo if self.last_confirmed is None else max(lo, self.last_confirmed + timedelta(days=1))
        if tail <= hi:
            out.update(trading_days_between(tail, hi))
        return sorted(out)

    def session_minutes(self, d: date) -> int:
        n = self.index_minutes.get(d)
        return min(n, FULL_SESSION_MINUTES) if n else FULL_SESSION_MINUTES


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
    confirmed: int = 0       # short sessions the source has confirmed it cannot fill

    @property
    def ok(self) -> bool:
        return self.rows > 0 and not self.missing and not self.partial


def expected_bars(interval: str, session_minutes: int = FULL_SESSION_MINUTES) -> int:
    m = MINUTES.get(interval)
    if m is None:
        return 1
    return -(-session_minutes // m)  # ceil


def _not_final(d: date, now: datetime) -> bool:
    """True for today's session until its candles are final, and for any future date."""
    return d > now.date() or (d == now.date() and now < session_close(d) + FINAL_AFTER)


def check(token: int, name: str, interval: str, start: date | None = None, end: date | None = None,
          calendar: SessionCalendar | None = None, confirmed: dict[str, int] | None = None,
          reference: bool = False) -> Coverage:
    """Coverage of one instrument/interval.

    `confirmed` maps YYYY-MM-DD -> bars for source-confirmed short days (from StateDB.source_confirmed).
    `reference=True` marks the calendar instrument itself (NIFTY 50): it is held to a full session, since its
    own bar count cannot vouch for itself; a genuinely short session is cleared by the source-confirmed ledger.
    """
    df = parquet_store.read_candles(interval, token)
    if df.empty:
        return Coverage(token, name, interval, None, None, 0, 0, 0)
    cal = calendar or SessionCalendar()
    confirmed = confirmed or {}
    dates = df["ts"].dt.date
    first, last = dates.min(), dates.max()
    lo = max(start, first) if start else first
    hi = min(end, last) if end else last
    now = now_ist()
    have = set(dates.unique())
    expected = [d for d in cal.sessions(lo, hi) if d in have or not _not_final(d, now)]
    counts = dates.value_counts()
    missing: list[date] = []
    partial: list[tuple[date, int, int]] = []
    n_confirmed = 0
    present = 0
    for d in expected:
        n = int(counts.get(d, 0))
        present += n > 0
        need = expected_bars(interval, FULL_SESSION_MINUTES if reference else cal.session_minutes(d))
        short = n == 0 or (need > 1 and n < need * PARTIAL_TOLERANCE and not _not_final(d, now))
        if not short:
            continue
        c = confirmed.get(d.isoformat())
        if c is not None and n >= c:
            n_confirmed += 1
        elif n == 0:
            missing.append(d)
        else:
            partial.append((d, n, need))
    return Coverage(token, name, interval, first, last, len(df), present, len(expected), missing, partial, n_confirmed)


def repair(client, cov: Coverage, oi: bool = True, state=None) -> int:
    """Re-fetch the missing/partial sessions (grouped into contiguous ranges). Returns rows written.

    With `state` (a StateDB), every target day that was fetched without error and is still short afterwards is
    recorded as source-confirmed, so later runs stop asking Kite for bars it does not have. Days in a window that
    failed (network, rate limit, auth) and today's unfinished session are never confirmed.
    """
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
    failed: list[tuple[date, date]] = []
    for a, b in ranges:
        errs: list[tuple[date, date, str]] = []
        total += backfill(client, cov.token, cov.interval, a - timedelta(days=1), b + timedelta(days=1), oi=oi,
                          archive_raw=False, errors=errs)
        failed += [(e[0], e[1]) for e in errs]
    if state is not None:
        after = parquet_store.read_candles(cov.interval, cov.token, start=days[0], end=days[-1] + timedelta(days=1))
        counts = after["ts"].dt.date.value_counts() if not after.empty else pd.Series(dtype="int64")
        now = now_ist()
        need = {d: e for d, _n, e in cov.partial}
        confirm: dict[str, int] = {}
        for d in days:
            if _not_final(d, now) or any(fa <= d <= fb for fa, fb in failed):
                continue
            n = int(counts.get(d, 0))
            if n == 0 or n < need.get(d, 1) * PARTIAL_TOLERANCE:
                confirm[d.isoformat()] = n
        state.confirm_source(cov.token, cov.interval, confirm)
    return total


def summarise(covs: list[Coverage]) -> pd.DataFrame:
    def status(c: Coverage) -> str:
        if c.rows == 0:
            return "EMPTY"
        if not c.ok:
            return "GAPS"
        return "OK*" if c.confirmed else "OK"

    return pd.DataFrame([{
        "instrument": c.name, "interval": c.interval, "first": c.first, "last": c.last, "rows": c.rows,
        "sessions": f"{c.sessions_present}/{c.sessions_expected}", "missing": len(c.missing), "partial": len(c.partial),
        "confirmed": c.confirmed, "status": status(c),
    } for c in covs])


def reference_spot(index_token: int | None, state=None) -> float | None:
    """Spot for choosing the option strikes to check: last archived index close, else the snapshot job's value."""
    if index_token is not None:
        _f, last, n = parquet_store.candle_coverage("minute", index_token)
        if n and last is not None:
            df = parquet_store.read_candles("minute", index_token, start=last - timedelta(minutes=5))
            if not df.empty:
                return float(df["close"].iloc[-1])
    if state is not None:
        v = state.get_kv("spot")
        return float(v) if v else None
    return None


def unlisted_holiday_hint(missing: list[date]) -> list[date]:
    """Dates missing across *every* instrument are probably exchange holidays absent from market_holidays.yaml."""
    return [d for d in missing if is_trading_day(d)]
