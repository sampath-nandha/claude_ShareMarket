from __future__ import annotations


import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from mmm.analytics.options.chain import chain_table, oi_support_resistance, summarise_chain
from mmm.analytics.options.payoff import Leg, payoff
from mmm.analytics.options.pricing import bs_price, expected_move_from_iv
from mmm.analytics.options.selector import best_option, rank_options
from mmm.dashboard.common import DERIVED_NOTE, banner, expiry_label, fmt, load_bundle, page_header
from mmm.risk.costs import breakeven_move, round_trip_costs
from mmm.settings import get_config
from mmm.timeutil import years_to_expiry

st.set_page_config(page_title="Options Intelligence", layout="wide")
page_header("Options Intelligence", "Chain · OI structure · IV & Greeks · straddle/expected move · ranked contracts · payoff builder")
b = load_bundle()
banner(b)
cfg = get_config()
if b.chain.empty:
    st.info("No option chain yet. Start the collector during market hours (`mmm collect`).")
    st.stop()

expiries = sorted(pd.to_datetime(b.chain["expiry"]).dt.date.unique())
exp = st.selectbox("Expiry", expiries, format_func=lambda e: expiry_label(e, b.as_of))
en = b.chain[pd.to_datetime(b.chain["expiry"]).dt.date == exp].copy()
expected_strikes = 2 * cfg.universe.option_strikes_each_side + 1
summ = summarise_chain(b.chain, b.spot, exp, expected_strikes, cfg.universe.strike_step)
lot = cfg.risk.nifty_lot_size
t_years = years_to_expiry(exp, b.as_of)

# ---------- headline metrics ----------
m = st.columns(8)
m[0].metric("Spot", fmt(b.spot))
m[1].metric("ATM", fmt(summ.atm_strike, 0))
m[2].metric("ATM straddle", fmt(summ.atm_straddle), f"±{summ.expected_move:,.0f} ({summ.expected_move_pct:.2f}%) exp. move" if summ.expected_move else "")
m[3].metric("ATM IV", f"{summ.atm_iv*100:.1f}%" if summ.atm_iv else "–", f"skew(25Δ P-C) {summ.iv_skew_25d*100:+.1f}%" if summ.iv_skew_25d is not None else "")
m[4].metric("PCR (OI)", fmt(summ.pcr_oi), f"vol PCR {summ.pcr_volume:.2f}" if summ.pcr_volume else "")
m[5].metric("Max pain", fmt(summ.max_pain, 0))
m[6].metric("Call wall / Put wall", f"{summ.call_wall:,.0f} / {summ.put_wall:,.0f}" if summ.call_wall and summ.put_wall else "–")
m[7].metric("Chain coverage", summ.coverage, f"{summ.strikes_present}/{summ.strikes_expected} strikes")
if summ.atm_iv:
    em1 = expected_move_from_iv(b.spot, summ.atm_iv, t_years)
    st.caption(f"1σ expected move to expiry from ATM IV: ±{em1:,.0f} pts ({em1/b.spot*100:.2f}%). Straddle-based estimate uses the straddle/0.8 heuristic. {DERIVED_NOTE}")
for n in summ.notes:
    st.warning(n)

tab_chain, tab_oi, tab_iv, tab_cand, tab_build, tab_theta = st.tabs(
    ["Option chain", "OI structure", "IV & Greeks", "Ranked contracts", "Strategy builder", "Theta & cost planner"])

# ---------- chain ----------
with tab_chain:
    tbl = chain_table(b.chain, exp)
    width = st.slider("Strikes each side of ATM", 3, cfg.universe.option_strikes_each_side, 8)
    tbl = tbl[(tbl["strike"] - summ.atm_strike).abs() <= width * cfg.universe.strike_step]
    cols = ["CE_oi", "CE_oi_change", "CE_volume", "CE_iv", "CE_delta", "CE_last_price", "CE_bid", "CE_ask", "strike",
            "PE_bid", "PE_ask", "PE_last_price", "PE_delta", "PE_iv", "PE_volume", "PE_oi_change", "PE_oi"]
    view = tbl[cols].copy()
    for c in ("CE_iv", "PE_iv"):
        view[c] = view[c] * 100

    def _hl(row):
        styles = [""] * len(row)
        if row["strike"] == summ.atm_strike:
            styles = ["background-color: #37474f"] * len(row)
        return styles

    st.dataframe(
        view.style.apply(_hl, axis=1).format({"CE_oi": "{:,.0f}", "PE_oi": "{:,.0f}", "CE_oi_change": "{:+,.0f}", "PE_oi_change": "{:+,.0f}",
                                             "CE_volume": "{:,.0f}", "PE_volume": "{:,.0f}", "CE_iv": "{:.1f}", "PE_iv": "{:.1f}", "CE_delta": "{:.2f}",
                                             "PE_delta": "{:.2f}", "CE_last_price": "{:.2f}", "PE_last_price": "{:.2f}", "CE_bid": "{:.2f}", "CE_ask": "{:.2f}",
                                             "PE_bid": "{:.2f}", "PE_ask": "{:.2f}", "strike": "{:,.0f}"}).background_gradient(subset=["CE_oi", "PE_oi"], cmap="Blues"),
        hide_index=True, width="stretch", height=600)
    st.caption("Grey row = ATM. OI change is vs the previous captured snapshot/close. Build-up labels per contract are in the OI tab.")

