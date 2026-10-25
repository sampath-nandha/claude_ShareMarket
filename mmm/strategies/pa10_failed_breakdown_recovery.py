"""PA10 - Failed Breakdown Recovery (bullish only).

Sequence over the last 2-10 completed setup bars: (1) a close below a support level L (PDL, PWL, ORL, SL or
ZONE_S >= 50) by >= 0.15 ATR - a real breakdown, not just a wick; (2) within the following bars price reclaims
L and the decision bar closes above both L + 0.1 ATR and the 20-EMA with close_loc >= 0.6; (3) the breakdown
low is not violated after the reclaim. Entry = close. Stop = breakdown low - 0.2 ATR. Targets from levels / R.
Differs from PA03 (wick sweep, immediate) and PA06 (requires a CHoCH); this is a *closed* breakdown that
fails, which historically carries more trapped participants - treated as a hypothesis to validate.
"""

from __future__ import annotations

from mmm.signals.model import SetupCandidate
from mmm.strategies.base import Strategy, StrategyContext

SUPPORT_KINDS = ("PDL", "PWL", "ORL", "SL", "ZONE_S", "PDC")


class FailedBreakdownRecovery(Strategy):
    id = "PA10"
    name = "Failed Breakdown Recovery"
    version = "0.1.0"

    def evaluate(self, ctx: StrategyContext) -> SetupCandidate | None:
        df = ctx.setup
        if len(df) < 40 or (ctx.regime.direction == "BEARISH" and ctx.regime.strength == "STRONG"):
            return None
        a = ctx.atr
        an = ctx.anatomy.iloc[-1]
        bar = df.iloc[-1]
        ema20 = float(df["close"].ewm(span=20, adjust=False).mean().iloc[-1])
        if an["close_loc"] < 0.6 or bar["close"] <= ema20:
            return None
        levels = [lv for lv in ctx.levels if lv.kind in SUPPORT_KINDS and (not lv.kind.startswith("ZONE") or lv.strength >= 50) and lv.price < bar["close"]]
        for lv in sorted(levels, key=lambda x: -x.strength):
            L = lv.price
            if bar["close"] < L + 0.1 * a:
                continue
            window = df.iloc[-11:-1]
            broke = window[window["close"] <= L - 0.15 * a]
            if broke.empty:
                continue
            b_idx = broke.index[-1]
            after = df.loc[b_idx:].iloc[1:]
            bd_low = float(df.loc[b_idx:, "low"].min())
            # must have reclaimed (a close above L) before/at decision bar, and the breakdown low must hold afterwards
            reclaimed = (after["close"] > L).any()
            if not reclaimed:
                continue
            entry = float(bar["close"])
            stop = float(bd_low - 0.2 * a)
            risk = entry - stop
            if risk < 0.3 * a or risk > 3 * a:
                continue
            t1, t2 = self.targets_from_levels(ctx, 1, entry, risk)
            rr = (t1 - entry) / risk
            if rr < self.min_reward_risk:
                continue
            ev = [f"closed below {lv.kind} {L:.0f} (low {bd_low:.0f}) then reclaimed", f"decision bar above level and 20-EMA, close_loc {an['close_loc']:.2f}"]
            con = ["regime BEARISH: counter-trend recovery"] if ctx.regime.direction == "BEARISH" else []
            pa = 50 + min(lv.strength / 4, 20) + (10 if ctx.regime.direction == "BULLISH" else 0) + (10 if an["bull_engulf"] or an["momentum"] else 0) + min(10, (rr - 1.5) * 8)
            return SetupCandidate(strategy_id=self.id, direction="BULLISH", timeframe=ctx.setup_tf, trigger_event="FAILED_BREAKDOWN_RECOVERY",
                                  entry=entry, stop=stop, target1=t1, target2=t2, invalidation=f"close below {stop:.0f}", pa_score=float(min(pa, 100)),
                                  evidence=ev, contradictions=con, bar_ts=bar["ts"].to_pydatetime())
        return None
