"""PA02 - Breakout and Retest.

Regimes: any except SIDEWAYS+LOW volatility. Level L = PDH/PDL, ORH/ORL, PWH/PWL or a zone with strength >= 50.
Breakout (candidate): within the last 12 setup bars a bar closed beyond L by >= 0.2 ATR with body_ratio >= 0.5.
Acceptance: at least one further close beyond L after the breakout bar.
Retest: the decision bar's low (for upside) comes back to within 0.35 ATR of L, does not close back
inside by more than 0.1 ATR, and closes in the direction of the breakout (close_loc >= 0.55).
Entry = close. Stop = L - 0.5 ATR (below the retest low if lower). Targets from levels / R multiples.
Mirror for breakdowns.
"""

from __future__ import annotations

from mmm.signals.model import SetupCandidate
from mmm.strategies.base import Strategy, StrategyContext

LEVEL_KINDS = ("PDH", "PDL", "PWH", "PWL", "ORH", "ORL", "ZONE_R", "ZONE_S")


class BreakoutRetest(Strategy):
    id = "PA02"
    name = "Breakout and Retest"
    version = "0.1.0"

    def evaluate(self, ctx: StrategyContext) -> SetupCandidate | None:
        if len(ctx.setup) < 30:
            return None
        if ctx.regime.direction == "SIDEWAYS" and ctx.regime.volatility == "LOW":
            return None
        df = ctx.setup
        a = ctx.atr
        an = ctx.anatomy
        bar, abar = df.iloc[-1], an.iloc[-1]
        recent = df.iloc[-13:-1]
        levels = [lv for lv in ctx.levels if lv.kind in LEVEL_KINDS and (not lv.kind.startswith("ZONE") or lv.strength >= 50)]
        for lv in sorted(levels, key=lambda x: -x.strength):
            L = lv.price
            # upside
            brk = recent[(recent["close"] > L + 0.2 * a)]
            if not brk.empty:
                b_idx = brk.index[0]
                b_an = an.loc[b_idx]
                after = df.loc[b_idx:].iloc[1:-1]
                accepted = (after["close"] > L).sum() >= 1
                if b_an["body_ratio"] >= 0.5 and accepted and bar["low"] <= L + 0.35 * a and bar["close"] >= L - 0.1 * a and abar["close_loc"] >= 0.55:
                    entry = float(bar["close"])
                    stop = float(min(L - 0.5 * a, bar["low"] - 0.15 * a))
                    risk = entry - stop
                    t1, t2 = self.targets_from_levels(ctx, 1, entry, risk)
                    rr = (t1 - entry) / risk
                    if rr < self.min_reward_risk:
                        continue
                    ev = [f"broke {lv.kind} {L:.0f} with body {b_an['body_ratio']:.2f}", f"acceptance closes: {int((after['close'] > L).sum())}",
                          f"retest low {bar['low']:.0f}, close_loc {abar['close_loc']:.2f}"]
                    con = [] if ctx.regime.direction != "BEARISH" else ["regime BEARISH against upside breakout"]
                    pa = 50 + min(lv.strength / 4, 20) + (10 if ctx.regime.direction == "BULLISH" else 0) + min(10, (rr - 1.5) * 8)
                    return SetupCandidate(strategy_id=self.id, direction="BULLISH", timeframe=ctx.setup_tf, trigger_event="BREAKOUT_RETEST",
                                          entry=entry, stop=stop, target1=t1, target2=t2, invalidation=f"close below {L - 0.1 * a:.0f}",
                                          pa_score=float(min(pa, 100)), evidence=ev, contradictions=con, bar_ts=bar["ts"].to_pydatetime())
            # downside
            brk = recent[(recent["close"] < L - 0.2 * a)]
            if not brk.empty:
                b_idx = brk.index[0]
                b_an = an.loc[b_idx]
                after = df.loc[b_idx:].iloc[1:-1]
                accepted = (after["close"] < L).sum() >= 1
                if b_an["body_ratio"] >= 0.5 and accepted and bar["high"] >= L - 0.35 * a and bar["close"] <= L + 0.1 * a and abar["close_loc"] <= 0.45:
                    entry = float(bar["close"])
                    stop = float(max(L + 0.5 * a, bar["high"] + 0.15 * a))
                    risk = stop - entry
                    t1, t2 = self.targets_from_levels(ctx, -1, entry, risk)
                    rr = (entry - t1) / risk
                    if rr < self.min_reward_risk:
                        continue
                    ev = [f"broke down {lv.kind} {L:.0f} with body {b_an['body_ratio']:.2f}", f"acceptance closes: {int((after['close'] < L).sum())}",
                          f"retest high {bar['high']:.0f}, close_loc {abar['close_loc']:.2f}"]
                    con = [] if ctx.regime.direction != "BULLISH" else ["regime BULLISH against downside breakdown"]
                    pa = 50 + min(lv.strength / 4, 20) + (10 if ctx.regime.direction == "BEARISH" else 0) + min(10, (rr - 1.5) * 8)
                    return SetupCandidate(strategy_id=self.id, direction="BEARISH", timeframe=ctx.setup_tf, trigger_event="BREAKDOWN_RETEST",
                                          entry=entry, stop=stop, target1=t1, target2=t2, invalidation=f"close above {L + 0.1 * a:.0f}",
                                          pa_score=float(min(pa, 100)), evidence=ev, contradictions=con, bar_ts=bar["ts"].to_pydatetime())
        return None
