from __future__ import annotations

from datetime import timedelta

import streamlit as st

from mmm.dashboard.common import banner, candlestick, load_bundle, page_header, session_bars, trade_marker
from mmm.signals.engine import build_context, stage1
from mmm.strategies import load_strategies

st.set_page_config(page_title="Market Replay", layout="wide")
page_header("Market Replay", "Step through a session; the engine only ever sees bars before the cursor")
b = load_bundle(days=60)
banner(b)
sessions = sorted(b.minute_bars["ts"].dt.date.unique())
day = st.selectbox("Session", sessions[::-1], index=0)
bars = session_bars(b.minute_bars, day)
n = len(bars)
if "replay_i" not in st.session_state or st.session_state.get("replay_day") != day:
    st.session_state.replay_i, st.session_state.replay_day = 60, day
c = st.columns([1, 1, 1, 6])
if c[0].button("⏮ Start"):
    st.session_state.replay_i = 30
if c[1].button("◀ -5m"):
    st.session_state.replay_i = max(30, st.session_state.replay_i - 5)
if c[2].button("▶ +5m"):
    st.session_state.replay_i = min(n, st.session_state.replay_i + 5)
st.session_state.replay_i = c[3].slider("Minute of session", 30, n, st.session_state.replay_i)
i = st.session_state.replay_i
cursor = bars["ts"].iloc[i - 1] + timedelta(minutes=1)
past = b.minute_bars[b.minute_bars["ts"] < cursor]
ctx = build_context(past, cursor.to_pydatetime())
view = bars.iloc[:i]
if ctx is None:
    st.plotly_chart(candlestick(view, f"{day} up to {cursor:%H:%M}"), width="stretch")
    st.info("Not enough history before this point.")
    st.stop()
cands = stage1(ctx, load_strategies())
fig = candlestick(view, f"{day} · 1m · as of {cursor:%H:%M} · regime {ctx.regime.code}", ctx.levels, None, height=520)
for _s, cnd in cands:
    trade_marker(fig, cnd.entry, cnd.stop, cnd.target1, cnd.target2)
st.plotly_chart(fig, width="stretch")
c = st.columns(4)
c[0].metric("Setup structure", ctx.setup_structure.trend, ctx.setup_structure.last_event or "")
c[1].metric("Context structure", ctx.context_structure.trend, ctx.context_structure.last_event or "")
c[2].metric("ATR", f"{ctx.atr:.1f}")
c[3].metric("Candidates", len(cands))
for s, cnd in cands:
    st.write(f"**{s.id} {cnd.direction} {cnd.trigger_event}** entry {cnd.entry:,.0f} stop {cnd.stop:,.0f} T1 {cnd.target1:,.0f} T2 {cnd.target2:,.0f} R:R {cnd.reward_risk:.2f} — " + "; ".join(cnd.evidence))
    future = bars.iloc[i:]
    if not future.empty:
        d = 1 if cnd.direction == "BULLISH" else -1
        hit_stop = (future["low"] <= cnd.stop) if d > 0 else (future["high"] >= cnd.stop)
        hit_t1 = (future["high"] >= cnd.target1) if d > 0 else (future["low"] <= cnd.target1)
        first_stop = hit_stop.idxmax() if hit_stop.any() else None
        first_t1 = hit_t1.idxmax() if hit_t1.any() else None
        if first_stop is not None and (first_t1 is None or first_stop <= first_t1):
            st.write("→ outcome (revealed only here, never to the engine): **STOP** first")
        elif first_t1 is not None:
            st.write("→ outcome: **T1** first")
        else:
            st.write("→ outcome: neither hit by session end")
