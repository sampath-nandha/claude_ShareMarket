"""Market-open analytics: gap classification, opening type, initial balance, pre-open reading.

Opening types follow the market-profile convention (Dalton): Open Drive, Open Test Drive, Open Rejection
Reverse, Open Auction. They are computed from completed 1-minute bars of the first N minutes only. Whether a
type has any forward edge on NIFTY is a Phase-4 research question; the labels here make no claim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from mmm.analytics.volatility import atr
from mmm.timeutil import IST, session_open


@dataclass
class Gap:
    kind: str            # GAP_UP_OUTSIDE | GAP_UP_INSIDE | FLAT | GAP_DOWN_INSIDE | GAP_DOWN_OUTSIDE | UNKNOWN
    direction: int       # +1 up, -1 down, 0 flat
    points: float
    pct: float
    atr_multiple: float | None
    prev_close: float
    filled: bool = False          # price traded back to the previous close after the open
    fill_ts: datetime | None = None

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["fill_ts"] = self.fill_ts.isoformat() if self.fill_ts else None
        return d


@dataclass
class OpeningType:
    code: str            # OPEN_DRIVE | OPEN_TEST_DRIVE | OPEN_REJECTION_REVERSE | OPEN_AUCTION | FORMING | UNKNOWN
    direction: int       # drive direction (+1/-1), 0 for auction
    window: int          # minutes
    open: float
    high: float
    low: float
    close: float
    up_excursion: float
    down_excursion: float
    first_side: int      # which extreme came first in time (+1 high first, -1 low first, 0 same bar)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class InitialBalance:
    minutes: int
    high: float
    low: float
    complete: bool
    ext_up: float        # how far price traded above IB high, in IB widths
    ext_down: float
    status: str          # FORMING | INSIDE | EXT_UP | EXT_DOWN | EXT_BOTH

    @property
    def width(self) -> float:
        return self.high - self.low

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["width"] = self.width
        return d


def daily_atr(daily: pd.DataFrame, n: int = 14) -> float | None:
    if daily is None or len(daily) < n + 1:
        return None
    s = atr(daily.reset_index(drop=True), n).dropna()
    return float(s.iloc[-1]) if len(s) else None


def classify_gap(today_open: float, daily: pd.DataFrame, flat_atr: float = 0.1) -> Gap:
    """Gap of today's open versus the previous completed session (last row of `daily`)."""
    if daily is None or daily.empty:
        return Gap("UNKNOWN", 0, 0.0, 0.0, None, float("nan"))
    prev = daily.iloc[-1]
    pc, ph, pl = float(prev["close"]), float(prev["high"]), float(prev["low"])
    pts = today_open - pc
    a = daily_atr(daily)
    mult = pts / a if a else None
    if mult is not None and abs(mult) < flat_atr:
        kind, d = "FLAT", 0
    elif pts > 0:
        kind, d = ("GAP_UP_OUTSIDE" if today_open > ph else "GAP_UP_INSIDE"), 1
    elif pts < 0:
        kind, d = ("GAP_DOWN_OUTSIDE" if today_open < pl else "GAP_DOWN_INSIDE"), -1
    else:
        kind, d = "FLAT", 0
    return Gap(kind, d, pts, pts / pc * 100 if pc else 0.0, mult, pc)


def gap_with_fill(intraday_today: pd.DataFrame, daily: pd.DataFrame) -> Gap | None:
    if intraday_today is None or intraday_today.empty:
        return None
    g = classify_gap(float(intraday_today["open"].iloc[0]), daily)
    if g.direction == 0 or g.kind == "UNKNOWN":
        return g
    hit = intraday_today["low"] <= g.prev_close if g.direction > 0 else intraday_today["high"] >= g.prev_close
    if hit.any():
        g.filled = True
        g.fill_ts = intraday_today.loc[hit.idxmax(), "ts"].to_pydatetime()
    return g


def opening_type(intraday_today: pd.DataFrame, window: int = 15, as_of: datetime | None = None) -> OpeningType:
    """Classify the first `window` minutes. Returns FORMING until the window has completed at `as_of`."""
    if intraday_today is None or intraday_today.empty:
        return OpeningType("UNKNOWN", 0, window, *(float("nan"),) * 4, 0.0, 0.0, 0)
    d0 = intraday_today["ts"].iloc[0].date()
    start = session_open(d0)
    end = start + timedelta(minutes=window)
    w = intraday_today[intraday_today["ts"] < end]
    if w.empty:
        return OpeningType("UNKNOWN", 0, window, *(float("nan"),) * 4, 0.0, 0.0, 0)
    o, h, lo, c = float(w["open"].iloc[0]), float(w["high"].max()), float(w["low"].min()), float(w["close"].iloc[-1])
    up, dn = h - o, o - lo
    rng = h - lo
    done = (as_of is None and len(w) >= window) or (as_of is not None and as_of >= end)
    if not done:
        return OpeningType("FORMING", 0, window, o, h, lo, c, up, dn, 0)
    if rng <= 0:
        return OpeningType("OPEN_AUCTION", 0, window, o, h, lo, c, up, dn, 0, ["no range"])
    # which extreme of the window was printed first in time
    i_hi, i_lo = int(np.argmax(w["high"].to_numpy())), int(np.argmin(w["low"].to_numpy()))
    first = 1 if i_hi < i_lo else (-1 if i_lo < i_hi else 0)
    move = c - o
    d = 1 if move > 0 else (-1 if move < 0 else 0)
    with_ = up if d > 0 else dn
    against = dn if d > 0 else up
    notes = [f"range {rng:.1f}, close {move:+.1f} from open"]
    if d != 0 and against <= 0.15 * rng and abs(move) >= 0.5 * rng:
        code = "OPEN_DRIVE"
    elif d != 0 and first == -d and against >= 0.2 * rng and abs(move) >= 0.4 * rng and with_ >= 0.5 * rng:
        code = "OPEN_TEST_DRIVE"
        notes.append("tested the opposite side first, then drove through the open")
    elif max(up, dn) >= 0.65 * rng and abs(move) < 0.4 * rng:
        code = "OPEN_REJECTION_REVERSE"
        d = -1 if up >= dn else 1          # the rejected side's opposite
        notes.append("one side rejected, price back near the open")
    else:
        code, d = "OPEN_AUCTION", 0
    return OpeningType(code, d, window, o, h, lo, c, up, dn, first, notes)


