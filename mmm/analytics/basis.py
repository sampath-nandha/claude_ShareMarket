"""Spot-futures parity: fair-value basis, premium versus fair value, intraday z-score, divergence.

Raw basis (futures - spot) shrinks every day towards expiry because it is mostly cost of carry. What carries
information is the *premium over fair value*: basis - spot x (exp((r - q) T) - 1). A premium that rises with
price confirms the move; a premium that contracts while spot makes a new high (or expands at a new low) is a
divergence: futures are not following. Everything here is DERIVED from Kite candles with configured r and q.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np
import pandas as pd

from mmm.settings import get_config
from mmm.timeutil import expiry_datetime

YEAR_S = 365.0 * 24 * 3600


def fair_basis(spot: float, expiry: date, as_of: datetime, r: float | None = None, q: float | None = None) -> float:
    pc = get_config().pricing
    r = pc.risk_free_rate if r is None else r
    q = pc.dividend_yield if q is None else q
    t = max((expiry_datetime(expiry) - as_of).total_seconds(), 0.0) / YEAR_S
    return spot * (math.exp((r - q) * t) - 1.0)


def basis_frame(spot: pd.DataFrame, fut: pd.DataFrame, z_window: int = 750, z_min: int = 120) -> pd.DataFrame:
    """Minute-aligned parity frame.

    spot: index 1-minute candles (ts, close, ...). fut: continuous futures (ts, close, oi, expiry).
    Columns: ts, spot, fut, basis, fair, premium, premium_bps, z, fut_oi, expiry. `z` is a causal rolling
    z-score of the premium over ~2 sessions (no look-ahead).
    """
    if spot is None or fut is None or spot.empty or fut.empty:
        return pd.DataFrame(columns=["ts", "spot", "fut", "basis", "fair", "premium", "premium_bps", "z", "fut_oi", "expiry"])
    s = spot[["ts", "close"]].rename(columns={"close": "spot"})
    f = fut[["ts", "close", "oi", "expiry"]].rename(columns={"close": "fut", "oi": "fut_oi"})
    df = s.merge(f, on="ts", how="inner").sort_values("ts").reset_index(drop=True)
    if df.empty:
        return df.assign(basis=[], fair=[], premium=[], premium_bps=[], z=[])
    pc = get_config().pricing
    carry = pc.risk_free_rate - pc.dividend_yield
    exp_map = {e: pd.Timestamp(expiry_datetime(e)) for e in pd.unique(df["expiry"])}
    exp_close = pd.to_datetime(df["expiry"].map(exp_map), utc=True)
    t_years = np.clip((exp_close - df["ts"].dt.tz_convert("UTC")).dt.total_seconds().to_numpy() / YEAR_S, 0, None)
    df["basis"] = df["fut"] - df["spot"]
    df["fair"] = df["spot"] * (np.exp(carry * t_years) - 1.0)
    df["premium"] = df["basis"] - df["fair"]
    df["premium_bps"] = df["premium"] / df["spot"] * 1e4
    roll = df["premium"].rolling(z_window, min_periods=z_min)
    sd = roll.std().replace(0, np.nan)
    df["z"] = (df["premium"] - roll.mean()) / sd
    return df[["ts", "spot", "fut", "basis", "fair", "premium", "premium_bps", "z", "fut_oi", "expiry"]]


@dataclass
class Divergence:
    kind: str                 # BEARISH (spot up, futures lag) | BULLISH (spot down, futures hold)
    premium_change: float     # points over the lookback
    z: float | None
    oi_change_pct: float | None
    strength: float           # 0..1
    evidence: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class BasisState:
    ts: datetime
    spot: float
    fut: float
    basis: float
    fair: float
    premium: float
    premium_bps: float
    z: float | None
    expiry: date
    premium_change_30: float | None
    oi_change_30_pct: float | None
    divergence: Divergence | None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["divergence"] = self.divergence.as_dict() if self.divergence else None
        return d


def divergence(frame: pd.DataFrame, lookback: int = 30, near_extreme: float = 0.15, min_drop_sd: float = 0.75,
               min_points: float = 2.0) -> Divergence | None:
    """Detect a premium divergence on the last row of `frame` (today's rows up to as_of, causal).

    Bearish: spot is within `near_extreme` of the lookback range from its high (a fresh high zone) while the
    premium fell by >= max(min_points, min_drop_sd x session premium std) over the lookback. Bullish mirrored.
    """
    if frame is None or len(frame) < lookback + 5:
        return None
    w = frame.iloc[-lookback - 1:]
    sp = w["spot"].to_numpy(float)
    hi, lo = sp.max(), sp.min()
    rng = hi - lo
    if rng <= 0:
        return None
    pos = (sp[-1] - lo) / rng                       # 1 = at the lookback high
    dprem = float(w["premium"].iloc[-1] - w["premium"].iloc[0])
    sd = float(frame["premium"].std()) if len(frame) > 20 else float("nan")
    need = max(min_points, min_drop_sd * sd) if not math.isnan(sd) else min_points
    oi0, oi1 = float(w["fut_oi"].iloc[0]), float(w["fut_oi"].iloc[-1])
    oi_pct = (oi1 - oi0) / oi0 * 100 if oi0 and not (math.isnan(oi0) or math.isnan(oi1)) else None
    zl = frame["z"].iloc[-1] if "z" in frame else float("nan")
    z = None if pd.isna(zl) else float(zl)
    kind = None
    if pos >= 1 - near_extreme and dprem <= -need:
        kind = "BEARISH"
    elif pos <= near_extreme and dprem >= need:
        kind = "BULLISH"
    if kind is None:
        return None
    strength = min(1.0, abs(dprem) / (2 * need))
    side, verb = ("high", "fell") if kind == "BEARISH" else ("low", "rose")
    ev = [f"spot at the {lookback}m {side} while the premium {verb} {abs(dprem):.1f} pts (threshold {need:.1f})"]
    if oi_pct is not None:
        if kind == "BEARISH" and oi_pct > 0.3:
            ev.append(f"futures OI +{oi_pct:.1f}% with a falling premium (short build-up by the price/OI convention)")
            strength = min(1.0, strength + 0.15)
        elif kind == "BULLISH" and oi_pct > 0.3:
            ev.append(f"futures OI +{oi_pct:.1f}% with a rising premium at the low (long build-up by convention)")
            strength = min(1.0, strength + 0.15)
        elif oi_pct < -0.3:
            ev.append(f"futures OI {oi_pct:.1f}% (positions closing)")
    return Divergence(kind, dprem, z, oi_pct, strength, ev)


def basis_state(frame_today: pd.DataFrame, lookback: int = 30) -> BasisState | None:
    """Latest parity state from today's rows of a basis frame (ts < as_of)."""
    if frame_today is None or frame_today.empty:
        return None
    last = frame_today.iloc[-1]
    prev = frame_today.iloc[-lookback - 1] if len(frame_today) > lookback else None
    pchg = float(last["premium"] - prev["premium"]) if prev is not None else None
    oichg = None
    if prev is not None and prev["fut_oi"]:
        oichg = float((last["fut_oi"] - prev["fut_oi"]) / prev["fut_oi"] * 100)
    z = None if pd.isna(last["z"]) else float(last["z"])
    return BasisState(last["ts"].to_pydatetime(), float(last["spot"]), float(last["fut"]), float(last["basis"]), float(last["fair"]),
                      float(last["premium"]), float(last["premium_bps"]), z, last["expiry"], pchg, oichg,
                      divergence(frame_today, lookback), ["DERIVED: fair value uses configured r/q; index close vs futures close per minute"])


def premium_confirmation(state: BasisState | None, direction: int) -> float:
    """-1..+1: does the futures premium support a trade in `direction`? Used by scoring only when validated."""
    if state is None:
        return 0.0
    score = 0.0
    if state.z is not None:
        score += float(np.clip(state.z / 2.0, -1, 1)) * 0.5 * direction
    if state.divergence is not None:
        score += (-1 if state.divergence.kind == "BEARISH" else 1) * direction * 0.5 * state.divergence.strength
    return float(np.clip(score, -1, 1))
