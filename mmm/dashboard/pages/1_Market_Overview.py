from __future__ import annotations

import pandas as pd
import streamlit as st

from mmm.analytics.candles import resample
from mmm.analytics.levels import levels_frame
from mmm.analytics.structure import classify_structure, pivot_swings
from mmm.dashboard.common import banner, candlestick, fmt, load_bundle, page_header
from mmm.signals.engine import build_context

st.set_page_config(page_title="Market Overview", layout="wide")
page_header("Market Overview", "NIFTY 50 across timeframes with structure, regime and key levels")
b = load_bundle()
banner(b)

ctx = build_context(b.minute_bars, b.as_of)
if ctx is None:
    st.error("Not enough history to build context (need ~400 1-minute bars).")
    st.stop()

top = st.columns(6)
top[0].metric("Spot", fmt(b.spot))
top[1].metric("Regime", ctx.regime.direction, f"{ctx.regime.strength} · {ctx.regime.volatility} vol")
top[2].metric("Behaviour", ctx.regime.behaviour)
top[3].metric("ATR (5m)", fmt(ctx.atr, 1))
top[4].metric("Setup TF structure", ctx.setup_structure.trend, ctx.setup_structure.last_event or "")
top[5].metric("Context TF structure", ctx.context_structure.trend, ctx.context_structure.last_event or "")

r1, r2 = st.columns([2, 3])
has_fut = not b.futures_bars.empty
instrument = r1.radio("Instrument", ["Spot (NIFTY 50)", "Futures (near month)", "Spot + futures overlay"] if has_fut else ["Spot (NIFTY 50)"], horizontal=True)
tf = r2.radio("Chart timeframe", ["minute", "5minute", "15minute", "60minute", "day"], index=1, horizontal=True)
show_levels = st.checkbox("Show levels", True)
show_swings = st.checkbox("Show swings", True)
sess_n = st.slider("Sessions to show", 1, 20, 3)
sessions = sorted(b.minute_bars["ts"].dt.date.unique())[-sess_n:]
spot_bars = b.minute_bars[b.minute_bars["ts"].dt.date.isin(sessions)]
fut_bars = b.futures_bars[b.futures_bars["ts"].dt.date.isin(sessions)] if has_fut else pd.DataFrame()
spot_df = resample(spot_bars, tf) if tf != "minute" else spot_bars
fut_df = (resample(fut_bars, tf) if tf != "minute" else fut_bars) if has_fut and not fut_bars.empty else pd.DataFrame()
if has_fut and b.futures_quote.get("last_price"):
    basis = float(b.futures_quote["last_price"]) - b.spot
    st.caption(f"{b.futures_symbol}: {b.futures_quote['last_price']:,.2f} · basis {basis:+.1f} pts ({basis / b.spot * 100:+.2f}%) · futures carry volume/OI, the index does not")
if instrument.startswith("Spot"):
    df = spot_df
    sw = pivot_swings(df.reset_index(drop=True), 3, 3) if show_swings and len(df) > 10 else None
    ema = {"EMA20": df["close"].ewm(span=20, adjust=False).mean(), "EMA50": df["close"].ewm(span=50, adjust=False).mean()}
    if instrument.endswith("overlay") and not fut_df.empty:
        aligned = fut_df.set_index("ts")["close"].reindex(df["ts"]).to_numpy()
        ema[f"{b.futures_symbol} close"] = pd.Series(aligned, index=df.index)
    st.plotly_chart(candlestick(df, f"NIFTY 50 · {tf}" + (" + futures overlay" if instrument.endswith("overlay") else ""),
                                ctx.levels if show_levels else None, sw, extra_lines=ema), width="stretch")
else:
    df = fut_df
    if df.empty:
        st.info("No futures candles stored yet. Run `mmm data backfill` (futures are included) or wait for live capture.")
    else:
        sw = pivot_swings(df.reset_index(drop=True), 3, 3) if show_swings and len(df) > 10 else None
        ema = {"EMA20": df["close"].ewm(span=20, adjust=False).mean(), "EMA50": df["close"].ewm(span=50, adjust=False).mean()}
        st.plotly_chart(candlestick(df, f"{b.futures_symbol} · {tf}", None, sw, extra_lines=ema, show_volume=True), width="stretch")
        st.caption("Levels are computed on the index and are not drawn on the futures chart (they would be offset by the basis).")

c1, c2 = st.columns([1, 1])
with c1:
    st.subheader("Multi-timeframe structure")
    rows = []
    for name, frame, left in (("60minute", ctx.trend, 2), ("15minute", ctx.context, 3), ("5minute", ctx.setup, 3)):
        s = classify_structure(frame, pivot_swings(frame, left, left))
        rows.append({"timeframe": name, "trend": s.trend, "last event": s.last_event or "", "last swing H": fmt(s.last_high.price if s.last_high else None, 0),
                     "last swing L": fmt(s.last_low.price if s.last_low else None, 0), "recent labels": " ".join(s.labels[-6:])})
    daily = ctx.daily
    if len(daily) > 10:
        ds = classify_structure(daily.reset_index(drop=True), pivot_swings(daily.reset_index(drop=True), 2, 2))
        rows.insert(0, {"timeframe": "day", "trend": ds.trend, "last event": ds.last_event or "", "last swing H": fmt(ds.last_high.price if ds.last_high else None, 0),
                        "last swing L": fmt(ds.last_low.price if ds.last_low else None, 0), "recent labels": " ".join(ds.labels[-6:])})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.subheader("Opening range")
    for m in (5, 15, 30):
        o = ctx.intraday_today
        if o.empty:
            continue
        start = o["ts"].iloc[0]
        win = o[o["ts"] < start + pd.Timedelta(minutes=m)]
        done = o["ts"].iloc[-1] >= start + pd.Timedelta(minutes=m)
        st.write(f"OR{m}: {win['low'].min():,.0f} – {win['high'].max():,.0f} (width {win['high'].max() - win['low'].min():,.0f}) {'✓' if done else '⏳ forming'}")
with c2:
    st.subheader("Support / resistance")
    lf = levels_frame(ctx.levels)
    if not lf.empty:
        lf = lf[~((lf["kind"] == "ROUND") & (lf["strength"] < 40))]
        lf["side"] = ["R" if p > b.spot else "S" for p in lf["price"]]
        st.dataframe(lf[["side", "kind", "price", "strength", "touches", "tf"]].style.format({"price": "{:,.0f}", "strength": "{:.0f}"}),
                     hide_index=True, width="stretch", height=520)
