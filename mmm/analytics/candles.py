"""Candle utilities: resampling from canonical 1-minute bars, session slicing, completeness guards."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from mmm.timeutil import IST, session_close, session_open

RULES = {
    "minute": "1min", "3minute": "3min", "5minute": "5min", "10minute": "10min", "15minute": "15min",
    "30minute": "30min", "60minute": "60min", "day": "1D",
}
MINUTES = {"minute": 1, "3minute": 3, "5minute": 5, "10minute": 10, "15minute": 15, "30minute": 30, "60minute": 60}


def resample(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    """Resample OHLCV(+OI) candles to a coarser interval anchored at 09:15 (NSE convention)."""
    if df.empty:
        return df.copy()
    rule = RULES[interval]
    x = df.set_index("ts").sort_index()
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "oi": "last"}
    if interval == "day":
        out = x.resample("1D").agg(agg).dropna(subset=["open"])
        out.index = out.index.tz_convert(IST) if out.index.tz is not None else out.index.tz_localize(IST)
    else:
        # origin at 09:15 so 60-minute bars run 09:15-10:15, ... like Kite
        out = x.resample(rule, origin=x.index[0].normalize() + pd.Timedelta(hours=9, minutes=15), label="left", closed="left").agg(agg)
        out = out.dropna(subset=["open"])
    out["volume"] = out["volume"].fillna(0).astype("int64")
    out["oi"] = out["oi"].fillna(0).astype("int64")
    return out.reset_index()


def completed_only(df: pd.DataFrame, interval: str, as_of: datetime) -> pd.DataFrame:
    """Drop the bar that is still forming at `as_of` so partial bars never leak into features."""
    if df.empty or interval == "day":
        if interval == "day" and not df.empty:
            return df[df["ts"].dt.date < as_of.date()] if as_of < session_close(as_of.date()) else df
        return df
    width = pd.Timedelta(minutes=MINUTES[interval])
    return df[df["ts"] + width <= as_of]


def session_slice(df: pd.DataFrame, d: date) -> pd.DataFrame:
    return df[(df["ts"] >= session_open(d)) & (df["ts"] <= session_close(d))]


def sessions(df: pd.DataFrame) -> list[date]:
    return sorted(df["ts"].dt.date.unique().tolist()) if not df.empty else []


def daily_from_intraday(df: pd.DataFrame) -> pd.DataFrame:
    return resample(df, "day")
