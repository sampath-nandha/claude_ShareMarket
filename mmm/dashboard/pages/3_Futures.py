from __future__ import annotations

import pandas as pd
import streamlit as st

from mmm.analytics.futures import futures_snapshot
from mmm.dashboard.common import banner, fmt, load_bundle, page_header, session_bars

st.set_page_config(page_title="Futures", layout="wide")
page_header("Futures Intelligence", "NIFTY near-month futures: basis, OI, volume, VWAP (approx), depth imbalance")
b = load_bundle()
banner(b)

q = b.futures_quote
if not q:
    st.info("No futures quote captured yet (collector not running).")
    st.stop()
today = b.minute_bars["ts"].iloc[-1].date()
day = session_bars(b.minute_bars, today)
direction = st.radio("Evaluate confirmation for a hypothetical", ["BULLISH", "BEARISH"], horizontal=True)
snap = futures_snapshot(q, b.spot, int(q.get("oi", 0) * 0.98) if q.get("oi") else None, day, q.get("volume", 0) / 1.1 if q.get("volume") else None,
                        1 if direction == "BULLISH" else -1)
if b.is_demo:
    st.caption("Demo: previous-close OI and average volume are approximated; with real capture they come from the archive.")

c = st.columns(6)
c[0].metric("Futures", fmt(snap.last), f"basis {snap.basis:+.1f} ({snap.basis_pct:+.2f}%)" if snap.basis is not None else "")
c[1].metric("OI", f"{snap.oi:,.0f}" if snap.oi else "–", f"{snap.oi_change:+,.0f} ({snap.oi_change_pct:+.1f}%)" if snap.oi_change is not None else "")
c[2].metric("Volume", f"{snap.volume:,.0f}" if snap.volume else "–", f"rel {snap.rel_volume:.2f}x" if snap.rel_volume else "")
c[3].metric("Price/OI label", snap.price_oi_label)
c[4].metric("VWAP (approx)", fmt(snap.vwap))
c[5].metric("Confirmation", f"{snap.confirmation_score:+.2f}", direction)
c2 = st.columns(3)
c2[0].metric("Spread", f"{snap.spread:.2f} ({snap.spread_bps:.1f} bps)" if snap.spread is not None else "–")
c2[1].metric("Top-level depth imbalance", f"{snap.depth_imbalance:+.2f}" if snap.depth_imbalance is not None else "–")
c2[2].metric("Spot", fmt(snap.spot))
for n in snap.notes:
    st.write("• " + n)
st.caption("Price/OI labels (long build-up etc.) are conventional interpretations treated as statistical features, not proof of institutional positioning. "
           "Five-level depth is not the full order book.")

st.subheader("Interpretation grid")
grid = pd.DataFrame([
    {"price": "↑", "OI": "↑", "label": "LONG_BUILDUP", "conventional read": "new longs"},
    {"price": "↓", "OI": "↑", "label": "SHORT_BUILDUP", "conventional read": "new shorts"},
    {"price": "↑", "OI": "↓", "label": "SHORT_COVERING", "conventional read": "shorts exiting"},
    {"price": "↓", "OI": "↓", "label": "LONG_UNWINDING", "conventional read": "longs exiting"},
])
st.dataframe(grid, hide_index=True, width="stretch")