def initial_balance(intraday_today: pd.DataFrame, minutes: int = 60, as_of: datetime | None = None) -> InitialBalance | None:
    if intraday_today is None or intraday_today.empty:
        return None
    start = session_open(intraday_today["ts"].iloc[0].date())
    end = start + timedelta(minutes=minutes)
    ib = intraday_today[intraday_today["ts"] < end]
    if ib.empty:
        return None
    hi, lo = float(ib["high"].max()), float(ib["low"].min())
    complete = (as_of >= end) if as_of is not None else (intraday_today["ts"].iloc[-1] >= end - timedelta(minutes=1))
    after = intraday_today[intraday_today["ts"] >= end]
    w = max(hi - lo, 1e-9)
    ext_up = max(0.0, float(after["high"].max()) - hi) / w if not after.empty else 0.0
    ext_dn = max(0.0, lo - float(after["low"].min())) / w if not after.empty else 0.0
    if not complete:
        status = "FORMING"
    elif ext_up > 0 and ext_dn > 0:
        status = "EXT_BOTH"
    elif ext_up > 0:
        status = "EXT_UP"
    elif ext_dn > 0:
        status = "EXT_DOWN"
    else:
        status = "INSIDE"
    return InitialBalance(minutes, hi, lo, complete, ext_up, ext_dn, status)


@dataclass
class PreOpen:
    available: bool
    index_price: float | None = None          # last index value printed in the 09:00-09:08 call auction
    gap_points: float | None = None
    buy_qty: int | None = None                 # summed total buy quantity across constituents at the last pre-open tick
    sell_qty: int | None = None
    imbalance: float | None = None             # (buy - sell) / (buy + sell), -1..+1
    constituents_seen: int = 0
    note: str = ""

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def preopen_reading(ticks: pd.DataFrame, index_token: int, constituent_tokens: list[int], prev_close: float | None) -> PreOpen:
    """Read the NSE pre-open call auction (09:00-09:08 IST) from archived collector ticks.

    Uses exchange timestamps when present, else receipt time. Returns available=False with a reason when the
    collector did not capture the window (it starts at 09:00 only when running and logged in).
    """
    if ticks is None or ticks.empty:
        return PreOpen(False, note="no ticks archived for this session")
    ts = ticks["exchange_timestamp"].where(ticks["exchange_timestamp"].notna(), ticks["received_ts"])
    ts = pd.to_datetime(ts)
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(IST)
    d = ts.dt.tz_convert(IST)
    day = d.dt.date.iloc[0]
    lo = datetime(day.year, day.month, day.day, 9, 0, tzinfo=IST)
    hi = datetime(day.year, day.month, day.day, 9, 8, 30, tzinfo=IST)
    pre = ticks[(d >= lo) & (d <= hi)].assign(_t=d[(d >= lo) & (d <= hi)])
    if pre.empty:
        return PreOpen(False, note="no ticks between 09:00 and 09:08 (collector not running or not logged in yet)")
    idx = pre[pre["instrument_token"] == index_token].sort_values("_t")
    price = float(idx["last_price"].dropna().iloc[-1]) if not idx.empty and idx["last_price"].notna().any() else None
    cons = pre[pre["instrument_token"].isin(constituent_tokens)].sort_values("_t").groupby("instrument_token").tail(1)
    buy = int(cons["total_buy_quantity"].fillna(0).sum()) if not cons.empty else None
    sell = int(cons["total_sell_quantity"].fillna(0).sum()) if not cons.empty else None
    imb = (buy - sell) / (buy + sell) if buy is not None and sell is not None and (buy + sell) > 0 else None
    gap = (price - prev_close) if price is not None and prev_close else None
    return PreOpen(True, price, gap, buy, sell, imb, len(cons),
                   "pre-open order quantities are indicative and can change until 09:08")


def load_preopen(day: date, index_token: int, constituent_tokens: list[int], prev_close: float | None) -> PreOpen:
    from mmm.data import parquet_store

    try:
        toks = ",".join(str(int(t)) for t in [index_token, *constituent_tokens])
        df = parquet_store.read_partition("ticks", day, where=f"instrument_token IN ({toks})")
    except Exception as exc:  # noqa: BLE001 - any read failure means 'not available', never a crash
        return PreOpen(False, note=f"ticks unreadable: {exc}")
    return preopen_reading(df, index_token, constituent_tokens, prev_close)
