"""Market regime = trend direction x strength x volatility x price behaviour. Rule-based baseline."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from mmm.analytics.structure import Structure
from mmm.analytics.volatility import compression, vol_regime


@dataclass(frozen=True)
class Regime:
    direction: str      # BULLISH / BEARISH / SIDEWAYS
    strength: str       # STRONG / MODERATE / WEAK
    volatility: str     # HIGH / NORMAL / LOW / UNKNOWN
    behaviour: str      # TRENDING / MEAN_REVERTING / COMPRESSING / EXPANDING / TRANSITIONING

    @property
    def code(self) -> str:
        return f"{self.direction}|{self.strength}|{self.volatility}|{self.behaviour}"

    def as_dict(self) -> dict[str, str]:
        return {"direction": self.direction, "strength": self.strength, "volatility": self.volatility,
                "behaviour": self.behaviour, "code": self.code}


def classify_regime(df: pd.DataFrame, structure: Structure, ema_fast: int = 20, ema_slow: int = 50) -> Regime:
    """Combine structure labels with EMA slope and ATR compression for a deterministic regime code."""
    if len(df) < ema_slow + 5:
        return Regime("SIDEWAYS", "WEAK", vol_regime(df), "TRANSITIONING")
    close = df["close"]
    ef, es = close.ewm(span=ema_fast, adjust=False).mean(), close.ewm(span=ema_slow, adjust=False).mean()
    slope = (es.iloc[-1] - es.iloc[-6]) / max(es.iloc[-6], 1e-9)
    above = close.iloc[-1] > ef.iloc[-1] > es.iloc[-1]
    below = close.iloc[-1] < ef.iloc[-1] < es.iloc[-1]

    t = structure.trend
    if t in ("STRONG_BULL", "WEAK_BULL") and not below:
        direction = "BULLISH"
    elif t in ("STRONG_BEAR", "WEAK_BEAR") and not above:
        direction = "BEARISH"
    elif above and slope > 0:
        direction = "BULLISH"
    elif below and slope < 0:
        direction = "BEARISH"
    else:
        direction = "SIDEWAYS"

    if direction == "SIDEWAYS":
        strength = "WEAK"
    elif t.startswith("STRONG") and (above or below):
        strength = "STRONG"
    elif t.startswith("STRONG") or (above or below):
        strength = "MODERATE"
    else:
        strength = "WEAK"

    comp = compression(df).dropna()
    c = float(comp.iloc[-1]) if len(comp) else 1.0
    if structure.last_event in ("CHOCH_UP", "CHOCH_DOWN") or t == "TRANSITION":
        behaviour = "TRANSITIONING"
    elif c < 0.7:
        behaviour = "COMPRESSING"
    elif c > 1.3:
        behaviour = "EXPANDING"
    elif direction == "SIDEWAYS":
        behaviour = "MEAN_REVERTING"
    else:
        behaviour = "TRENDING"
    return Regime(direction, strength, vol_regime(df), behaviour)
