"""Price-action strategy library. Each strategy is deterministic, versioned and independently switchable."""

from __future__ import annotations

from mmm.strategies.base import Strategy, StrategyContext


def load_strategies(enabled: list[str] | None = None) -> list[Strategy]:
    from mmm.strategies.pa01_trend_pullback import TrendPullback
    from mmm.strategies.pa02_breakout_retest import BreakoutRetest
    from mmm.strategies.pa03_false_breakout import FalseBreakout
    from mmm.strategies.pa04_orb import OpeningRangeBreakout
    from mmm.strategies.pa05_sr_rejection import SRRejection

    all_: list[Strategy] = [TrendPullback(), BreakoutRetest(), FalseBreakout(), OpeningRangeBreakout(), SRRejection()]
    if enabled is None:
        return all_
    return [s for s in all_ if s.id in enabled]


__all__ = ["Strategy", "StrategyContext", "load_strategies"]
