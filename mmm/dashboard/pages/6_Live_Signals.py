from __future__ import annotations

import pandas as pd
import streamlit as st

from mmm.analytics.breadth import compute_breadth
from mmm.analytics.futures import futures_snapshot
from mmm.analytics.options.chain import summarise_chain
from mmm.analytics.options.selector import best_option, rank_options
from mmm.dashboard.common import banner, candlestick, fmt, load_bundle, page_header, session_bars, state_db, trade_marker
from mmm.data.quality import DQ
from mmm.paper.journal import Journal
from mmm.settings import get_config
from mmm.signals.engine import Confirmations, build_context, stage1, stage2
from mmm.strategies import load_strategies

st.set_page_config(page_title="Live Signals", layout="wide")
page_header("Live Signals", "Stage 1 price action → Stage 2 confirmation → risk gate → option selection. NO TRADE is the default.")
b = load_bundle()
banner(b)
cfg = get_config()
ctx = build_context(b.minute_bars, b.as_of)
if ctx is None:
    st.error("Not enough history.")
    st.stop()


def confirmations_for(cand):
    d = 1 if cand.direction == "BULLISH" else -1
    fut = None
    if b.futures_quote:
        day = session_bars(b.minute_bars, b.as_of.date())
        fs = futures_snapshot(b.futures_quote, b.spot, int(b.futures_quote.get("oi", 0) * 0.98) if b.futures_quote.get("oi") else None, day,
                              b.futures_quote.get("volume", 0) / 1.1 if b.futures_quote.get("volume") else None, d)
        fut = {"confirmation_score": fs.confirmation_score, "basis": fs.basis, "oi_change_pct": fs.oi_change_pct, "rel_volume": fs.rel_volume,
               "price_oi_label": fs.price_oi_label, "notes": fs.notes}
    opts, rec, alts, liq, prem_risk = None, None, [], None, None
    if not b.chain.empty and b.expiries:
        e = b.expiries[0]
        summ = summarise_chain(b.chain, b.spot, e, 2 * cfg.universe.option_strikes_each_side + 1)
        # option confirmation: PCR tilt and OI-change tilt in the trade's favour (statistical feature, weak weight)
        score = 0.0
        if summ.pcr_oi is not None:
            score += 0.3 * d * (1 if summ.pcr_oi > 1.1 else -1 if summ.pcr_oi < 0.8 else 0)
        if summ.pcr_oi_change is not None and summ.pcr_oi_change > 0:
            score += 0.2 * d * (1 if summ.pcr_oi_change > 1 else -1)
        opts = {"confirmation_score": max(min(score, 1), -1), "pcr_oi": summ.pcr_oi, "max_pain": summ.max_pain, "call_wall": summ.call_wall,
                "put_wall": summ.put_wall, "atm_iv": summ.atm_iv, "expected_move": summ.expected_move, "coverage": summ.coverage}
        cands = rank_options(b.chain[pd.to_datetime(b.chain["expiry"]).dt.date == e], d, b.spot, cand.target1, cand.stop)
        bo = best_option(cands)
        alts = [c.as_dict() for c in cands]
        if bo:
            rec = bo.as_dict()
            liq = {"score": bo.liquidity_score / 100, "spread_pct": bo.spread_pct}
            prem_risk = (bo.premium - bo.expected_premium_at_stop) if bo.expected_premium_at_stop is not None else bo.premium * 0.4
    brd = None
    if not b.constituents.empty:
        bb = compute_breadth(b.constituents, b.universe_size)
        brd = {"score": bb.score, "authoritative": bb.authoritative, "coverage_pct": bb.coverage_pct, "advancing": bb.advancing, "declining": bb.declining}
    dq = DQ.PARTIAL if b.is_demo else DQ.VALID
    return Confirmations(fut, opts, brd, liq, rec, alts, dq, prem_risk)


strategies = load_strategies()
signals = [stage2(ctx, s, c, confirmations_for(c)) for s, c in stage1(ctx, strategies)]
db = state_db()
for s in signals:
    if not b.is_demo:
        db.upsert_signal(s.to_row())

st.subheader(f"Decision bar {ctx.setup['ts'].iloc[-1]:%H:%M} · regime `{ctx.regime.code}` · {len(signals)} signal(s)")
if not signals:
    st.info("**NO TRADE.** No strategy found a valid Stage-1 setup on the latest completed 5-minute bar.")
