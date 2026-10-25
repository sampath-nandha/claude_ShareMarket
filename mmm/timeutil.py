"""Exchange-session time helpers. Everything market-facing is Asia/Kolkata (IST)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from mmm.settings import get_config, get_holidays

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(tz=IST)


def to_ist(ts: datetime) -> datetime:
    """Attach IST to naive datetimes (Kite returns IST-naive), convert aware ones."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=IST)
    return ts.astimezone(IST)


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def session_open(d: date | None = None) -> datetime:
    d = d or now_ist().date()
    return datetime.combine(d, parse_hhmm(get_config().session.open), tzinfo=IST)


def session_close(d: date | None = None) -> datetime:
    d = d or now_ist().date()
    return datetime.combine(d, parse_hhmm(get_config().session.close), tzinfo=IST)


def is_trading_day(d: date | None = None) -> bool:
    d = d or now_ist().date()
    if d.weekday() >= 5:
        return False
    return d.isoformat() not in get_holidays()


def is_market_open(ts: datetime | None = None) -> bool:
    ts = ts or now_ist()
    if not is_trading_day(ts.date()):
        return False
    return session_open(ts.date()) <= ts <= session_close(ts.date())


def previous_trading_day(d: date | None = None) -> date:
    d = d or now_ist().date()
    cur = d - timedelta(days=1)
    while not is_trading_day(cur):
        cur -= timedelta(days=1)
    return cur


def next_trading_day(d: date | None = None) -> date:
    d = d or now_ist().date()
    cur = d + timedelta(days=1)
    while not is_trading_day(cur):
        cur += timedelta(days=1)
    return cur


def trading_days_between(start: date, end: date) -> list[date]:
    out = []
    cur = start
    while cur <= end:
        if is_trading_day(cur):
            out.append(cur)
        cur += timedelta(days=1)
    return out


def expiry_datetime(expiry: date) -> datetime:
    """Options settle at the closing time on the expiry date (internal convention)."""
    return datetime.combine(expiry, parse_hhmm(get_config().pricing.expiry_time), tzinfo=IST)


def years_to_expiry(expiry: date, at: datetime | None = None) -> float:
    """Calendar-time fraction of a year to expiry (ACT/365). Clamped to a tiny positive floor."""
    at = at or now_ist()
    secs = (expiry_datetime(expiry) - to_ist(at)).total_seconds()
    return max(secs / (365.0 * 24 * 3600), 1e-6)


def minutes_since_open(ts: datetime | None = None) -> float:
    ts = ts or now_ist()
    return (ts - session_open(ts.date())).total_seconds() / 60.0
