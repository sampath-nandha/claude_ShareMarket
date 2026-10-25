"""PA01 - Trend Pullback.

Regimes: BULLISH or BEARISH (not SIDEWAYS). Context TF structure must agree with trend TF direction.
Setup: in an up-trend (setup TF last labels HH/HL, context TF bullish), price pulls back to within
0.5 ATR of a support zone / the last higher low / the 20-EMA, prints a rejection or bullish close
(close in the upper 60% of the bar, or bull engulf / pin) and does not break the last HL.
Trigger: the decision bar's close. Entry = close. Stop = min(pullback low, last HL) - 0.15 ATR.
Targets: from levels (>= 1R) else 1.5R / 2.5R. Expires after 6 setup bars.
Mirror for down-trends.
"""

from __future__ import annotations

from mmm.analytics.structure import candle_anatomy, trend_direction
from mmm.signals.model import SetupCandidate
from mmm.strategies.base import Strategy, StrategyContext


class TrendPullback(Strategy):
    id = "PA01"
    name = "Trend Pullback"
    version = "0.1.0"
    eligible_regimes = ("BULLISH", "BEARISH")

    def evaluate(self, ctx: StrategyContext) -> SetupCandidate | None:
        if not self.regime_ok(ctx.regime) or len(ctx.setup) < 30:
            return None
        d = trend_direction(ctx.context_structure.trend)
        if d == 0 or trend_direction(ctx.setup_structure.trend) not in (0, d):
            return None
        if (ctx.regime.direction == "BULLISH") != (d > 0):
            return None
        df = ctx.setup
        bar = df.iloc[-1]
        an = candle_anatomy(df).iloc[-1]
        ema20 = df["close"].ewm(span=20, adjust=False).mean().iloc[-1]
        a = ctx.atr
        evidence, contra = [], []
        if d > 0:
            hl = ctx.setup_structure.last_low
            if hl is None or bar["low"] < hl.price - 0.1 * a:
                return None
            sup = ctx.nearest_below(bar["low"] + 0.5 * a, kinds=("ZONE_S", "PDL", "PDC", "ORL", "SL", "PWL"))
            near_zone = sup is not None and abs(bar["low"] - sup.price) <= 0.5 * a
            near_ema = abs(bar["low"] - ema20) <= 0.5 * a
            if not (near_zone or near_ema):
                return None
            pulled_back = df["high"].iloc[-8:-1].max() - bar["low"] >= 0.8 * a
            if not pulled_back:
                return None
            rejection = an["close_loc"] >= 0.6 or an["bull_engulf"] or an["pin_bull"]
            if not rejection:
                return None
            entry = float(bar["close"])
            stop = float(min(bar["low"], hl.price) - 0.15 * a)
            evidence += [f"context {ctx.context_structure.trend}, setup {ctx.setup_structure.trend}"]
            evidence.append(f"pullback to {'zone ' + f'{sup.price:.0f}' if near_zone else '20-EMA'} with close_loc {an['close_loc']:.2f}")
            if an["bull_engulf"]:
                evidence.append("bullish engulfing")
            if an["pin_bull"]:
                evidence.append("bullish pin bar")
            inval = f"5m close below {stop:.0f} (last HL {hl.price:.0f})"
        else:
            lh = ctx.setup_structure.last_high
            if lh is None or bar["high"] > lh.price + 0.1 * a:
                return None
            res = ctx.nearest_above(bar["high"] - 0.5 * a, kinds=("ZONE_R", "PDH", "PDC", "ORH", "SH", "PWH"))
            near_zone = res is not None and abs(bar["high"] - res.price) <= 0.5 * a
            near_ema = abs(bar["high"] - ema20) <= 0.5 * a
            if not (near_zone or near_ema):
                return None
            pulled_back = bar["high"] - df["low"].iloc[-8:-1].min() >= 0.8 * a
            if not pulled_back:
                return None
            rejection = an["close_loc"] <= 0.4 or an["bear_engulf"] or an["pin_bear"]
            if not rejection:
                return None
            entry = float(bar["close"])
            stop = float(max(bar["high"], lh.price) + 0.15 * a)
            evidence += [f"context {ctx.context_structure.trend}, setup {ctx.setup_structure.trend}"]
            evidence.append(f"pullback to {'zone ' + f'{res.price:.0f}' if near_zone else '20-EMA'} with close_loc {an['close_loc']:.2f}")
            inval = f"5m close above {stop:.0f} (last LH {lh.price:.0f})"
        risk = abs(entry - stop)
        if risk < 0.3 * a:
            return None
        t1, t2 = self.targets_from_levels(ctx, d, entry, risk)
        rr = abs(t1 - entry) / risk
        if rr < self.min_reward_risk:
            contra.append(f"reward/risk {rr:.2f} below {self.min_reward_risk}")
            return None
        if ctx.regime.strength == "WEAK":
            contra.append("regime strength WEAK")
        pa = 55 + (15 if ctx.regime.strength == "STRONG" else 5) + (10 if an["bull_engulf"] or an["bear_engulf"] or an["pin_bull"] or an["pin_bear"] else 0) + min(10, (rr - 1.5) * 10)
        return SetupCandidate(
            strategy_id=self.id, direction="BULLISH" if d > 0 else "BEARISH", timeframe=ctx.setup_tf,
            trigger_event="PULLBACK_REJECTION", entry=entry, stop=stop, target1=t1, target2=t2, invalidation=inval,
            pa_score=float(min(pa, 100)), evidence=evidence, contradictions=contra, bar_ts=bar["ts"].to_pydatetime(),
        )