for sig in signals:
    color = {"HIGH_CONVICTION": "🟢", "STRONG_SETUP": "🟢", "VALID_SETUP": "🟡", "WATCH": "⚪", "NO_TRADE": "🔴"}.get(sig.status, "⚪")
    with st.container(border=True):
        h = st.columns([3, 1, 1, 1, 1, 1])
        h[0].markdown(f"### {color} {sig.status.replace('_', ' ')} · {sig.direction} · {sig.strategy_id} · {sig.trigger_event}")
        h[1].metric("Score", f"{sig.score:.0f}/100")
        h[2].metric("Entry", fmt(sig.entry, 0)); h[3].metric("Stop", fmt(sig.stop, 0)); h[4].metric("T1 / T2", f"{sig.target1:,.0f} / {sig.target2:,.0f}")
        h[5].metric("R:R", f"{sig.reward_risk:.2f}")
        c1, c2 = st.columns([1, 1])
        with c1:
            st.write("**Evidence**"); [st.write("• " + e) for e in sig.evidence]
            st.write("**Contradictions**"); [st.write("• " + e) for e in sig.contradictions] if sig.contradictions else st.write("• none")
            st.write(f"**Invalidation:** {sig.invalidation} · **expires:** {sig.expiry_conditions} · **data quality:** `{sig.data_quality}`")
            st.write("**Component scores:** " + ", ".join(f"{k} {v}" for k, v in sig.component_scores.items()))
            st.write(f"**Win probability:** {sig.win_probability if sig.win_probability is not None else 'n/a'} — {sig.win_probability_note}")
        with c2:
            if sig.recommended_option:
                ro = sig.recommended_option
                st.write(f"**Recommended option:** `{ro['tradingsymbol']}` @ {ro['premium']:.2f} · Δ {ro['delta']:.2f} · IV {ro['iv']*100:.1f}% · spread {ro['spread_pct']:.2f}% · liq {ro['liquidity_score']:.0f}")
                st.write(f"Premium at T1 ≈ {ro['expected_premium_at_target']:.1f} · at stop ≈ {ro['expected_premium_at_stop']:.1f} · premium R:R {ro['reward_risk_premium']:.2f}"
                         if ro.get("reward_risk_premium") else "")
                st.write(f"**Suggested size:** {sig.suggested_lots or 0} lot(s), max premium risk ≈ ₹{sig.suggested_max_risk_rupees or 0:,.0f}")
            else:
                st.write("**Recommended option:** NO SUITABLE OPTION")
            if sig.futures_confirmation:
                st.write(f"**Futures:** {sig.futures_confirmation['confirmation_score']:+.2f} · {sig.futures_confirmation['price_oi_label']} · basis {fmt(sig.futures_confirmation['basis'], 1)}")
            if sig.options_confirmation:
                oc = sig.options_confirmation
                st.write(f"**Options:** {oc['confirmation_score']:+.2f} · PCR {fmt(oc['pcr_oi'])} · max pain {fmt(oc['max_pain'], 0)} · walls {fmt(oc['put_wall'], 0)}/{fmt(oc['call_wall'], 0)}")
            if sig.breadth_confirmation:
                bc = sig.breadth_confirmation
                st.write(f"**Breadth:** {bc['score']:+.2f} ({bc['advancing']}↑ {bc['declining']}↓, coverage {bc['coverage_pct']:.0f}%{'' if bc['authoritative'] else ', not authoritative'})")
            st.error("**Do not execute because:**\n" + "\n".join(f"- {r}" for r in sig.do_not_execute_reasons)) if sig.do_not_execute_reasons else st.success("Risk gate passed.")
        with st.expander("Chart & manual acknowledgement"):
            view = ctx.setup.tail(80)
            st.plotly_chart(trade_marker(candlestick(view, "", ctx.levels, None, height=380), sig.entry, sig.stop, sig.target1, sig.target2), width="stretch")
            st.caption("This app never sends orders. If you execute manually in Kite, record it here so the journal can reconcile fills vs signal.")
            with st.form(f"ack_{sig.signal_id}"):
                a = st.columns(4)
                inst = a[0].text_input("Instrument", value=(sig.recommended_option or {}).get("tradingsymbol", "NIFTY"))
                qty = a[1].number_input("Qty", 75, 5000, 75, 75)
                price = a[2].number_input("Fill price", value=float((sig.recommended_option or {}).get("premium", sig.entry)))
                mode = a[3].selectbox("Mode", ["PAPER", "MANUAL"])
                if st.form_submit_button("Record"):
                    tid = Journal(db).open_trade(mode, inst, "BUY", int(qty), float(price), signal_id=sig.signal_id,
                                                  payload={"signal": sig.to_row(), "demo": b.is_demo})
                    st.success(f"Recorded {mode} trade {tid}")

with st.expander("Recent signals (state DB)"):
    hist = db.signals(100)
    if hist:
        hd = pd.DataFrame(hist)[["created_at", "strategy_id", "direction", "status", "score", "entry", "stop", "target1", "regime"]]
        st.dataframe(hd, hide_index=True, width="stretch")
    else:
        st.write("none yet")