# ---------- OI structure ----------
with tab_oi:
    sub = en[(en["strike"] - summ.atm_strike).abs() <= 10 * cfg.universe.strike_step]
    ce, pe = sub[sub["option_type"] == "CE"], sub[sub["option_type"] == "PE"]
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.6, 0.4], vertical_spacing=0.05, subplot_titles=("Open interest by strike", "OI change by strike"))
    fig.add_trace(go.Bar(x=ce["strike"], y=ce["oi"], name="Call OI", marker_color="#ef5350"), row=1, col=1)
    fig.add_trace(go.Bar(x=pe["strike"], y=pe["oi"], name="Put OI", marker_color="#26a69a"), row=1, col=1)
    fig.add_trace(go.Bar(x=ce["strike"], y=ce["oi_change"], name="Call ΔOI", marker_color="#ef9a9a"), row=2, col=1)
    fig.add_trace(go.Bar(x=pe["strike"], y=pe["oi_change"], name="Put ΔOI", marker_color="#80cbc4"), row=2, col=1)
    fig.add_vline(x=b.spot, line=dict(color="white", dash="dash"), annotation_text=f"spot {b.spot:,.0f}")
    if summ.max_pain:
        fig.add_vline(x=summ.max_pain, line=dict(color="#ffb300", dash="dot"), annotation_text=f"max pain {summ.max_pain:,.0f}", annotation_position="top left")
    fig.update_layout(barmode="group", height=620, template="plotly_dark", legend=dict(orientation="h", y=1.08), margin=dict(l=10, r=10, t=60, b=10))
    st.plotly_chart(fig, width="stretch")
    sr = oi_support_resistance(b.chain, exp, b.spot)
    c1, c2 = st.columns(2)
    c1.write("**OI resistance (top call OI ≥ spot):** " + ", ".join(f"{k:,.0f} ({oi/1e5:.1f}L)" for k, oi in sr["resistance"]))
    c2.write("**OI support (top put OI ≤ spot):** " + ", ".join(f"{k:,.0f} ({oi/1e5:.1f}L)" for k, oi in sr["support"]))
    st.write(f"Total call OI {summ.call_oi_total/1e5:,.1f}L (Δ {summ.call_oi_change/1e5:+,.1f}L) · total put OI {summ.put_oi_total/1e5:,.1f}L (Δ {summ.put_oi_change/1e5:+,.1f}L)"
             if summ.call_oi_change is not None else f"Total call OI {summ.call_oi_total/1e5:,.1f}L · put OI {summ.put_oi_total/1e5:,.1f}L")
    st.subheader("Per-contract build-up (price vs ΔOI)")
    bu = sub[["tradingsymbol", "strike", "option_type", "last_price", "oi", "oi_change", "oi_change_pct", "buildup"]].sort_values(["strike", "option_type"])
    st.dataframe(bu.style.format({"strike": "{:,.0f}", "last_price": "{:.2f}", "oi": "{:,.0f}", "oi_change": "{:+,.0f}", "oi_change_pct": "{:+.1f}%"}),
                 hide_index=True, width="stretch", height=400)
    st.caption("OI walls are conventional support/resistance proxies; they are confirmation inputs, never a trade trigger. Build-up labels need `prev_close` per contract (captured from the archive).")

