"""Realised-volatility and range statistics from Zerodha prices (distinct from option-implied volatility)."""

from __future__ import annotations

import numpy as np
import pandas as pd


def true_range(df: pd.DataFrame) -> pd.Series:
    h, l, c = df["high"].to_numpy(float), df["low"].to_numpy(float), df["close"].to_numpy(float)
    prev_c = np.roll(c, 1)
    tr = np.maximum.reduce([h - l, np.abs(h - prev_c), np.abs(l - prev_c)])
    if len(tr):
        tr[0] = h[0] - l[0]
    return pd.Series(tr, index=df.index)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder ATR."""
    return true_range(df).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def realised_vol(df: pd.DataFrame, window: int = 20, periods_per_year: int = 252) -> pd.Series:
    r = np.log(df["close"] / df["close"].shift(1))
    return r.rolling(window).std() * np.sqrt(periods_per_year)


def range_percentile(daily: pd.DataFrame, lookback: int = 250) -> pd.Series:
    """Percentile rank of today's daily range within the trailing lookback."""
    rng = daily["high"] - daily["low"]
    return rng.rolling(lookback, min_periods=20).apply(lambda w: (w[:-1] < w[-1]).mean() * 100, raw=True)


def compression(df: pd.DataFrame, short: int = 5, long: int = 20) -> pd.Series:
    """ATR(short)/ATR(long): < 0.7 = compressing, > 1.3 = expanding."""
    return atr(df, short) / atr(df, long)


def intraday_range_utilisation(session_high: float, session_low: float, avg_daily_range: float) -> float:
    if avg_daily_range <= 0:
        return float("nan")
    return (session_high - session_low) / avg_daily_range


def vol_regime(df: pd.DataFrame, period: int = 14, lookback: int = 100) -> str:
    """HIGH / NORMAL / LOW based on current ATR vs its trailing distribution."""
    a = atr(df, period).dropna()
    if len(a) < max(period * 2, 20):
        return "UNKNOWN"
    hist = a.iloc[-lookback:]
    cur = a.iloc[-1]
    hi, lo = hist.quantile(0.75), hist.quantile(0.25)
    if cur > hi:
        return "HIGH"
    if cur < lo:
        return "LOW"
    return "NORMAL"
