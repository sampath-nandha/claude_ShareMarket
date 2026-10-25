"""PA06 - Structure Change and Reversal.

Trigger chain: the setup TF prints a CHANGE OF CHARACTER (close beyond the last opposing swing while the prior
structure pointed the other way), then within the next 2-8 bars price retests the broken swing level (within
0.4 ATR) and the decision bar closes back in the new direction (close_loc >= 0.6 for bullish) without closing
beyond the retest extreme. Context TF must not be a STRONG trend against the new direction.
Entry = close. Stop = retest extreme -/+ 0.2 ATR. Targets from levels / R multiples.
Distinct from PA03 (which fades a level sweep immediately) because it requires the structural break first and
trades the retest, not the sweep.
"""

from __future__ import annotations

from mmm.analytics.structure import classify_structure
from mmm.signals.model import SetupCandidate
from mmm.strategies.base import Strategy, StrategyContext


class StructureReversal(Strategy):
    id = "PA06"
    name = "Structure Change and Reversal"
    version = "0.1.0"

    def evaluate(self, ctx: StrategyContext) -> SetupCandidate | None:
        df = ctx.setup
        if len(df) < 40:
            return None
        a = ctx.atr
        an = ctx.anatomy.iloc[-1]
        bar = df.iloc[-1]
        n = len(df) - 1
        # find a CHoCH on one of the bars 2..8 back
        for back in range(2, 9):
            i = n - back
            st = classify_structure(df, ctx.setup_swings, i, atr_value=a)
            if st.last_event not in ("CHOCH_UP", "CHOCH_DOWN"):
                continue
            if st.last_event == "CHOCH_UP" and st.last_high is not None:
                lvl = st.last_high.price
                if ctx.context_structure.trend == "STRONG_BEAR":
                    return None
                window = df.iloc[i + 1 : n + 1]
                retest = window["low"].min()
                if abs(retest - lvl) > 0.4 * a or bar["close"] < lvl - 0.1 * a or an["close_loc"] < 0.6:
                    return None
                entry = float(bar["close"])
                stop = float(retest - 0.2 * a)
                risk = entry - stop
                if risk < 0.3 * a:
                    return None
                t1, t2 = self.targets_from_levels(ctx, 1, entry, risk)
                rr = (t1 - entry) / risk
                if rr < self.min_reward_risk:
                    return None
                ev = [f"CHoCH up through {lvl:.0f} {back} bars ago", f"retest low {retest:.0f} held, close_loc {an['close_loc']:.2f}"]
                con = ["context TF still bearish"] if "BEAR" in ctx.context_structure.trend else []
                pa = 50 + (15 if ctx.regime.behaviour == "TRANSITIONING" else 5) + (10 if an["bull_engulf"] or an["pin_bull"] else 0) + min(10, (rr - 1.5) * 8)
                return SetupCandidate(strategy_id=self.id, direction="BULLISH", timeframe=ctx.setup_tf, trigger_event="CHOCH_RETEST_UP", entry=entry,
                                      stop=stop, target1=t1, target2=t2, invalidation=f"close below {stop:.0f}", pa_score=float(min(pa, 100)),
                                      evidence=ev, contradictions=con, bar_ts=bar["ts"].to_pydatetime())
            if st.last_event == "CHOCH_DOWN" and st.last_low is not None:
                lvl = st.last_low.price
                if ctx.context_structure.trend == "STRONG_BULL":
                    return None
                window = df.iloc[i + 1 : n + 1]
                retest = window["high"].max()
                if abs(retest - lvl) > 0.4 * a or bar["close"] > lvl + 0.1 * a or an["close_loc"] > 0.4:
                    return None
                entry = float(bar["close"])
                stop = float(retest + 0.2 * a)
                risk = stop - entry
                if risk < 0.3 * a:
                    return None
                t1, t2 = self.targets_from_levels(ctx, -1, entry, risk)
                rr = (entry - t1) / risk
                if rr < self.min_reward_risk:
                    return None
                ev = [f"CHoCH down through {lvl:.0f} {back} bars ago", f"retest high {retest:.0f} rejected, close_loc {an['close_loc']:.2f}"]
                con = ["context TF still bullish"] if "BULL" in ctx.context_structure.trend else []
                pa = 50 + (15 if ctx.regime.behaviour == "TRANSITIONING" else 5) + (10 if an["bear_engulf"] or an["pin_bear"] else 0) + min(10, (rr - 1.5) * 8)
                return SetupCandidate(strategy_id=self.id, direction="BEARISH", timeframe=ctx.setup_tf, trigger_event="CHOCH_RETEST_DOWN", entry=entry,
                                      stop=stop, target1=t1, target2=t2, invalidation=f"close above {stop:.0f}", pa_score=float(min(pa, 100)),
                                      evidence=ev, contradictions=con, bar_ts=bar["ts"].to_pydatetime())
            return None
        return None