# ---------- IV & Greeks ----------
with tab_iv:
    sub = en[(en["strike"] - summ.atm_strike).abs() <= 10 * cfg.universe.strike_step]
    ce, pe = sub[sub["option_type"] == "CE"], sub[sub["option_type"] == "PE"]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=ce["strike"], y=ce["iv"] * 100, mode="lines+markers", name="Call IV", line=dict(color="#ef5350")))
    fig.add_trace(go.Scatter(x=pe["strike"], y=pe["iv"] * 100, mode="lines+markers", name="Put IV", line=dict(color="#26a69a")))
    fig.add_vline(x=b.spot, line=dict(color="white", dash="dash"))
    fig.update_layout(title="IV smile (derived, Black-Scholes)", yaxis_title="IV %", height=380, template="plotly_dark", margin=dict(l=10, r=10, t=40, b=10))
    st.plotly_chart(fig, width="stretch")
    if len(expiries) > 1:
        rows = []
        for e in expiries:
            s = summarise_chain(b.chain, b.spot, e, expected_strikes, cfg.universe.strike_step)
            rows.append({"expiry": expiry_label(e, b.as_of), "ATM IV %": s.atm_iv * 100 if s.atm_iv else np.nan, "straddle": s.atm_straddle,
                         "exp. move": s.expected_move, "PCR": s.pcr_oi, "max pain": s.max_pain})
        st.subheader("Term structure")
        st.dataframe(pd.DataFrame(rows).style.format({"ATM IV %": "{:.1f}", "straddle": "{:.1f}", "exp. move": "{:,.0f}", "PCR": "{:.2f}", "max pain": "{:,.0f}"}),
                     hide_index=True, width="stretch")
    st.subheader("Greeks per contract")
    g = sub[["tradingsymbol", "strike", "option_type", "mid", "iv", "delta", "gamma", "theta", "vega", "intrinsic", "time_value", "spread_pct", "liquidity_score"]].copy()
    g["iv"] = g["iv"] * 100
    g["theta_pct_day"] = g["theta"] / g["mid"] * 100
    st.dataframe(g.sort_values(["strike", "option_type"]).style.format({"strike": "{:,.0f}", "mid": "{:.2f}", "iv": "{:.1f}", "delta": "{:.3f}", "gamma": "{:.5f}",
                                                                       "theta": "{:.2f}", "vega": "{:.2f}", "intrinsic": "{:.1f}", "time_value": "{:.1f}", "spread_pct": "{:.2f}",
                                                                       "liquidity_score": "{:.0f}", "theta_pct_day": "{:.1f}"}), hide_index=True, width="stretch", height=420)
    st.caption("Theta is per calendar day in premium points; theta_pct_day = daily decay as % of premium (the real cost of being wrong on timing).")

# ---------- ranked contracts ----------
with tab_cand:
    st.write("Direction comes from price action first. This ranks contracts for a *given* view with transparent scoring and hard liquidity filters.")
    c = st.columns(4)
    direction = c[0].radio("View", ["BULLISH", "BEARISH"], horizontal=True)
    d = 1 if direction == "BULLISH" else -1
    tgt = c[1].number_input("Underlying target", value=float(round(b.spot + d * 120, 0)), step=10.0)
    stp = c[2].number_input("Underlying stop", value=float(round(b.spot - d * 60, 0)), step=10.0)
    hold = c[3].slider("Expected hold (hours)", 0.5, 6.0, 1.5, 0.5)
    lo, hi = st.slider("Delta band", 0.1, 0.9, (0.35, 0.65), 0.05)
    cands = rank_options(b.chain[pd.to_datetime(b.chain["expiry"]).dt.date == exp], d, b.spot, tgt, stp, hold / 6.25 / 1.0 / 1.0, (lo, hi), top_n=8)
    best = best_option(cands)
    if best:
        st.success(f"Best fit: **{best.tradingsymbol}** @ {best.premium:.2f} · Δ {best.delta:.2f} · IV {best.iv*100:.1f}% · liquidity {best.liquidity_score:.0f}/100 · "
                   f"premium at target ≈ {best.expected_premium_at_target:.1f}, at stop ≈ {best.expected_premium_at_stop:.1f} → premium R:R {best.reward_risk_premium:.2f}"
                   if best.reward_risk_premium else f"Best fit: **{best.tradingsymbol}** @ {best.premium:.2f}")
        risk_per_unit = best.premium - (best.expected_premium_at_stop or 0)
        from mmm.risk.limits import position_size

        lots, rupees = position_size(risk_per_unit)
        st.write(f"Suggested size at {cfg.risk.max_risk_per_trade_pct}% risk of ₹{cfg.risk.account_capital:,.0f}: **{lots} lot(s)** ({lots*lot} qty), premium at risk ≈ ₹{rupees:,.0f}. "
                 f"Round-trip costs for 1 lot ≈ ₹{round_trip_costs(best.premium, best.premium, lot).total:,.0f} ({breakeven_move(best.premium, lot):.2f} pts of premium).")
    else:
        st.error("NO SUITABLE OPTION — every candidate fails a hard filter (OI, volume, spread or delta band). Widen the band or wait for liquidity.")
    rows = [{**c_.as_dict(), "reasons": "; ".join(c_.reasons), "rejections": "; ".join(c_.rejections)} for c_ in cands]
    if rows:
        df = pd.DataFrame(rows)[["tradingsymbol", "strike", "premium", "delta", "iv", "theta", "spread_pct", "oi", "volume", "liquidity_score", "score",
                                 "expected_premium_at_target", "expected_premium_at_stop", "reward_risk_premium", "reasons", "rejections"]]
        df["iv"] = df["iv"] * 100
        st.dataframe(df.style.format({"strike": "{:,.0f}", "premium": "{:.2f}", "delta": "{:.2f}", "iv": "{:.1f}", "theta": "{:.2f}", "spread_pct": "{:.2f}", "oi": "{:,.0f}",
                                      "volume": "{:,.0f}", "liquidity_score": "{:.0f}", "score": "{:.1f}", "expected_premium_at_target": "{:.1f}",
                                      "expected_premium_at_stop": "{:.1f}", "reward_risk_premium": "{:.2f}"}), hide_index=True, width="stretch")
    st.caption("Score: liquidity 35 · delta fit 25 · theta burden 15 · spread 15 · premium efficiency 10. Re-pricing at target/stop keeps IV constant — a vol crush would lower both.")

