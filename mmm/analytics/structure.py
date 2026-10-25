"""Swing structure and market-structure classification.

Two swing detectors (both confirmation-aware):
  1. pivot_swings:  a bar is a swing high if its high is the max of the window [i-left, i+right].
                    It is only *confirmed* `right` bars later; `confirmed_at` records that index.
  2. zigzag_swings: ATR-scaled directional change (reversal >= k * ATR flips the leg).
                    Confirmed when the reversal threshold is crossed.

All downstream logic must use `confirmed_at <= current index` to avoid look-ahead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from mmm.analytics.volatility import atr

SwingKind = Literal["H", "L"]
Label = Literal["HH", "HL", "LH", "LL", "EQH", "EQL"]


@dataclass(frozen=True)
class Swing:
    idx: int                 # bar index of the pivot
    ts: pd.Timestamp
    price: float
    kind: SwingKind
    confirmed_at: int        # bar index at which the pivot became known
    label: str | None = None  # HH/HL/LH/LL/EQH/EQL relative to previous same-kind swing


def pivot_swings(df: pd.DataFrame, left: int = 3, right: int = 3) -> list[Swing]:
    """Vectorised: bar i is a swing high if high[i] is strictly greater than every high in [i-left, i+right]."""
    n = len(df)
    if n < left + right + 1:
        return []
    h, l = df["high"], df["low"]
    left_max = h.shift(1).rolling(left).max()
    right_max = h[::-1].shift(1).rolling(right).max()[::-1]
    left_min = l.shift(1).rolling(left).min()
    right_min = l[::-1].shift(1).rolling(right).min()[::-1]
    is_high = (h > left_max) & (h > right_max)
    is_low = (l < left_min) & (l < right_min)
    ts = df["ts"]
    out: list[Swing] = []
    hv, lv = h.to_numpy(), l.to_numpy()
    for i in np.flatnonzero(is_high.to_numpy()):
        if left <= i < n - right:
            out.append(Swing(int(i), ts.iloc[i], float(hv[i]), "H", int(i) + right))
    for i in np.flatnonzero(is_low.to_numpy()):
        if left <= i < n - right:
            out.append(Swing(int(i), ts.iloc[i], float(lv[i]), "L", int(i) + right))
    out.sort(key=lambda s: (s.idx, s.kind))
    return _alternate(out)


def zigzag_swings(df: pd.DataFrame, atr_mult: float = 2.0, atr_period: int = 14) -> list[Swing]:
    """Volatility-adjusted directional change. Flips when price retraces >= atr_mult * ATR from the extreme."""
    if len(df) < atr_period + 2:
        return []
    a = atr(df, atr_period).bfill().to_numpy()
    h, l = df["high"].to_numpy(), df["low"].to_numpy()
    out: list[Swing] = []
    direction = 0  # +1 looking for a high (uptrend leg), -1 looking for a low
    ext_i = 0
    for i in range(1, len(df)):
        thr = atr_mult * a[i]
        if direction >= 0:
            if h[i] >= h[ext_i]:
                ext_i = i
            elif h[ext_i] - l[i] >= thr:
                out.append(Swing(ext_i, df["ts"].iloc[ext_i], float(h[ext_i]), "H", i))
                direction, ext_i = -1, i
        if direction <= 0:
            if l[i] <= l[ext_i]:
                ext_i = i
            elif h[i] - l[ext_i] >= thr:
                out.append(Swing(ext_i, df["ts"].iloc[ext_i], float(l[ext_i]), "L", i))
                direction, ext_i = 1, i
    return _alternate(out)


def _alternate(swings: list[Swing]) -> list[Swing]:
    """Enforce H/L alternation (keep the more extreme of consecutive same-kind pivots) and label HH/HL/LH/LL."""
    cleaned: list[Swing] = []
    for s in swings:
        if cleaned and cleaned[-1].kind == s.kind:
            prev = cleaned[-1]
            better = (s.price > prev.price) if s.kind == "H" else (s.price < prev.price)
            if better:
                cleaned[-1] = s
            continue
        cleaned.append(s)
    labelled: list[Swing] = []
    last_by_kind: dict[str, Swing] = {}
    for s in cleaned:
        prev = last_by_kind.get(s.kind)
        label = None
        if prev is not None:
            tol = max(abs(prev.price) * 0.0005, 1e-9)  # 5 bps "equal" tolerance
            if abs(s.price - prev.price) <= tol:
                label = "EQH" if s.kind == "H" else "EQL"
            elif s.kind == "H":
                label = "HH" if s.price > prev.price else "LH"
            else:
                label = "HL" if s.price > prev.price else "LL"
        s2 = Swing(s.idx, s.ts, s.price, s.kind, s.confirmed_at, label)
        labelled.append(s2)
        last_by_kind[s.kind] = s2
    return labelled


def swings_known_at(swings: list[Swing], i: int) -> list[Swing]:
    return [s for s in swings if s.confirmed_at <= i]


@dataclass
class Structure:
    trend: str                 # STRONG_BULL / WEAK_BULL / STRONG_BEAR / WEAK_BEAR / RANGE / TRANSITION / UNCERTAIN
    last_event: str | None     # BOS_UP / BOS_DOWN / CHOCH_UP / CHOCH_DOWN / FAILED_BREAK_UP / FAILED_BREAK_DOWN
    last_high: Swing | None
    last_low: Swing | None
    labels: list[str]


def classify_structure(df: pd.DataFrame, swings: list[Swing], i: int | None = None, atr_period: int = 14, atr_value: float | None = None) -> Structure:
    """Deterministic classification from the last confirmed swings and the current close.

    BOS_UP:    close > last confirmed swing high while structure was bullish (continuation).
    CHOCH_UP:  close > last confirmed swing high while structure was bearish (change of character).
    FAILED_BREAK_UP: close exceeded the swing high within the last 3 bars but is now back below it.
    Trend strength uses the last 4 same-kind labels: >=3 of 4 directional = STRONG, 2 = WEAK.
    """
    i = len(df) - 1 if i is None else i
    known = swings_known_at(swings, i)
    highs = [s for s in known if s.kind == "H"]
    lows = [s for s in known if s.kind == "L"]
    if len(highs) < 2 or len(lows) < 2:
        return Structure("UNCERTAIN", None, highs[-1] if highs else None, lows[-1] if lows else None, [])

    labels = [s.label for s in known[-6:] if s.label]
    recent = labels[-4:]
    bull = sum(x in ("HH", "HL") for x in recent)
    bear = sum(x in ("LH", "LL") for x in recent)
    eq = sum(x in ("EQH", "EQL") for x in recent)
    if bull >= 3:
        trend = "STRONG_BULL"
    elif bear >= 3:
        trend = "STRONG_BEAR"
    elif eq >= 2 or (bull == bear):
        trend = "RANGE" if eq >= 1 or len(recent) >= 4 else "TRANSITION"
    elif bull > bear:
        trend = "WEAK_BULL"
    else:
        trend = "WEAK_BEAR"

    close = float(df["close"].iloc[i])
    lh, ll = highs[-1], lows[-1]
    if atr_value is not None:
        a = atr_value
    else:
        a = float(atr(df.iloc[: i + 1], atr_period).iloc[-1]) if i >= atr_period else float((df["high"] - df["low"]).iloc[: i + 1].mean())
    buffer = 0.1 * a  # acceptance buffer: a break must clear the level by 10% of ATR
    event = None
    prev_bull = trend in ("STRONG_BULL", "WEAK_BULL")
    prev_bear = trend in ("STRONG_BEAR", "WEAK_BEAR")
    if close > lh.price + buffer:
        event = "BOS_UP" if prev_bull else "CHOCH_UP"
    elif close < ll.price - buffer:
        event = "BOS_DOWN" if prev_bear else "CHOCH_DOWN"
    else:
        recent_bars = df.iloc[max(i - 3, 0) : i + 1]
        if (recent_bars["high"] > lh.price + buffer).any() and close < lh.price:
            event = "FAILED_BREAK_UP"
        elif (recent_bars["low"] < ll.price - buffer).any() and close > ll.price:
            event = "FAILED_BREAK_DOWN"
    return Structure(trend, event, lh, ll, labels)


def trend_direction(trend: str) -> int:
    if "BULL" in trend:
        return 1
    if "BEAR" in trend:
        return -1
    return 0


def swings_frame(swings: list[Swing]) -> pd.DataFrame:
    return pd.DataFrame(
        [{"idx": s.idx, "ts": s.ts, "price": s.price, "kind": s.kind, "label": s.label, "confirmed_at": s.confirmed_at} for s in swings]
    )


def candle_anatomy(df: pd.DataFrame) -> pd.DataFrame:
    """Body/wick ratios and simple single/multi-bar patterns. Context-free: never a trade trigger on its own."""
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    rng = (h - l).replace(0, np.nan)
    body = (c - o).abs()
    out = pd.DataFrame(index=df.index)
    out["range"] = h - l
    out["body"] = body
    out["body_ratio"] = body / rng
    out["upper_wick"] = (h - pd.concat([o, c], axis=1).max(axis=1)) / rng
    out["lower_wick"] = (pd.concat([o, c], axis=1).min(axis=1) - l) / rng
    out["close_loc"] = (c - l) / rng  # 1 = closed at high
    out["bull"] = c > o
    out["inside"] = (h <= h.shift(1)) & (l >= l.shift(1))
    out["outside"] = (h > h.shift(1)) & (l < l.shift(1))
    out["bull_engulf"] = (c > o) & (c.shift(1) < o.shift(1)) & (c >= o.shift(1)) & (o <= c.shift(1))
    out["bear_engulf"] = (c < o) & (c.shift(1) > o.shift(1)) & (c <= o.shift(1)) & (o >= c.shift(1))
    out["pin_bull"] = (out["lower_wick"] >= 0.6) & (out["body_ratio"] <= 0.3)
    out["pin_bear"] = (out["upper_wick"] >= 0.6) & (out["body_ratio"] <= 0.3)
    avg_rng = out["range"].rolling(20).mean()
    out["momentum"] = (out["body_ratio"] >= 0.7) & (out["range"] >= 1.2 * avg_rng)
    out["narrow"] = out["range"] <= 0.5 * avg_rng
    out["wide"] = out["range"] >= 1.8 * avg_rng
    return out
