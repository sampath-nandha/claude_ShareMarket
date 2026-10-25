"""PA04 - Opening Range Breakout.

Window: configurable (default 15 minutes; 5/30 evaluated separately in research). Valid only between
OR completion and 12:00 IST. Trigger: the first setup-TF bar that closes beyond ORH (ORL) by >= 0.15 ATR with
body_ratio >= 0.5, where the OR width is between 0.4 and 1.6 x the 20-day average daily range * 0.25
(too narrow = noise, too wide = exhausted). Gap context recorded as evidence/contradiction.
Entry = close. Stop = opposite side of the OR mid (ORH+ORL)/2 - 0.1 ATR, or OR edge if that is tighter than 0.5 ATR.
"""

from __future__ import annotations

from datetime import timedelta

from mmm.analytics.structure import candle_anatomy
from mmm.signals.model import SetupCandidate
from mmm.strategies.base import Strategy, StrategyContext
from mmm.timeutil import session_open


class OpeningRangeBreakout(Strategy):
    id = "PA04"
    name = "Opening Range Breakout"
    version = "0.1.0"

    def __init__(self, or_minutes: int = 15):
        self.or_minutes = or_minutes

    def evaluate(self, ctx: StrategyContext) -> SetupCandidate | None:
        if ctx.intraday_today.empty or len(ctx.setup) < 25:
            return None
        o = session_open(ctx.today)
        if ctx.as_of < o + timedelta(minutes=self.or_minutes) or ctx.as_of.time().hour >= 12:
            return None
        orng = ctx.intraday_today[ctx.intraday_today["ts"] < o + timedelta(minutes=self.or_minutes)]
        if orng.empty:
            return None
        orh, orl = float(orng["high"].max()), float(orng["low"].min())
        width = orh - orl
        a = ctx.atr
        daily = ctx.daily[ctx.daily["ts"].dt.date < ctx.today]
        if len(daily) < 20:
            return None
        adr = float((daily["high"] - daily["low"]).tail(20).mean())
        if not (0.4 * 0.25 * adr <= width <= 1.6 * 0.25 * adr):
            return None
        df = ctx.setup
        bar = df.iloc[-1]
        an = candle_anatomy(df).iloc[-1]
        prev_bars = df[(df["ts"] >= o + timedelta(minutes=self.or_minutes)) & (df["ts"] < bar["ts"])]
        gap = float(ctx.intraday_today["open"].iloc[0] - daily["close"].iloc[-1])
        ev, con = [f"OR{self.or_minutes} {orl:.0f}-{orh:.0f} (width {width/adr*100:.0f}% of ADR)"], []
        if abs(gap) > 0.3 * adr:
            ev.append(f"gap {'up' if gap > 0 else 'down'} {gap:+.0f}")
        if bar["close"] >= orh + 0.15 * a and an["body_ratio"] >= 0.5 and (prev_bars.empty or (prev_bars["close"] < orh).all()):
            d = 1
            entry = float(bar["close"])
            mid_stop = (orh + orl) / 2 - 0.1 * a
            stop = float(max(mid_stop, orl - 0.1 * a)) if entry - mid_stop > 0.5 * a else float(orl - 0.1 * a)
            if gap < -0.3 * adr:
                con.append("gap-down day: upside ORB is counter-gap")
        elif bar["close"] <= orl - 0.15 * a and an["body_ratio"] >= 0.5 and (prev_bars.empty or (prev_bars["close"] > orl).all()):
            d = -1
            entry = float(bar["close"])
            mid_stop = (orh + orl) / 2 + 0.1 * a
            stop = float(min(mid_stop, orh + 0.1 * a)) if mid_stop - entry > 0.5 * a else float(orh + 0.1 * a)
            if gap > 0.3 * adr:
                con.append("gap-up day: downside ORB is counter-gap")
        else:
            return None
        risk = abs(entry - stop)
        if risk < 0.3 * a:
            return None
        t1, t2 = self.targets_from_levels(ctx, d, entry, risk)
        rr = abs(t1 - entry) / risk
        if rr < self.min_reward_risk:
            return None
        if (d > 0 and ctx.regime.direction == "BEARISH") or (d < 0 and ctx.regime.direction == "BULLISH"):
            con.append(f"higher-TF regime {ctx.regime.direction} opposes breakout")
        pa = 50 + 15 * an["body_ratio"] + (10 if ctx.regime.direction == ("BULLISH" if d > 0 else "BEARISH") else 0) + min(10, (rr - 1.5) * 8) + (10 if an["momentum"] else 0)
        return SetupCandidate(strategy_id=self.id, direction="BULLISH" if d > 0 else "BEARISH", timeframe=ctx.setup_tf,
                              trigger_event=f"ORB{self.or_minutes}_{'UP' if d > 0 else 'DOWN'}", entry=entry, stop=stop, target1=t1, target2=t2,
                              invalidation=f"close back inside OR beyond {stop:.0f}", pa_score=float(min(pa, 100)), evidence=ev,
                              contradictions=con, expires_after_bars=4, bar_ts=bar["ts"].to_pydatetime())
