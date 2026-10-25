"""PA09 - Trend Continuation After Compression.

Regime: BULLISH or BEARISH with context TF trend agreeing. Compression: the last 4-10 completed setup bars
include >= 3 narrow bars (range <= 0.5 x 20-bar avg) or inside bars, compression ratio (ATR5/ATR20) < 0.8,
and price held on the trend side of the 20-EMA throughout (no close against it). The decision bar is a
momentum bar (body_ratio >= 0.6, range >= 1.0 ATR) closing beyond the compression cluster's extreme in the
trend direction. Entry = close. Stop = cluster's opposite extreme - 0.15 ATR. Targets from levels / R multiples.
Differs from PA01 (needs a pullback to a level) and PA07 (needs a box; direction-agnostic).
"""

from __future__ import annotations

from mmm.analytics.structure import candle_anatomy, trend_direction
from mmm.analytics.volatility import compression
from mmm.signals.model import SetupCandidate
from mmm.strategies.base import Strategy, StrategyContext


class CompressionContinuation(Strategy):
    id = "PA09"
    name = "Trend Continuation After Compression"
    version = "0.1.0"
    eligible_regimes = ("BULLISH", "BEARISH")

    def evaluate(self, ctx: StrategyContext) -> SetupCandidate | None:
        if not self.regime_ok(ctx.regime) or len(ctx.setup) < 40:
            return None
        d = 1 if ctx.regime.direction == "BULLISH" else -1
        if trend_direction(ctx.context_structure.trend) != d:
            return None
        df = ctx.setup
        a = ctx.atr
        ana = candle_anatomy(df)
        an, bar = ana.iloc[-1], df.iloc[-1]
        if an["body_ratio"] < 0.6 or an["range"] < 1.0 * a or (d > 0) != bool(an["bull"]):
            return None
        ema20 = df["close"].ewm(span=20, adjust=False).mean()
        for n in range(4, 11):
            cl = df.iloc[-1 - n : -1]
            cl_an = ana.iloc[-1 - n : -1]
            quiet = int((cl_an["narrow"] | cl_an["inside"]).sum())
            if quiet < 3:
                continue
            comp = compression(df.iloc[:-1]).dropna()
            if comp.empty or float(comp.iloc[-1]) >= 0.8:
                return None
            held = (cl["close"] >= ema20.iloc[-1 - n : -1]).all() if d > 0 else (cl["close"] <= ema20.iloc[-1 - n : -1]).all()
            if not held:
                continue
            ext_hi, ext_lo = float(cl["high"].max()), float(cl["low"].min())
            if (d > 0 and bar["close"] <= ext_hi) or (d < 0 and bar["close"] >= ext_lo):
                return None
            entry = float(bar["close"])
            stop = float(ext_lo - 0.15 * a) if d > 0 else float(ext_hi + 0.15 * a)
            risk = abs(entry - stop)
            if risk < 0.3 * a:
                return None
            t1, t2 = self.targets_from_levels(ctx, d, entry, risk)
            rr = abs(t1 - entry) / risk
            if rr < self.min_reward_risk:
                return None
            ev = [f"{quiet}/{n} quiet bars, compression {float(comp.iloc[-1]):.2f}, held {'above' if d > 0 else 'below'} 20-EMA",
                  f"momentum bar body {an['body_ratio']:.2f}, range {an['range']/a:.1f} ATR beyond cluster {'high' if d > 0 else 'low'}"]
            con = ["regime strength WEAK"] if ctx.regime.strength == "WEAK" else []
            pa = 55 + (15 if ctx.regime.strength == "STRONG" else 5) + 10 * an["body_ratio"] + min(10, (rr - 1.5) * 8)
            return SetupCandidate(strategy_id=self.id, direction="BULLISH" if d > 0 else "BEARISH", timeframe=ctx.setup_tf,
                                  trigger_event="COMPRESSION_CONTINUATION", entry=entry, stop=stop, target1=t1, target2=t2,
                                  invalidation=f"close back through cluster at {stop:.0f}", pa_score=float(min(pa, 100)), evidence=ev,
                                  contradictions=con, expires_after_bars=4, bar_ts=bar["ts"].to_pydatetime())
        return None
