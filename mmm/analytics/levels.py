"""Support/resistance levels and zone-strength scoring."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from mmm.analytics.structure import Swing
from mmm.analytics.volatility import atr
from mmm.timeutil import session_open


@dataclass
class Level:
    price: float
    kind: str                   # PDH PDL PDC PWH PWL OPEN SH SL ORH ORL ROUND SWING ZONE
    source_tf: str = ""
    strength: float = 0.0       # 0-100
    touches: int = 0
    last_touch: datetime | None = None
    tags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"price": self.price, "kind": self.kind, "tf": self.source_tf, "strength": round(self.strength, 1),
                "touches": self.touches, "last_touch": self.last_touch}


def reference_levels(daily: pd.DataFrame, intraday_today: pd.DataFrame, today: date, or_minutes: int = 15) -> list[Level]:
    """Previous-day/week levels, session open/high/low and opening range from completed data only."""
    out: list[Level] = []
    prev = daily[daily["ts"].dt.date < today]
    if not prev.empty:
        pd_row = prev.iloc[-1]
        out += [
            Level(float(pd_row["high"]), "PDH", "day", 70),
            Level(float(pd_row["low"]), "PDL", "day", 70),
            Level(float(pd_row["close"]), "PDC", "day", 55),
        ]
        wk = prev[prev["ts"].dt.date >= (today - timedelta(days=today.weekday() + 7))]
        wk = wk[wk["ts"].dt.date < today - timedelta(days=today.weekday())]
        if not wk.empty:
            out += [Level(float(wk["high"].max()), "PWH", "week", 75), Level(float(wk["low"].min()), "PWL", "week", 75)]
    if not intraday_today.empty:
        out.append(Level(float(intraday_today["open"].iloc[0]), "OPEN", "session", 45))
        out.append(Level(float(intraday_today["high"].max()), "SH", "session", 50))
        out.append(Level(float(intraday_today["low"].min()), "SL", "session", 50))
        o = session_open(today)
        orng = intraday_today[intraday_today["ts"] < o + timedelta(minutes=or_minutes)]
        if not orng.empty and intraday_today["ts"].iloc[-1] >= o + timedelta(minutes=or_minutes):
            out.append(Level(float(orng["high"].max()), "ORH", f"OR{or_minutes}", 60))
            out.append(Level(float(orng["low"].min()), "ORL", f"OR{or_minutes}", 60))
    return out


def round_levels(price: float, step: int = 100, n_each_side: int = 3, major_step: int = 500) -> list[Level]:
    base = round(price / step) * step
    out = []
    for k in range(-n_each_side, n_each_side + 1):
        p = base + k * step
        major = p % major_step == 0
        out.append(Level(float(p), "ROUND", "", 40 if major else 25, tags=["major"] if major else []))
    return out


def swing_zones(
    df: pd.DataFrame,
    swings: list[Swing],
    as_of_idx: int,
    atr_period: int = 14,
    merge_atr: float = 0.35,
    max_age_bars: int = 400,
    tf: str = "",
    atr_value: float | None = None,
) -> list[Level]:
    """Cluster confirmed swing pivots into zones and score them.

    Strength = f(independent reactions, recency, rejection strength, higher-TF weight, invalidations).
    Reactions closer than `merge_atr * ATR` are one zone; touches within 3 bars count once (not independent).
    """
    known = [s for s in swings if s.confirmed_at <= as_of_idx and as_of_idx - s.idx <= max_age_bars]
    if not known or as_of_idx < 2:
        return []
    if atr_value is not None:
        a = atr_value
    else:
        a = float(atr(df.iloc[: as_of_idx + 1], atr_period).dropna().iloc[-1]) if as_of_idx >= atr_period else float((df["high"] - df["low"]).iloc[: as_of_idx + 1].mean())
    if a <= 0:
        return []
    tol = merge_atr * a
    o, h, l, c = df["open"].to_numpy(float), df["high"].to_numpy(float), df["low"].to_numpy(float), df["close"].to_numpy(float)
    known = sorted(known, key=lambda s: s.price)
    clusters: list[list[Swing]] = []
    for s in known:
        if clusters and abs(s.price - clusters[-1][-1].price) <= tol:
            clusters[-1].append(s)
        else:
            clusters.append([s])
    close = float(c[as_of_idx])
    tf_weight = {"day": 1.4, "60minute": 1.25, "30minute": 1.15, "15minute": 1.0, "5minute": 0.85, "minute": 0.7}.get(tf, 1.0)
    out: list[Level] = []
    for cl in clusters:
        idxs = sorted(s.idx for s in cl)
        independent = 1 + sum(1 for a_, b_ in zip(idxs, idxs[1:]) if b_ - a_ > 3)
        price = float(np.mean([s.price for s in cl]))
        age = as_of_idx - max(idxs)
        recency = np.exp(-age / 150)
        rej = []
        for s in cl:
            i = s.idx
            wick = (h[i] - max(o[i], c[i])) if s.kind == "H" else (min(o[i], c[i]) - l[i])
            rej.append(min(wick / a, 2.0))
        rejection = float(np.mean(rej))
        seg = c[min(idxs) : as_of_idx + 1]
        beyond = int((seg > price + tol).sum()) if cl[0].kind == "H" else int((seg < price - tol).sum())
        invalid_pen = min(beyond / 5, 1.0)
        dist_pen = min(abs(close - price) / (6 * a), 1.0)
        strength = 100 * (0.35 * min(independent / 4, 1) + 0.25 * recency + 0.2 * (rejection / 2) + 0.2 * (1 - dist_pen))
        strength *= tf_weight * (1 - 0.5 * invalid_pen)
        kind = "R" if price > close else "S"
        out.append(Level(price, f"ZONE_{kind}", tf, float(np.clip(strength, 0, 100)), independent, cl[-1].ts.to_pydatetime(),
                         tags=[s.label for s in cl if s.label]))
    return sorted(out, key=lambda z: -z.strength)


def nearest_levels(levels: list[Level], price: float, n: int = 3) -> tuple[list[Level], list[Level]]:
    above = sorted([lv for lv in levels if lv.price > price], key=lambda lv: lv.price)[:n]
    below = sorted([lv for lv in levels if lv.price <= price], key=lambda lv: -lv.price)[:n]
    return above, below


def levels_frame(levels: list[Level]) -> pd.DataFrame:
    return pd.DataFrame([lv.as_dict() for lv in levels]).sort_values("price", ascending=False) if levels else pd.DataFrame()
