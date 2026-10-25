"""PA07 - Consolidation Breakout.

Box: >= 8 completed setup-TF bars with total range <= 1.6 ATR (see analytics.consolidation). The decision bar
closes outside the box by >= 0.2 ATR with body_ratio >= 0.55 and range >= 0.8 ATR. Compression ratio
(ATR5/ATR20) at the box end must be < 0.9 (energy stored). Higher-TF regime must not oppose strongly.
Entry = close. Stop = box mid (or box edge if that is >= 0.5 ATR away). Targets: measured move (box height)
added to the breakout edge is T1 unless a level is closer; T2 from levels / 2.5R.
"""

from __future__ import annotations

from mmm.analytics.consolidation import detect_box
from mmm.analytics.volatility import compression
from mmm.signals.model import SetupCandidate
from mmm.strategies.base import Strategy, StrategyContext


class ConsolidationBreakout(Strategy):
    id = "PA07"
    name = "Consolidation Breakout"
    version = "0.1.0"

    def evaluate(self, ctx: StrategyContext) -> SetupCandidate | None:
        df = ctx.setup
        if len(df) < 40:
            return None
        box = detect_box(df)
        if box is None:
            return None
        a = ctx.atr
        an = ctx.anatomy.iloc[-1]
        bar = df.iloc[-1]
        comp = compression(df.iloc[: box.end_idx + 1]).dropna()
        c = float(comp.iloc[-1]) if len(comp) else 1.0
        if c >= 0.9 or an["body_ratio"] < 0.55 or an["range"] < 0.8 * a:
            return None
        height = box.high - box.low
        if bar["close"] >= box.high + 0.2 * a:
            d, edge = 1, box.high
            if ctx.regime.direction == "BEARISH" and ctx.regime.strength == "STRONG":
                return None
        elif bar["close"] <= box.low - 0.2 * a:
            d, edge = -1, box.low
            if ctx.regime.direction == "BULLISH" and ctx.regime.strength == "STRONG":
                return None
        else:
            return None
        entry = float(bar["close"])
        mid_stop = box.mid
        stop = float(mid_stop) if abs(entry - mid_stop) >= 0.5 * a else float(edge - d * 0.5 * a)
        risk = abs(entry - stop)
        if risk < 0.3 * a:
            return None
        lt1, lt2 = self.targets_from_levels(ctx, d, entry, risk)
        measured = edge + d * height
        t1 = measured if (d * (measured - entry) >= risk and d * (measured - entry) < d * (lt1 - entry)) else lt1
        t2 = lt2 if d * (lt2 - t1) > 0 else t1 + d * risk
        rr = abs(t1 - entry) / risk
        if rr < self.min_reward_risk:
            return None
        ev = [f"box {box.low:.0f}-{box.high:.0f} over {box.bars} bars ({box.width_atr:.1f} ATR), compression {c:.2f}",
              f"breakout bar body {an['body_ratio']:.2f}, range {an['range']/a:.1f} ATR"]
        con = []
        if (d > 0 and ctx.regime.direction == "BEARISH") or (d < 0 and ctx.regime.direction == "BULLISH"):
            con.append(f"regime {ctx.regime.direction} opposes breakout direction")
        pa = 50 + 15 * (1 - c) * 2 + 10 * an["body_ratio"] + (10 if ctx.regime.behaviour == "COMPRESSING" else 0) + min(10, (rr - 1.5) * 8)
        return SetupCandidate(strategy_id=self.id, direction="BULLISH" if d > 0 else "BEARISH", timeframe=ctx.setup_tf,
                              trigger_event=f"BOX_BREAK_{'UP' if d > 0 else 'DOWN'}", entry=entry, stop=stop, target1=float(t1), target2=float(t2),
                              invalidation=f"close back inside box beyond {stop:.0f}", pa_score=float(min(pa, 100)), evidence=ev, contradictions=con,
                              bar_ts=bar["ts"].to_pydatetime())