# ---------- strategy builder ----------
with tab_build:
    st.write("Build a multi-leg position from this chain and inspect payoff at expiry, T+0 mark-to-model, breakevens and aggregate Greeks.")
    if "legs" not in st.session_state:
        st.session_state.legs = []
    strikes = sorted(en["strike"].unique())
    c = st.columns(5)
    k = c[0].selectbox("Strike", strikes, index=strikes.index(summ.atm_strike) if summ.atm_strike in strikes else 0)
    kind = c[1].selectbox("Type", ["CE", "PE"])
    side = c[2].selectbox("Side", ["BUY", "SELL"])
    lots_n = c[3].number_input("Lots", 1, 50, 1)
    row = en[(en["strike"] == k) & (en["option_type"] == kind)]
    prem = float(row["mid"].iloc[0]) if not row.empty and pd.notna(row["mid"].iloc[0]) else 0.0
    iv = float(row["iv"].iloc[0]) if not row.empty and pd.notna(row["iv"].iloc[0]) else None
    c[4].metric("Mid premium", f"{prem:.2f}")
    bcol = st.columns(4)
    if bcol[0].button("Add leg"):
        st.session_state.legs.append(Leg(kind, float(k), int(lots_n) * (1 if side == "BUY" else -1), prem, iv, t_years, lot))
    if bcol[1].button("Clear"):
        st.session_state.legs = []
    presets = bcol[2].selectbox("Preset", ["—", "Long straddle", "Short straddle", "Bull call spread", "Bear put spread", "Iron condor", "Long strangle"])
    if bcol[3].button("Load preset") and presets != "—":
        def _leg(kk, kd, q):
            r = en[(en["strike"] == kk) & (en["option_type"] == kd)]
            p = float(r["mid"].iloc[0]) if not r.empty and pd.notna(r["mid"].iloc[0]) else 0.0
            v = float(r["iv"].iloc[0]) if not r.empty and pd.notna(r["iv"].iloc[0]) else None
            return Leg(kd, float(kk), q, p, v, t_years, lot)
        a, s = summ.atm_strike, cfg.universe.strike_step
        st.session_state.legs = {
            "Long straddle": [_leg(a, "CE", 1), _leg(a, "PE", 1)],
            "Short straddle": [_leg(a, "CE", -1), _leg(a, "PE", -1)],
            "Bull call spread": [_leg(a, "CE", 1), _leg(a + 4 * s, "CE", -1)],
            "Bear put spread": [_leg(a, "PE", 1), _leg(a - 4 * s, "PE", -1)],
            "Iron condor": [_leg(a + 4 * s, "CE", -1), _leg(a + 8 * s, "CE", 1), _leg(a - 4 * s, "PE", -1), _leg(a - 8 * s, "PE", 1)],
            "Long strangle": [_leg(a + 3 * s, "CE", 1), _leg(a - 3 * s, "PE", 1)],
        }[presets]
    legs: list[Leg] = st.session_state.legs
    if legs:
        st.dataframe(pd.DataFrame([{"type": l.kind, "strike": l.strike, "lots": l.qty, "premium": l.premium, "iv %": (l.iv or np.nan) * 100} for l in legs])
                     .style.format({"strike": "{:,.0f}", "premium": "{:.2f}", "iv %": "{:.1f}"}), hide_index=True, width="stretch")
        days_fwd = st.slider("T+0 curve: days forward", 0.0, max(t_years * 365 - 0.1, 0.1), 0.0, 0.5)
        res = payoff(legs, b.spot, width_pct=5.0, days_forward=days_fwd)
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=res.grid["spot"], y=res.grid["expiry_pnl"], name="At expiry", line=dict(color="#ffb300", width=2)))
        if res.grid["t0_pnl"].notna().any():
            fig.add_trace(go.Scatter(x=res.grid["spot"], y=res.grid["t0_pnl"], name=f"T+{days_fwd:.1f}d (model)", line=dict(color="#29b6f6", dash="dash")))
        fig.add_hline(y=0, line=dict(color="grey"))
        fig.add_vline(x=b.spot, line=dict(color="white", dash="dot"), annotation_text="spot")
        for be in res.breakevens:
            fig.add_vline(x=be, line=dict(color="#66bb6a", dash="dot"), annotation_text=f"BE {be:,.0f}", annotation_position="bottom")
        if summ.expected_move:
            fig.add_vrect(x0=b.spot - summ.expected_move, x1=b.spot + summ.expected_move, fillcolor="#90a4ae", opacity=0.08, annotation_text="±1σ (straddle)", annotation_position="top left")
        fig.update_layout(height=460, template="plotly_dark", yaxis_title="P&L (₹)", xaxis_title="NIFTY at expiry", margin=dict(l=10, r=10, t=30, b=10))
        st.plotly_chart(fig, width="stretch")
        mm = st.columns(6)
        mm[0].metric("Net premium", f"₹{res.net_premium:+,.0f}", "credit" if res.net_premium > 0 else "debit")
        mm[1].metric("Max profit", "unlimited" if res.max_profit == float("inf") else f"₹{res.max_profit:,.0f}")
        mm[2].metric("Max loss", "unlimited" if res.max_loss == float("-inf") else f"₹{res.max_loss:,.0f}")
        mm[3].metric("Breakevens", ", ".join(f"{x:,.0f}" for x in res.breakevens) or "–")
        mm[4].metric("Δ / Γ", f"{res.agg_delta:+.1f} / {res.agg_gamma:+.4f}")
        mm[5].metric("Θ / day · Vega", f"₹{res.agg_theta:+,.0f} · ₹{res.agg_vega:+,.0f}")
        for n in res.notes:
            st.caption(n)
        if res.max_loss == float("-inf"):
            st.error("Undefined-risk position: naked short option exposure. Margin and tail risk are not modelled here.")
    else:
        st.info("Add legs or load a preset.")

