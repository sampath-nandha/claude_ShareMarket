from __future__ import annotations

import pandas as pd
import streamlit as st

from mmm.analytics.structure import candle_anatomy, swings_frame
from mmm.dashboard.common import banner, candlestick, fmt, load_bundle, page_header, trade_marker
from mmm.signals.engine import build_context, stage1
from mmm.strategies import load_strategies

st.set_page_config(page_title="Price Action", layout="wide")
page_header("Price Action", "Swings, structure events, zones, candle behaviour and Stage-1 setup candidates")
b = load_bundle()
banner(b)
ctx = build_context(b.minute_bars, b.as_of)
if ctx is None:
    st.error("Not enough history.")
    st.stop()

strategies = load_strategies()
cands = stage1(ctx, strategies)

st.subheader("Setup timeframe (5m) with confirmed swings and zones")
sess = sorted(ctx.setup["ts"].dt.date.unique())[-2:]
view = ctx.setup[ctx.setup["ts"].dt.date.isin(sess)]
sw_view = [s for s in ctx.setup_swings if s.ts >= view["ts"].iloc[0]]
fig = candlestick(view, "NIFTY 50 · 5m", ctx.levels, sw_view, height=560)
for _s, c in cands:
    trade_marker(fig, c.entry, c.stop, c.target1, c.target2)
st.plotly_chart(fig, width="stretch")

c1, c2 = st.columns(2)
with c1:
    st.subheader("Structure")
    st.write(f"**Setup TF:** {ctx.setup_structure.trend} · event: `{ctx.setup_structure.last_event}` · labels: {' '.join(ctx.setup_structure.labels)}")
    st.write(f"**Context TF:** {ctx.context_structure.trend} · event: `{ctx.context_structure.last_event}`")
    st.write(f"**Trend TF:** {ctx.trend_structure.trend} · event: `{ctx.trend_structure.last_event}`")
    st.write(f"**Regime:** `{ctx.regime.code}`")
    sf = swings_frame(ctx.setup_swings).tail(12)
    if not sf.empty:
        sf["confirmed_bars_later"] = sf["confirmed_at"] - sf["idx"]
        st.dataframe(sf[["ts", "kind", "label", "price", "confirmed_bars_later"]].style.format({"price": "{:,.0f}"}), hide_index=True, width="stretch")
with c2:
    st.subheader("Last candles (anatomy)")
    an = candle_anatomy(ctx.setup).tail(8)
    show = pd.concat([ctx.setup[["ts", "open", "high", "low", "close"]].tail(8).reset_index(drop=True), an.reset_index(drop=True)], axis=1)
    flags = []
    for _, r in show.iterrows():
        f = [k for k in ("inside", "outside", "bull_engulf", "bear_engulf", "pin_bull", "pin_bear", "momentum", "narrow", "wide") if bool(r[k])]
        flags.append(", ".join(f))
    show["patterns"] = flags
    st.dataframe(show[["ts", "close", "body_ratio", "upper_wick", "lower_wick", "close_loc", "patterns"]].style.format(
        {"close": "{:,.0f}", "body_ratio": "{:.2f}", "upper_wick": "{:.2f}", "lower_wick": "{:.2f}", "close_loc": "{:.2f}"}), hide_index=True, width="stretch")
    st.caption("Candle patterns are context only — they never authorise a trade by themselves.")

st.subheader(f"Stage-1 setup candidates on the latest completed 5m bar ({len(cands)})")
if not cands:
    st.info("No strategy found a valid price-action setup on the decision bar. NO TRADE is the default.")
for strat, c in cands:
    with st.expander(f"{strat.id} {strat.name} · {c.direction} · {c.trigger_event} · PA score {c.pa_score:.0f} · R:R {c.reward_risk:.2f}", expanded=True):
        cc = st.columns(5)
        cc[0].metric("Entry", fmt(c.entry, 0)); cc[1].metric("Stop", fmt(c.stop, 0)); cc[2].metric("T1", fmt(c.target1, 0)); cc[3].metric("T2", fmt(c.target2, 0))
        cc[4].metric("Risk (pts)", fmt(c.risk, 0))
        st.write("**Evidence:** " + "; ".join(c.evidence))
        if c.contradictions:
            st.write("**Contradictions:** " + "; ".join(c.contradictions))
        st.write(f"**Invalidation:** {c.invalidation} · expires after {c.expires_after_bars} bars · strategy status `{strat.status}`")

with st.expander("Strategy library"):
    st.dataframe(pd.DataFrame([s.spec() for s in strategies]), hide_index=True, width="stretch")
