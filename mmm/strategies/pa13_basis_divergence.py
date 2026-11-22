"""PA13 - Basis Divergence Reversal (spot-futures parity).

Needs futures parity history (ctx.extras["basis"]); silent without it. Window: 09:45-14:30 IST.
Bearish: the decision bar's high is within 0.25 ATR of today's session high, the 30-minute premium-over-fair-value
divergence is BEARISH (spot at its high while the futures premium contracted, see analytics.basis) and the bar
rejects (close_loc <= 0.45). Bullish mirrored at the session low. Entry = close. Stop = session extreme +/- 0.2 ATR.
Targets from levels / R multiples, R:R >= 1.5. Futures OI rising with the divergence adds to the score.
"""

from __future__ import annotations

from datetime import time

from mmm.signals.model import SetupCandidate
from mmm.strategies.base import Strategy, StrategyContext


class BasisDivergence(Strategy):
    id = "PA13"
    name = "Basis Divergence Reversal"
    version = "0.1.0"

    def evaluate(self, ctx: StrategyContext) -> SetupCandidate | None:
        if not (time(9, 45) <= ctx.as_of.time() <= time(14, 30)) or ctx.intraday_today.empty:
            return None
        st = ctx.extras.get("basis")
        if st is None or st.divergence is None:
            return None
        dv = st.divergence
        d = -1 if dv.kind == "BEARISH" else 1
        a = ctx.atr
        bar, an = ctx.last_bar, ctx.anatomy.iloc[-1]
        s_hi, s_lo = float(ctx.intraday_today["high"].max()), float(ctx.intraday_today["low"].min())
        if d < 0 and not (s_hi - float(bar["high"]) <= 0.25 * a and an["close_loc"] <= 0.45):
            return None
        if d > 0 and not (float(bar["low"]) - s_lo <= 0.25 * a and an["close_loc"] >= 0.55):
            return None
        entry = ctx.close
        stop = s_hi + 0.2 * a if d < 0 else s_lo - 0.2 * a
        risk = abs(entry - stop)
        if risk <= 0 or risk > 3 * a:
            return None
        t1, t2 = self.targets_from_levels(ctx, d, entry, risk)
        rr = abs(t1 - entry) / risk
        if rr < self.min_reward_risk:
            return None
        ev = list(dv.evidence) + [f"premium {st.premium:+.1f} pts vs fair ({st.premium_bps:+.1f} bps)" + (f", z {st.z:+.2f}" if st.z is not None else "")]
        con: list[str] = []
        if ctx.regime.strength == "STRONG" and ((d < 0 and ctx.regime.direction == "BULLISH") or (d > 0 and ctx.regime.direction == "BEARISH")):
            con.append(f"counter-trend against a strong {ctx.regime.direction.lower()} regime")
        pa = 48 + 22 * dv.strength + min(10, (rr - 1.5) * 8) - (8 if con else 0)
        return SetupCandidate(strategy_id=self.id, direction="BULLISH" if d > 0 else "BEARISH", timeframe=ctx.setup_tf,
                              trigger_event=f"BASIS_DIVERGENCE_{dv.kind}", entry=entry, stop=float(stop), target1=t1, target2=t2,
                              invalidation=f"new session {'high' if d < 0 else 'low'} beyond {stop:,.0f}", pa_score=float(max(0, min(pa, 100))),
                              evidence=ev, contradictions=con, expires_after_bars=3, bar_ts=bar["ts"].to_pydatetime())
