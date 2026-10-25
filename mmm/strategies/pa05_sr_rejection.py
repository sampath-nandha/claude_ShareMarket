"""PA05 - Support/Resistance Rejection (reversal at a strong zone).

Regimes: any; disabled when regime is STRONG trending against the trade. Zone Z: strength >= 60 (ZONE_*) or
PDH/PDL/PWH/PWL. The decision bar touches Z (wick within 0.25 ATR beyond or short of the level) and rejects:
pin bar or engulfing in the opposite direction with range >= 0.7 ATR and close back on the far side of Z by
>= 0.1 ATR. The previous 2 bars must not have already rejected the same zone (first touch only).
Entry = close. Stop = rejection extreme +/- 0.15 ATR. T1/T2 from levels / R multiples.
"""

from __future__ import annotations

from mmm.signals.model import SetupCandidate
from mmm.strategies.base import Strategy, StrategyContext

HARD = ("PDH", "PDL", "PWH", "PWL")


class SRRejection(Strategy):
    id = "PA05"
    name = "Support/Resistance Rejection"
    version = "0.1.0"

    def evaluate(self, ctx: StrategyContext) -> SetupCandidate | None:
        if len(ctx.setup) < 30:
            return None
        df = ctx.setup
        a = ctx.atr
        ana = ctx.anatomy
        an, bar = ana.iloc[-1], df.iloc[-1]
        if an["range"] < 0.7 * a:
            return None
        zones = [lv for lv in ctx.levels if lv.kind in HARD or (lv.kind.startswith("ZONE") and lv.strength >= 60)]
        prev = df.iloc[-3:-1]
        for lv in sorted(zones, key=lambda x: -x.strength):
            Z = lv.price
            # resistance rejection -> bearish
            if abs(bar["high"] - Z) <= 0.25 * a and bar["close"] <= Z - 0.1 * a and (an["pin_bear"] or an["bear_engulf"]):
                if ((prev["high"] - Z).abs() <= 0.25 * a).any():
                    continue
                if ctx.regime.direction == "BULLISH" and ctx.regime.strength == "STRONG":
                    continue
                entry = float(bar["close"])
                stop = float(bar["high"] + 0.15 * a)
                risk = stop - entry
                t1, t2 = self.targets_from_levels(ctx, -1, entry, risk)
                rr = (entry - t1) / risk
                if rr < self.min_reward_risk:
                    continue
                ev = [f"rejected {lv.kind} {Z:.0f} (strength {lv.strength:.0f})", "bearish pin" if an["pin_bear"] else "bearish engulfing"]
                con = ["counter-trend vs BULLISH regime"] if ctx.regime.direction == "BULLISH" else []
                pa = 45 + min(lv.strength / 3, 30) + (10 if ctx.regime.direction != "BULLISH" else 0) + min(10, (rr - 1.5) * 8)
                return SetupCandidate(strategy_id=self.id, direction="BEARISH", timeframe=ctx.setup_tf, trigger_event="RESISTANCE_REJECTION",
                                      entry=entry, stop=stop, target1=t1, target2=t2, invalidation=f"close above {stop:.0f}",
                                      pa_score=float(min(pa, 100)), evidence=ev, contradictions=con, bar_ts=bar["ts"].to_pydatetime())
            # support rejection -> bullish
            if abs(bar["low"] - Z) <= 0.25 * a and bar["close"] >= Z + 0.1 * a and (an["pin_bull"] or an["bull_engulf"]):
                if ((prev["low"] - Z).abs() <= 0.25 * a).any():
                    continue
                if ctx.regime.direction == "BEARISH" and ctx.regime.strength == "STRONG":
                    continue
                entry = float(bar["close"])
                stop = float(bar["low"] - 0.15 * a)
                risk = entry - stop
                t1, t2 = self.targets_from_levels(ctx, 1, entry, risk)
                rr = (t1 - entry) / risk
                if rr < self.min_reward_risk:
                    continue
                ev = [f"rejected {lv.kind} {Z:.0f} (strength {lv.strength:.0f})", "bullish pin" if an["pin_bull"] else "bullish engulfing"]
                con = ["counter-trend vs BEARISH regime"] if ctx.regime.direction == "BEARISH" else []
                pa = 45 + min(lv.strength / 3, 30) + (10 if ctx.regime.direction != "BEARISH" else 0) + min(10, (rr - 1.5) * 8)
                return SetupCandidate(strategy_id=self.id, direction="BULLISH", timeframe=ctx.setup_tf, trigger_event="SUPPORT_REJECTION",
                                      entry=entry, stop=stop, target1=t1, target2=t2, invalidation=f"close below {stop:.0f}",
                                      pa_score=float(min(pa, 100)), evidence=ev, contradictions=con, bar_ts=bar["ts"].to_pydatetime())
        return None
