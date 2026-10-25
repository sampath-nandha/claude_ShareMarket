"""Consolidation (box) detection: N or more completed bars whose total range is small relative to ATR."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from mmm.analytics.volatility import atr


@dataclass(frozen=True)
class Box:
    start_idx: int
    end_idx: int           # last bar inside the box (inclusive)
    high: float
    low: float
    bars: int
    width_atr: float       # (high-low)/ATR

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2


def detect_box(df: pd.DataFrame, end_idx: int | None = None, min_bars: int = 8, max_bars: int = 40, max_width_atr: float = 1.6,
               atr_period: int = 14) -> Box | None:
    """Longest box ending at `end_idx` (default: the bar before the last) with width <= max_width_atr * ATR.

    The box is built backwards from end_idx so it only uses completed bars; the decision bar (end_idx+1) is the
    candidate breakout bar and is never part of the box.
    """
    end_idx = len(df) - 2 if end_idx is None else end_idx
    if end_idx < min_bars:
        return None
    a_series = atr(df.iloc[: end_idx + 1], atr_period).dropna()
    if a_series.empty:
        return None
    a = float(a_series.iloc[-1])
    if a <= 0:
        return None
    h, l = df["high"].to_numpy(), df["low"].to_numpy()
    best: Box | None = None
    hi, lo = h[end_idx], l[end_idx]
    for n in range(2, max_bars + 1):
        i = end_idx - n + 1
        if i < 0:
            break
        hi, lo = max(hi, h[i]), min(lo, l[i])
        width = (hi - lo) / a
        if width > max_width_atr:
            break
        if n >= min_bars:
            best = Box(i, end_idx, float(hi), float(lo), n, float(width))
    return best
