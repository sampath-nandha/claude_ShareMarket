"""PA11 - Opening Drive Continuation.

Decision bars: exactly at 09:30 (15-minute window) or 09:45 (30-minute window, only if the 15-minute window was
not already a drive). The window must classify as OPEN_DRIVE or OPEN_TEST_DRIVE (see analytics.opening).
Entry = close of the decision bar, in the drive direction. Stop = the window's opposite extreme -/+ 0.1 ATR
(the drive's origin; a return there means the drive failed). Skipped when the stop is wider than 2.5 ATR or
the context timeframe is a STRONG trend against the drive. Targets from levels / R multiples, R:R >= 1.5.
Evidence: gap type (drive with the gap scores higher), CPR width (NARROW is the classic trend-day hint).
"""

from __future__ import annotations

from datetime import timedelta

from mmm.analytics.opening import opening_type
from mmm.signals.model import SetupCandidate
from mmm.strategies.base import Strategy, StrategyContext
from mmm.timeutil import session_open

DRIVES = ("OPEN_DRIVE", "OPEN_TEST_DRIVE")


class OpeningDrive(Strategy):
    id = "PA11"
    name = "Opening Drive Continuation"
    version = "0.1.0"

    def evaluate(self, ctx: StrategyContext) -> SetupCandidate | None:
        start = session_open(ctx.today)
        window = next((w for w in (15, 30) if ctx.as_of == start + timedelta(minutes=w)), None)
        if window is None or ctx.intraday_today.empty:
            return None
        ot = opening_type(ctx.intraday_today, window, ctx.as_of)
        if window == 30 and opening_type(ctx.intraday_today, 15, ctx.as_of).code in DRIVES:
            return None
        if ot.code not in DRIVES or ot.direction == 0:
            return None
        d, a = ot.direction, ctx.atr
        cs = ctx.context_structure.trend
        if ctx.regime.strength == "STRONG" and ((d > 0 and ctx.regime.direction == "BEARISH") or (d < 0 and ctx.regime.direction == "BULLISH")):
            return None
        entry = ctx.close
        stop = (ot.low - 0.1 * a) if d > 0 else (ot.high + 0.1 * a)
        risk = abs(entry - stop)
        if risk <= 0 or risk > 2.5 * a:
            return None
        t1, t2 = self.targets_from_levels(ctx, d, entry, risk)
        rr = abs(t1 - entry) / risk
        if rr < self.min_reward_risk:
            return None
        ev = [f"{ot.code.replace('_', ' ').lower()} ({window}m): " + "; ".join(ot.notes)]
        con: list[str] = []
        gap = ctx.extras.get("gap")
        pa = 50 + (8 if ot.code == "OPEN_DRIVE" else 4)
        if gap is not None and gap.direction != 0:
            if gap.direction == d:
                ev.append(f"drive with the {gap.kind.lower()} ({gap.points:+.0f})")
                pa += 8
            else:
                con.append(f"drive against the {gap.kind.lower()} ({gap.points:+.0f})")
                pa -= 6
        c = ctx.extras.get("cpr")
        if c is not None and c.width_class == "NARROW":
            ev.append("narrow CPR (trend-day hint)")
            pa += 5
        elif c is not None and c.width_class == "WIDE":
            con.append("wide CPR (range-day hint)")
            pa -= 5
        if (d > 0 and cs.startswith("BEAR")) or (d < 0 and cs.startswith("BULL")):
            con.append(f"context timeframe {cs}")
            pa -= 5
        pa += min(10, (rr - 1.5) * 8)
        bar = ctx.last_bar
        return SetupCandidate(strategy_id=self.id, direction="BULLISH" if d > 0 else "BEARISH", timeframe=ctx.setup_tf,
                              trigger_event=ot.code, entry=entry, stop=float(stop), target1=t1, target2=t2,
                              invalidation=f"trade back through {stop:,.0f} (drive origin)", pa_score=float(max(0, min(pa, 100))),
                              evidence=ev, contradictions=con, expires_after_bars=3, bar_ts=bar["ts"].to_pydatetime())
