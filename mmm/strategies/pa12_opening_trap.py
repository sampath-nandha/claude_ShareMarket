"""PA12 - Opening Trap (gap fails to hold the opening range).

Gap day only: today's open is not FLAT versus the previous close and the gap is at least 0.3 daily ATR.
After the 15-minute opening range (OR15) completes and before 11:30, the first setup-TF bar that closes beyond
the OR on the side AGAINST the gap by >= 0.1 ATR triggers a trade against the gap (gap-up -> close below ORL ->
BEARISH). No earlier setup bar today may have closed beyond that side, and the gap must still be unfilled.
Entry = close. Stop = OR midpoint +/- 0.2 ATR (back inside the upper/lower half = trap thesis wrong).
T1 = the previous close (gap fill) when it is at least 1.5R away, else levels / R multiples.
"""

from __future__ import annotations

from datetime import time, timedelta

from mmm.signals.model import SetupCandidate
from mmm.strategies.base import Strategy, StrategyContext
from mmm.timeutil import session_open


class OpeningTrap(Strategy):
    id = "PA12"
    name = "Opening Trap"
    version = "0.1.0"
    min_gap_atr = 0.3

    def evaluate(self, ctx: StrategyContext) -> SetupCandidate | None:
        gap = ctx.extras.get("gap")
        if gap is None or gap.direction == 0 or gap.atr_multiple is None or abs(gap.atr_multiple) < self.min_gap_atr:
            return None
        start = session_open(ctx.today)
        or_end = start + timedelta(minutes=15)
        if ctx.as_of <= or_end or ctx.as_of.time() > time(11, 30):
            return None
        it = ctx.intraday_today
        orng = it[it["ts"] < or_end]
        if len(orng) < 10:
            return None
        orh, orl = float(orng["high"].max()), float(orng["low"].min())
        mid = (orh + orl) / 2
        a = ctx.atr
        d = -gap.direction
        level = orl if d < 0 else orh
        bar = ctx.last_bar
        close = float(bar["close"])
        beyond = (close <= level - 0.1 * a) if d < 0 else (close >= level + 0.1 * a)
        if not beyond:
            return None
        today_setup = ctx.setup[(ctx.setup["ts"].dt.date == ctx.today) & (ctx.setup["ts"] >= or_end)]
        earlier = today_setup.iloc[:-1]
        if not earlier.empty and (((earlier["close"] < level) if d < 0 else (earlier["close"] > level)).any()):
            return None
        if (d < 0 and close <= gap.prev_close) or (d > 0 and close >= gap.prev_close):
            return None                                   # gap already filled: nothing left to trap
        entry = close
        stop = mid + 0.2 * a if d < 0 else mid - 0.2 * a
        risk = abs(entry - stop)
        if risk <= 0:
            return None
        if abs(gap.prev_close - entry) >= 1.5 * risk:
            t1 = float(gap.prev_close)
            _, t2 = self.targets_from_levels(ctx, d, entry, abs(t1 - entry))
            if abs(t2 - entry) <= abs(t1 - entry):
                t2 = entry + d * (abs(t1 - entry) + risk)
        else:
            t1, t2 = self.targets_from_levels(ctx, d, entry, risk)
        rr = abs(t1 - entry) / risk
        if rr < self.min_reward_risk:
            return None
        ev = [f"{gap.kind.lower()} {gap.points:+.0f} ({gap.atr_multiple:+.2f} daily ATR) failed to hold OR15 {orl:,.0f}-{orh:,.0f}",
              f"first close beyond OR{'L' if d < 0 else 'H'}", f"gap-fill target {gap.prev_close:,.0f}"]
        con: list[str] = []
        pa = 52 + min(12, abs(gap.atr_multiple) * 10)
        ot = ctx.extras.get("open15")
        if ot is not None and ot.code == "OPEN_DRIVE" and ot.direction == gap.direction:
            con.append("the open drove with the gap")
            pa -= 8
        if ot is not None and ot.code == "OPEN_REJECTION_REVERSE":
            ev.append("open rejection-reverse")
            pa += 6
        if ctx.regime.strength == "STRONG" and ((d > 0 and ctx.regime.direction == "BEARISH") or (d < 0 and ctx.regime.direction == "BULLISH")):
            con.append(f"strong {ctx.regime.direction.lower()} regime against the trade")
            pa -= 8
        pa += min(10, (rr - 1.5) * 6)
        return SetupCandidate(strategy_id=self.id, direction="BULLISH" if d > 0 else "BEARISH", timeframe=ctx.setup_tf,
                              trigger_event="OPENING_TRAP", entry=entry, stop=float(stop), target1=float(t1), target2=float(t2),
                              invalidation=f"close back beyond OR mid {mid:,.0f}", pa_score=float(max(0, min(pa, 100))),
                              evidence=ev, contradictions=con, expires_after_bars=3, bar_ts=bar["ts"].to_pydatetime())
