"""PA14 - OI Wall: defended-wall rejection and unwinding-wall breakout.

Needs option OI flow (ctx.extras["oi"], from archived option minute candles); silent without it.
Window: 09:45-14:45 IST. Walls: call wall = most call OI at/above ATM, put wall = most put OI at/below ATM.

Rejection (fade): the wall is DEFENDED (its OI rose >= 3% in 30 min with price within 2 strikes). The decision
bar trades to within 0.3 ATR of the wall (or through it by <= 0.3 ATR) and closes back on the near side by
>= 0.05 ATR with a rejecting close (close_loc <= 0.45 at a call wall). Stop = beyond the bar extreme and the
wall by 0.25 ATR.
Breakout: the wall is UNWINDING (its OI fell >= 5% in 30 min). The decision bar closes beyond the wall by
>= 0.1 ATR after the previous bar closed on the near side. Stop = wall -/+ 0.3 ATR.
Targets from levels (the opposite wall and confluence clusters are levels) / R multiples, R:R >= 1.5.
"""

from __future__ import annotations

from datetime import time

from mmm.signals.model import SetupCandidate
from mmm.strategies.base import Strategy, StrategyContext


class OIWall(Strategy):
    id = "PA14"
    name = "OI Wall Rejection / Breakout"
    version = "0.1.0"

    def evaluate(self, ctx: StrategyContext) -> SetupCandidate | None:
        if not (time(9, 45) <= ctx.as_of.time() <= time(14, 45)) or len(ctx.setup) < 3:
            return None
        st = ctx.extras.get("oi")
        if st is None:
            return None
        a = ctx.atr
        bar, prev, an = ctx.last_bar, ctx.setup.iloc[-2], ctx.anatomy.iloc[-1]
        close = float(bar["close"])
        for wall, d_fade in ((st.call_wall, -1), (st.put_wall, 1)):
            if wall is None:
                continue
            w = wall.strike
            if wall.status == "DEFENDED":
                if d_fade < 0:
                    touched = float(bar["high"]) >= w - 0.3 * a and float(bar["high"]) <= w + 0.3 * a
                    ok = touched and close <= w - 0.05 * a and an["close_loc"] <= 0.45
                    stop = max(float(bar["high"]), w) + 0.25 * a
                else:
                    touched = float(bar["low"]) <= w + 0.3 * a and float(bar["low"]) >= w - 0.3 * a
                    ok = touched and close >= w + 0.05 * a and an["close_loc"] >= 0.55
                    stop = min(float(bar["low"]), w) - 0.25 * a
                if ok:
                    return self._cand(ctx, d_fade, close, stop, "OI_WALL_REJECTION", wall, st)
            if wall.status == "UNWINDING":
                d = -d_fade
                if d > 0 and close >= w + 0.1 * a and float(prev["close"]) < w:
                    return self._cand(ctx, d, close, w - 0.3 * a, "OI_WALL_BREAKOUT", wall, st)
                if d < 0 and close <= w - 0.1 * a and float(prev["close"]) > w:
                    return self._cand(ctx, d, close, w + 0.3 * a, "OI_WALL_BREAKOUT", wall, st)
        return None

    def _cand(self, ctx: StrategyContext, d: int, entry: float, stop: float, trigger: str, wall, st) -> SetupCandidate | None:
        risk = abs(entry - stop)
        if risk <= 0 or risk > 3 * ctx.atr:
            return None
        t1, t2 = self.targets_from_levels(ctx, d, entry, risk)
        rr = abs(t1 - entry) / risk
        if rr < self.min_reward_risk:
            return None
        chg = f"{wall.oi_chg_30_pct:+.1f}%" if wall.oi_chg_30_pct is not None else "n/a"
        ev = [f"{wall.side.lower()} wall {wall.strike:,.0f} {wall.status.lower()} (OI {wall.oi:,}, 30m change {chg}, {wall.strength:.1f}x median)",
              f"near-ATM PCR {st.pcr_near:.2f} ({st.pcr_trend.lower()})" if st.pcr_near is not None else "near-ATM PCR n/a",
              f"wall migration since open: {wall.migration.lower()}"]
        con: list[str] = []
        if st.strikes_present < 10:
            con.append(f"thin strike coverage: {st.coverage_note}")
        flow = st.confirmation(d)
        if flow < 0:
            con.append(f"broader option flow leans the other way ({flow:+.2f})")
        pa = 50 + min(15, (wall.strength - 1) * 5) + 10 * flow + min(10, (rr - 1.5) * 8) - (6 if con else 0)
        return SetupCandidate(strategy_id=self.id, direction="BULLISH" if d > 0 else "BEARISH", timeframe=ctx.setup_tf, trigger_event=trigger,
                              entry=entry, stop=float(stop), target1=t1, target2=t2, invalidation=f"close beyond {stop:,.0f}",
                              pa_score=float(max(0, min(pa, 100))), evidence=ev, contradictions=con, expires_after_bars=3,
                              bar_ts=ctx.last_bar["ts"].to_pydatetime())
