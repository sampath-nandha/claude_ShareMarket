"""Price-action strategy library. Each strategy is deterministic, versioned and independently switchable.

Strategy *status* (Part H10) is not hard-coded: it is read from the research registry (state DB), which is
only advanced by `mmm research run` when the statistical gates pass. New strategies start EXPERIMENTAL.
"""

from __future__ import annotations

from mmm.strategies.base import Strategy, StrategyContext


def _all() -> list[Strategy]:
    from mmm.strategies.pa01_trend_pullback import TrendPullback
    from mmm.strategies.pa02_breakout_retest import BreakoutRetest
    from mmm.strategies.pa03_false_breakout import FalseBreakout
    from mmm.strategies.pa04_orb import OpeningRangeBreakout
    from mmm.strategies.pa05_sr_rejection import SRRejection
    from mmm.strategies.pa06_structure_reversal import StructureReversal
    from mmm.strategies.pa07_consolidation_breakout import ConsolidationBreakout
    from mmm.strategies.pa08_range_mean_reversion import RangeMeanReversion
    from mmm.strategies.pa09_compression_continuation import CompressionContinuation
    from mmm.strategies.pa10_failed_breakdown_recovery import FailedBreakdownRecovery

    return [TrendPullback(), BreakoutRetest(), FalseBreakout(), OpeningRangeBreakout(), SRRejection(), StructureReversal(),
            ConsolidationBreakout(), RangeMeanReversion(), CompressionContinuation(), FailedBreakdownRecovery()]


ALL_IDS = ["PA01", "PA02", "PA03", "PA04", "PA05", "PA06", "PA07", "PA08", "PA09", "PA10"]


def load_strategies(enabled: list[str] | None = None, apply_registry: bool = True) -> list[Strategy]:
    strategies = _all()
    if enabled is not None:
        strategies = [s for s in strategies if s.id in enabled]
    if apply_registry:
        try:
            from mmm.research.registry import statuses

            st = statuses()
            for s in strategies:
                s.status = st.get(s.id, {}).get("status", s.status)
        except Exception:  # registry unavailable (no data dir yet) -> keep defaults
            pass
    return strategies


__all__ = ["Strategy", "StrategyContext", "load_strategies", "ALL_IDS"]
