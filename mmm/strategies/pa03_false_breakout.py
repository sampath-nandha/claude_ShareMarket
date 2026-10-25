"""PA03 - False Breakout (failed break / liquidity sweep reversal).

Regimes: any; strongest in SIDEWAYS / MEAN_REVERTING. Level L = PDH/PDL, ORH/ORL, PWH/PWL, SH/SL or zone >= 50.
Sweep: within the last 3 setup bars (including the decision bar) price traded beyond L by >= 0.15 ATR.
Failure: the decision bar closes back inside the level by >= 0.1 ATR, with close_loc opposing the break
(for a failed upside break: close_loc <= 0.4) and the bar's range >= 0.6 ATR.
Entry = close. Stop = sweep extreme +/- 0.15 ATR. Targets from levels / R multiples.
No claim is made about "stop hunting"; this is a measurable price pattern only.
"""

from __future__ import annotations

from mmm.analytics.structure import candle_anatomy
from mmm.signals.model import SetupCandidate
from mmm.strategies.base import Strategy, StrategyContext

LEVEL_KINDS = ("PDH", "PDL", "PWH", "PWL", "ORH", "ORL", "SH", "SL", "ZONE_R", "ZONE_S")


class FalseBreakout(Strategy):
    id = "PA03"
    name = "False Breakout"
    version = "0.1.0"

    def evaluate(self, ctx: StrategyContext) -> SetupCandidate | None:
        if len(ctx.setup) < 30:
            return None
        df = ctx.setup
        a = ctx.atr
        an = candle_anatomy(df).iloc[-1]
        bar = df.iloc[-1]
        win = df.iloc[-3:]
        if an["range"] < 0.6 * a:
            return None
        levels = [lv for lv in ctx.levels if lv.kind in LEVEL_KINDS and (not lv.kind.startswith("ZONE") or lv.strength >= 50)]
        for lv in sorted(levels, key=lambda x: -x.strength):
            L = lv.price
            # failed upside break -> bearish
            if win["high"].max() >= L + 0.15 * a and bar["close"] <= L - 0.1 * a and an["close_loc"] <= 0.4 and bar["open"] >= L - 0.5 * a:
                entry = float(bar["close"])
                stop = float(win["high"].max() + 0.15 * a)
                risk = stop - entry
                if risk < 0.3 * a:
                    continue
                t1, t2 = self.targets_from_levels(ctx, -1, entry, risk)
                rr = (entry - t1) / risk
                if rr < self.min_reward_risk:
                    continue
                ev = [f"swept {lv.kind} {L:.0f} to {win['high'].max():.0f} and closed back below", f"close_loc {an['close_loc']:.2f}, range {an['range']/a:.1f} ATR"]
                con = ["regime BULLISH: fading a breakout against trend"] if ctx.regime.direction == "BULLISH" else []
                pa = 50 + min(lv.strength / 4, 20) + (10 if ctx.regime.behaviour == "MEAN_REVERTING" else 0) + (10 if an["pin_bear"] or an["bear_engulf"] else 0)
                return SetupCandidate(strategy_id=self.id, direction="BEARISH", timeframe=ctx.setup_tf, trigger_event="FAILED_BREAK_UP",
                                      entry=entry, stop=stop, target1=t1, target2=t2, invalidation=f"close above {stop:.0f}",
                                      pa_score=float(min(pa, 100)), evidence=ev, contradictions=con, bar_ts=bar["ts"].to_pydatetime())
            # failed downside break -> bullish
            if win["low"].min() <= L - 0.15 * a and bar["close"] >= L + 0.1 * a and an["close_loc"] >= 0.6 and bar["open"] <= L + 0.5 * a:
                entry = float(bar["close"])
                stop = float(win["low"].min() - 0.15 * a)
                risk = entry - stop
                if risk < 0.3 * a:
                    continue
                t1, t2 = self.targets_from_levels(ctx, 1, entry, risk)
                rr = (t1 - entry) / risk
                if rr < self.min_reward_risk:
                    continue
                ev = [f"swept {lv.kind} {L:.0f} to {win['low'].min():.0f} and closed back above", f"close_loc {an['close_loc']:.2f}, range {an['range']/a:.1f} ATR"]
                con = ["regime BEARISH: fading a breakdown against trend"] if ctx.regime.direction == "BEARISH" else []
                pa = 50 + min(lv.strength / 4, 20) + (10 if ctx.regime.behaviour == "MEAN_REVERTING" else 0) + (10 if an["pin_bull"] or an["bull_engulf"] else 0)
                return SetupCandidate(strategy_id=self.id, direction="BULLISH", timeframe=ctx.setup_tf, trigger_event="FAILED_BREAK_DOWN",
                                      entry=entry, stop=stop, target1=t1, target2=t2, invalidation=f"close below {stop:.0f}",
                                      pa_score=float(min(pa, 100)), evidence=ev, contradictions=con, bar_ts=bar["ts"].to_pydatetime())
        return None
