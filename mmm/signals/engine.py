"""Context builder and two-stage signal engine.

Stage 1: strategies' deterministic price-action checks (SetupCandidate or None).
Stage 2: confirmation (MTF alignment, futures, options, breadth, liquidity) -> score -> classification,
         risk checks, option selection. Stage 2 can only downgrade/annotate; it never creates a trade
         when Stage 1 found nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

from mmm.analytics.candles import completed_only, resample, session_slice
from mmm.analytics.levels import Level, reference_levels, round_levels, swing_zones
from mmm.analytics.regime import classify_regime
from mmm.analytics.structure import classify_structure, pivot_swings, trend_direction
from mmm.analytics.volatility import atr
from mmm.data.quality import DQ
from mmm.risk.limits import RiskState, check as risk_check
from mmm.settings import get_config
from mmm.signals.model import SetupCandidate, Signal
from mmm.signals.scoring import Components, classify, score
from mmm.strategies import Strategy, StrategyContext, load_strategies


def build_context(minute_bars: pd.DataFrame, as_of: datetime, setup_tf: str | None = None,
                  context_tf: str | None = None, trend_tf: str | None = None) -> StrategyContext | None:
    """Build a StrategyContext strictly from 1-minute bars with ts < as_of (no look-ahead)."""
    cfg = get_config().strategies
    setup_tf = setup_tf or cfg.setup_timeframe
    context_tf = context_tf or cfg.context_timeframe
    trend_tf = trend_tf or cfg.trend_timeframe
    past = minute_bars[minute_bars["ts"] < as_of]
    if len(past) < 400:
        return None
    setup = completed_only(resample(past, setup_tf), setup_tf, as_of).reset_index(drop=True)
    context = completed_only(resample(past, context_tf), context_tf, as_of).reset_index(drop=True)
    trend = completed_only(resample(past, trend_tf), trend_tf, as_of).reset_index(drop=True)
    daily = resample(past, "day")
    if len(setup) < 40 or len(context) < 20:
        return None
    setup = setup.tail(600).reset_index(drop=True)
    context = context.tail(300).reset_index(drop=True)
    trend = trend.tail(200).reset_index(drop=True)
    s_sw = pivot_swings(setup, 3, 3)
    c_sw = pivot_swings(context, 3, 3)
    t_sw = pivot_swings(trend, 2, 2)
    s_struct = classify_structure(setup, s_sw)
    c_struct = classify_structure(context, c_sw)
    t_struct = classify_structure(trend, t_sw) if len(trend) > 10 else c_struct
    regime = classify_regime(context, c_struct)
    a_series = atr(setup, 14).dropna()
    a = float(a_series.iloc[-1]) if len(a_series) else float((setup["high"] - setup["low"]).mean())
    today = as_of.date()
    intraday_today = session_slice(past, today)
    levels: list[Level] = reference_levels(daily, intraday_today, today)
    levels += swing_zones(setup, s_sw, len(setup) - 1, tf=setup_tf)
    levels += swing_zones(context, c_sw, len(context) - 1, tf=context_tf)
    levels += round_levels(float(setup["close"].iloc[-1]))
    return StrategyContext(
        as_of=as_of, today=today, setup=setup, setup_tf=setup_tf, setup_swings=s_sw, setup_structure=s_struct,
        context=context, context_structure=c_struct, trend=trend, trend_structure=t_struct, regime=regime, atr=a,
        levels=levels, daily=daily, intraday_today=intraday_today,
    )


def stage1(ctx: StrategyContext, strategies: list[Strategy] | None = None) -> list[tuple[Strategy, SetupCandidate]]:
    strategies = strategies or load_strategies(get_config().strategies.enabled)
    out = []
    for s in strategies:
        try:
            cand = s.evaluate(ctx)
        except Exception as exc:  # a broken strategy must never take the engine down
            import structlog

            structlog.get_logger(__name__).error("strategy_error", strategy=s.id, error=str(exc))
            cand = None
        if cand is not None:
            out.append((s, cand))
    return dedupe(out)


def dedupe(cands: list[tuple[Strategy, SetupCandidate]]) -> list[tuple[Strategy, SetupCandidate]]:
    """One candidate per direction per decision bar: keep the highest PA score, record the others as evidence.

    Several strategies can describe the same bar (e.g. PA03 sweep and PA05 rejection at the same zone). Emitting
    both would double-count exposure and inflate trade counts, so the winner carries the coincident ids.
    """
    best: dict[str, tuple[Strategy, SetupCandidate]] = {}
    for s, c in cands:
        cur = best.get(c.direction)
        if cur is None or c.pa_score > cur[1].pa_score:
            best[c.direction] = (s, c)
    out = []
    for direction, (s, c) in best.items():
        others = [f"{s2.id} ({c2.trigger_event})" for s2, c2 in cands if c2.direction == direction and s2.id != s.id]
        if others:
            c = c.model_copy(update={"evidence": c.evidence + ["coincident: " + ", ".join(others)]})
        out.append((s, c))
    # opposite-direction candidates on the same bar are a contradiction, not two trades
    if len(out) == 2:
        (s_a, c_a), (s_b, c_b) = out
        note_a = f"opposing setup from {s_b.id} ({c_b.trigger_event}) on the same bar"
        note_b = f"opposing setup from {s_a.id} ({c_a.trigger_event}) on the same bar"
        out = [(s_a, c_a.model_copy(update={"contradictions": c_a.contradictions + [note_a]})),
               (s_b, c_b.model_copy(update={"contradictions": c_b.contradictions + [note_b]}))]
    return out


@dataclass
class Confirmations:
    futures: dict[str, Any] | None = None       # expects 'confirmation_score' in -1..1
    options: dict[str, Any] | None = None       # expects 'confirmation_score' in -1..1
    breadth: dict[str, Any] | None = None       # expects 'score' in -1..1 and 'authoritative'
    liquidity: dict[str, Any] | None = None     # expects 'score' 0..1 and optional 'spread_pct'
    recommended_option: dict[str, Any] | None = None
    option_alternatives: list[dict[str, Any]] | None = None
    data_quality: DQ = DQ.PARTIAL
    premium_risk_per_unit: float | None = None


def mtf_alignment(ctx: StrategyContext, direction: int) -> float:
    votes = [trend_direction(ctx.setup_structure.trend), trend_direction(ctx.context_structure.trend), trend_direction(ctx.trend_structure.trend)]
    weights = [0.25, 0.4, 0.35]
    return float(sum(w * v * direction for w, v in zip(weights, votes)))


def stage2(ctx: StrategyContext, strategy: Strategy, cand: SetupCandidate, conf: Confirmations,
           risk_state: RiskState | None = None, now: datetime | None = None) -> Signal:
    d = 1 if cand.direction == "BULLISH" else -1
    now = now or ctx.as_of
    fut = (conf.futures or {}).get("confirmation_score", 0.0) or 0.0
    opt = (conf.options or {}).get("confirmation_score", 0.0) or 0.0
    br = conf.breadth or {}
    breadth_score = (br.get("score", 0.0) or 0.0) * d if br.get("authoritative", False) else 0.0
    liq = (conf.liquidity or {}).get("score", 0.5)
    comps = Components(cand.pa_score, mtf_alignment(ctx, d), fut, opt, breadth_score, liq)
    total, parts = score(comps)
    contradictions = list(cand.contradictions)
    if comps.mtf_alignment < 0:
        contradictions.append("higher timeframes oppose the setup")
    if fut < -0.2:
        contradictions.append("futures volume/OI do not confirm")
    if opt < -0.2:
        contradictions.append("options activity does not confirm")
    if br and not br.get("authoritative", False):
        contradictions.append(f"breadth coverage {br.get('coverage_pct', 0):.0f}% - not authoritative, ignored")
    rc = risk_check(risk_state or RiskState(), d, cand.reward_risk, conf.premium_risk_per_unit,
                    (conf.liquidity or {}).get("spread_pct"), cand.bar_ts, now, conf.data_quality)
    do_not = list(rc.reasons)
    if strategy.status not in ("ACCEPTED_FOR_PAPER_TRADING", "FORWARD_VALIDATED"):
        do_not.append(f"strategy {strategy.id} status {strategy.status}: research-only, not an actionable recommendation")
    if conf.recommended_option is None and conf.options is not None:
        do_not.append("NO SUITABLE OPTION (liquidity/delta/spread filters)")
    status = classify(total, True, rc.ok)
    wp, wp_note = _calibrated_probability(cand.pa_score)
    return Signal(
        created_at=now, direction=cand.direction, strategy_id=strategy.id, regime=ctx.regime.code, timeframe=cand.timeframe,
        trigger_event=cand.trigger_event, entry=cand.entry, entry_zone=cand.entry_zone, stop=cand.stop, target1=cand.target1,
        target2=cand.target2, reward_risk=round(cand.reward_risk, 2), invalidation=cand.invalidation, score=total, status=status,
        win_probability=wp, win_probability_note=wp_note,
        evidence=cand.evidence, contradictions=contradictions, futures_confirmation=conf.futures, options_confirmation=conf.options,
        breadth_confirmation=br or None, liquidity=conf.liquidity, data_quality=conf.data_quality.name,
        recommended_option=conf.recommended_option, option_alternatives=conf.option_alternatives or [],
        expected_spread_pct=(conf.liquidity or {}).get("spread_pct"), suggested_max_risk_rupees=rc.rupee_risk or None,
        suggested_lots=rc.max_lots or None, expiry_conditions=f"{cand.expires_after_bars} x {cand.timeframe} bars or invalidation",
        do_not_execute_reasons=do_not, strategy_status=strategy.status, component_scores=parts,
    )


def _calibrated_probability(pa_score: float) -> tuple[float | None, str]:
    """Out-of-sample calibrated win probability for the price-action score, or None with the reason."""
    try:
        from mmm.backtest import calibration

        cal = calibration.load("pooled")
    except Exception:
        cal = None
    if cal is None:
        return None, "no calibration fitted yet (run `mmm research run` on real archived history)"
    p, note = cal.predict(pa_score)
    return (round(p, 3) if p is not None else None), note


def run_engine(minute_bars: pd.DataFrame, as_of: datetime, conf_builder=None, risk_state: RiskState | None = None,
               strategies: list[Strategy] | None = None) -> tuple[StrategyContext | None, list[Signal]]:
    """Convenience: build context, run both stages. `conf_builder(ctx, cand) -> Confirmations`."""
    ctx = build_context(minute_bars, as_of)
    if ctx is None:
        return None, []
    signals = []
    for strat, cand in stage1(ctx, strategies):
        conf = conf_builder(ctx, cand) if conf_builder else Confirmations()
        signals.append(stage2(ctx, strat, cand, conf, risk_state))
    return ctx, signals
