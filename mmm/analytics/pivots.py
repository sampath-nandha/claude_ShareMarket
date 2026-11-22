"""Pre-open level sheet: floor pivots, central pivot range (CPR) and confluence clusters.

All inputs are the previous completed session (no look-ahead). These are reference levels traders watch, not
predictions; whether any of them improves a setup is decided by the Phase-4 feature study, not assumed here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from mmm.analytics.levels import Level


@dataclass
class CPR:
    pivot: float
    bc: float            # bottom central  = (H + L) / 2, ordered so bc <= tc
    tc: float            # top central     = 2P - BC
    width: float
    width_pct: float     # width / pivot * 100
    width_rank: float | None   # percentile of today's width within the trailing window (0-1); None if too short
    width_class: str     # NARROW | NORMAL | WIDE | UNKNOWN

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Cluster:
    price: float
    low: float
    high: float
    score: float                     # 0-100
    members: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"price": round(self.price, 2), "low": round(self.low, 2), "high": round(self.high, 2), "score": round(self.score, 1),
                "members": self.members}


def floor_pivots(high: float, low: float, close: float) -> dict[str, float]:
    p = (high + low + close) / 3
    rng = high - low
    return {"P": p, "R1": 2 * p - low, "S1": 2 * p - high, "R2": p + rng, "S2": p - rng,
            "R3": high + 2 * (p - low), "S3": low - 2 * (high - p)}


def _cpr_values(high: float, low: float, close: float) -> tuple[float, float, float]:
    p = (high + low + close) / 3
    bc = (high + low) / 2
    tc = 2 * p - bc
    return p, min(bc, tc), max(bc, tc)


def cpr(daily: pd.DataFrame, lookback: int = 60) -> CPR | None:
    """CPR for the session after the last row of `daily` (completed sessions only)."""
    if daily is None or daily.empty:
        return None
    last = daily.iloc[-1]
    p, bc, tc = _cpr_values(float(last["high"]), float(last["low"]), float(last["close"]))
    width_pct = (tc - bc) / p * 100 if p else 0.0
    hist = daily.tail(lookback + 1)
    rank: float | None = None
    cls = "UNKNOWN"
    if len(hist) >= 20:
        h, lo, c = hist["high"].to_numpy(float), hist["low"].to_numpy(float), hist["close"].to_numpy(float)
        pp = (h + lo + c) / 3
        widths = np.abs(2 * pp - 2 * ((h + lo) / 2)) / pp * 100
        rank = float((widths[:-1] < width_pct).mean())
        cls = "NARROW" if rank <= 0.2 else ("WIDE" if rank >= 0.8 else "NORMAL")
    return CPR(p, bc, tc, tc - bc, width_pct, rank, cls)


def pivot_levels(daily: pd.DataFrame) -> list[Level]:
    """Floor pivots + CPR as Level objects for the session after the last daily row."""
    if daily is None or daily.empty:
        return []
    last = daily.iloc[-1]
    fp = floor_pivots(float(last["high"]), float(last["low"]), float(last["close"]))
    strength = {"P": 55, "R1": 50, "S1": 50, "R2": 45, "S2": 45, "R3": 40, "S3": 40}
    out = [Level(v, f"PIV_{k}", "day", strength[k]) for k, v in fp.items()]
    c = cpr(daily)
    if c is not None:
        tag = [c.width_class]
        out += [Level(c.tc, "CPR_TC", "day", 50, tags=tag), Level(c.bc, "CPR_BC", "day", 50, tags=tag)]
    return out


def confluence(levels: list[Level], atr_value: float, tolerance_atr: float = 0.25, min_members: int = 2,
               exclude: tuple[str, ...] = ("ROUND",)) -> list[Cluster]:
    """Merge levels from different sources that sit within `tolerance_atr` x ATR of each other.

    Score = 100 x (1 - prod(1 - strength_i/100)) over distinct kinds, so two strong independent levels
    score higher than either alone and duplicates of one kind do not stack.
    """
    tol = max(atr_value * tolerance_atr, 1e-9)
    pts = sorted((lv for lv in levels if lv.kind not in exclude), key=lambda lv: lv.price)
    clusters: list[list[Level]] = []
    for lv in pts:
        if clusters and lv.price - clusters[-1][-1].price <= tol:
            clusters[-1].append(lv)
        else:
            clusters.append([lv])
    out: list[Cluster] = []
    for grp in clusters:
        best: dict[str, Level] = {}
        for lv in grp:
            if lv.kind not in best or lv.strength > best[lv.kind].strength:
                best[lv.kind] = lv
        if len(best) < min_members:
            continue
        miss = 1.0
        for lv in best.values():
            miss *= 1 - min(max(lv.strength, 0), 95) / 100
        w = np.array([lv.strength for lv in best.values()], float) + 1e-9
        price = float(np.average([lv.price for lv in best.values()], weights=w))
        out.append(Cluster(price, min(lv.price for lv in grp), max(lv.price for lv in grp), 100 * (1 - miss),
                           sorted(f"{k} {v.price:,.0f}" for k, v in best.items())))
    return sorted(out, key=lambda c: -c.score)


def cluster_levels(clusters: list[Cluster], price: float, top_n: int = 6) -> list[Level]:
    """Top clusters as Level objects (CONF_R above price, CONF_S below)."""
    out = []
    for c in clusters[:top_n]:
        out.append(Level(c.price, "CONF_R" if c.price > price else "CONF_S", "confluence", min(c.score, 95), tags=c.members))
    return out
