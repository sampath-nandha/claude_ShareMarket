"""Strategy interface. The same `evaluate` is used by backtests, replay, live detection and paper trading."""

from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime

import pandas as pd

from mmm.analytics.levels import Level
from mmm.analytics.regime import Regime
from mmm.analytics.structure import Structure, Swing
from mmm.signals.model import SetupCandidate


@dataclass
class StrategyContext:
    """Everything a strategy may look at, computed strictly from data available at `as_of`.

    `setup` is the setup-timeframe frame (completed bars only, index 0..n-1, last row = decision bar).
    `context`/`trend` are higher-timeframe frames (completed bars only).
    """

    as_of: datetime
    today: date
    setup: pd.DataFrame
    setup_tf: str
    setup_swings: list[Swing]
    setup_structure: Structure
    context: pd.DataFrame
    context_structure: Structure
    trend: pd.DataFrame
    trend_structure: Structure
    regime: Regime
    atr: float
    levels: list[Level]
    daily: pd.DataFrame
    intraday_today: pd.DataFrame      # 1-minute bars of the current session up to as_of
    extras: dict = field(default_factory=dict)
    anatomy: pd.DataFrame | None = None       # candle_anatomy(setup), computed once per context

    @property
    def close(self) -> float:
        return float(self.setup["close"].iloc[-1])

    @property
    def last_bar(self) -> pd.Series:
        return self.setup.iloc[-1]

    def nearest_above(self, price: float | None = None, kinds: tuple[str, ...] | None = None) -> Level | None:
        price = self.close if price is None else price
        c = [lv for lv in self.levels if lv.price > price and (kinds is None or lv.kind in kinds)]
        return min(c, key=lambda lv: lv.price) if c else None

    def nearest_below(self, price: float | None = None, kinds: tuple[str, ...] | None = None) -> Level | None:
        price = self.close if price is None else price
        c = [lv for lv in self.levels if lv.price <= price and (kinds is None or lv.kind in kinds)]
        return max(c, key=lambda lv: lv.price) if c else None


class Strategy(ABC):
    id: str = "PA00"
    name: str = "base"
    version: str = "0.1.0"
    status: str = "EXPERIMENTAL"          # see Part H10
    eligible_regimes: tuple[str, ...] = ()  # regime.direction values; empty = any
    min_reward_risk: float = 1.5

    @abstractmethod
    def evaluate(self, ctx: StrategyContext) -> SetupCandidate | None:
        """Return a SetupCandidate when Stage-1 price-action conditions hold on the decision bar, else None."""

    def regime_ok(self, regime: Regime) -> bool:
        return not self.eligible_regimes or regime.direction in self.eligible_regimes

    def spec(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name, "version": self.version, "status": self.status,
                "eligible_regimes": ",".join(self.eligible_regimes) or "ANY",
                "doc": (self.__doc__ or sys.modules[self.__class__.__module__].__doc__ or "").strip()}

    @staticmethod
    def targets_from_levels(ctx: StrategyContext, direction: int, entry: float, risk: float) -> tuple[float, float]:
        """T1 = nearest meaningful level in trade direction (>= 1R away), else 1.5R; T2 = next level or 2.5R."""
        kinds = ("PDH", "PDL", "PWH", "PWL", "ZONE_R", "ZONE_S", "SH", "SL", "ORH", "ORL", "ROUND")
        if direction > 0:
            cands = sorted([lv.price for lv in ctx.levels if lv.kind in kinds and lv.price >= entry + risk], key=lambda p: p)
        else:
            cands = sorted([lv.price for lv in ctx.levels if lv.kind in kinds and lv.price <= entry - risk], key=lambda p: -p)
        t1 = cands[0] if cands else entry + direction * 1.5 * risk
        t2 = cands[1] if len(cands) > 1 else entry + direction * 2.5 * risk
        if abs(t2 - entry) <= abs(t1 - entry):
            t2 = entry + direction * (abs(t1 - entry) + risk)
        return float(t1), float(t2)