# ---------- theta & cost planner ----------
with tab_theta:
    st.write("How much the underlying must move, by when, for a long option to beat decay and costs.")
    c = st.columns(3)
    kind = c[0].selectbox("Option", ["CE", "PE"], key="tk")
    strikes = sorted(en["strike"].unique())
    k = c[1].selectbox("Strike", strikes, index=strikes.index(summ.atm_strike) if summ.atm_strike in strikes else 0, key="ts")
    lots_n = c[2].number_input("Lots", 1, 20, 1, key="tl")
    row = en[(en["strike"] == k) & (en["option_type"] == kind)]
    if not row.empty and pd.notna(row["iv"].iloc[0]) and pd.notna(row["mid"].iloc[0]):
        r = row.iloc[0]
        prem, iv = float(r["mid"]), float(r["iv"])
        hours = np.array([0, 0.5, 1, 2, 3, 4, 6.25])
        moves = np.array([-150, -100, -50, 0, 50, 100, 150, 200])
        table = []
        for h in hours:
            t_rem = max(t_years - h / (24 * 365), 1e-6)
            rowv = {"hours held": h}
            for mv in moves:
                p = bs_price(b.spot + mv, k, t_rem, iv, kind)
                rowv[f"{mv:+d}"] = (p - prem) * lot * lots_n - round_trip_costs(prem, p, lot * lots_n).total
            table.append(rowv)
        tdf = pd.DataFrame(table).set_index("hours held")
        st.dataframe(tdf.style.format("{:+,.0f}").background_gradient(cmap="RdYlGn", axis=None), width="stretch")
        st.caption(f"Net ₹ P&L for {lots_n} lot(s) of {int(k)} {kind} bought at mid {prem:.2f} (IV held at {iv*100:.1f}%), after Zerodha costs and slippage. "
                   f"Daily theta ≈ {r['theta']:.2f} pts (₹{r['theta']*lot*lots_n:,.0f}/lot-day). Columns = NIFTY move in points.")
    else:
        st.info("No usable quote/IV for that contract.")
