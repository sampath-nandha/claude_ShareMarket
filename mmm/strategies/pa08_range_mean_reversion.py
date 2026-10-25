"""PA08 - Range Boundary Mean Reversion.

Regime gate: direction SIDEWAYS and behaviour MEAN_REVERTING (or RANGE structure on both setup and context TF).
Range = box of >= 12 completed bars (width 1.0-3.5 ATR) OR the pair (strongest ZONE_R, strongest ZONE_S) within
4 ATR of price with both strength >= 55. The decision bar touches the range edge (within 0.2 ATR), stays inside
(no close beyond the edge by more than 0.1 ATR) and rejects (pin/engulf or close_loc on the far 40%).
Entry = close. Stop = edge +/- 0.5 ATR. T1 = range mid, T2 = opposite edge - 0.3 ATR. Requires R:R >= 1.5 to T1.
Opposite of PA02/PA07 at the same level - the regime gate decides which is eligible.
"""

from __future__ import annotations

from mmm.analytics.consolidation import detect_box
from mmm.signals.model import SetupCandidate
from mmm.strategies.base import Strategy, StrategyContext


class RangeMeanReversion(Strategy):
    id = "PA08"
    name = "Range Boundary Mean Reversion"
    version = "0.1.0"
    eligible_regimes = ("SIDEWAYS",)

    def _range(self, ctx: StrategyContext) -> tuple[float, float] | None:
        box = detect_box(ctx.setup, min_bars=12, max_bars=80, max_width_atr=3.5)
        if box is not None and box.width_atr >= 1.0:
            return box.high, box.low
        res = [lv for lv in ctx.levels if lv.kind == "ZONE_R" and lv.strength >= 55 and lv.price - ctx.close <= 4 * ctx.atr]
        sup = [lv for lv in ctx.levels if lv.kind == "ZONE_S" and lv.strength >= 55 and ctx.close - lv.price <= 4 * ctx.atr]
        if res and sup:
            r, s = max(res, key=lambda x: x.strength), max(sup, key=lambda x: x.strength)
            if r.price - s.price >= 1.0 * ctx.atr:
                return r.price, s.price
        return None

    def evaluate(self, ctx: StrategyContext) -> SetupCandidate | None:
        if len(ctx.setup) < 40:
            return None
        range_ok = self.regime_ok(ctx.regime) and ctx.regime.behaviour == "MEAN_REVERTING"
        range_ok = range_ok or (ctx.setup_structure.trend == "RANGE" and ctx.context_structure.trend in ("RANGE", "UNCERTAIN"))
        if not range_ok:
            return None
        rng = self._range(ctx)
        if rng is None:
            return None
        hi, lo = rng
        a = ctx.atr
        df = ctx.setup
        bar = df.iloc[-1]
        an = ctx.anatomy.iloc[-1]
        mid = (hi + lo) / 2
        if abs(bar["high"] - hi) <= 0.2 * a and bar["close"] <= hi + 0.1 * a and (an["pin_bear"] or an["bear_engulf"] or an["close_loc"] <= 0.4):
            d, edge = -1, hi
        elif abs(bar["low"] - lo) <= 0.2 * a and bar["close"] >= lo - 0.1 * a and (an["pin_bull"] or an["bull_engulf"] or an["close_loc"] >= 0.6):
            d, edge = 1, lo
        else:
            return None
        entry = float(bar["close"])
        stop = float(edge - d * 0.5 * a)
        risk = abs(entry - stop)
        if risk < 0.3 * a:
            return None
        t1 = float(mid)
        t2 = float((lo if d < 0 else hi) + d * (-0.3 * a))
        rr = abs(t1 - entry) / risk
        if rr < self.min_reward_risk:
            return None
        ev = [f"range {lo:.0f}-{hi:.0f} ({(hi-lo)/a:.1f} ATR), regime {ctx.regime.code}", f"rejection at {'top' if d < 0 else 'bottom'}, close_loc {an['close_loc']:.2f}"]
        con = ["compression/expansion regime: range may be ending"] if ctx.regime.behaviour in ("COMPRESSING", "EXPANDING") else []
        pa = 50 + (15 if ctx.regime.behaviour == "MEAN_REVERTING" else 5) + (10 if an["pin_bear"] or an["pin_bull"] or an["bear_engulf"] or an["bull_engulf"] else 0) + min(10, (rr - 1.5) * 8)
        return SetupCandidate(strategy_id=self.id, direction="BULLISH" if d > 0 else "BEARISH", timeframe=ctx.setup_tf,
                              trigger_event=f"RANGE_{'BOTTOM' if d > 0 else 'TOP'}_FADE", entry=entry, stop=stop, target1=t1, target2=t2,
                              invalidation=f"close beyond range edge at {stop:.0f}", pa_score=float(min(pa, 100)), evidence=ev, contradictions=con,
                              bar_ts=bar["ts"].to_pydatetime())
