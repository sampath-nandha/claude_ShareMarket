from __future__ import annotations

import plotly.express as px
import streamlit as st

from mmm.analytics.breadth import compute_breadth
from mmm.dashboard.common import banner, fmt, load_bundle, page_header

st.set_page_config(page_title="Breadth", layout="wide")
page_header("Constituent Breadth", "EQUAL-WEIGHT basket statistics (Kite does not publish index weights)")
b = load_bundle()
banner(b)
if b.constituents.empty:
    st.info("No constituent quotes captured yet.")
    st.stop()
br = compute_breadth(b.constituents, b.universe_size)
c = st.columns(7)
c[0].metric("Coverage", f"{br.coverage_pct:.0f}%", "authoritative" if br.authoritative else "NOT authoritative")
c[1].metric("Advancing", br.advancing)
c[2].metric("Declining", br.declining)
c[3].metric("% above prev close", fmt(br.pct_above_prev_close, 0, pct=True))
c[4].metric("% above open", fmt(br.pct_above_open, 0, pct=True))
c[5].metric("Mean / median ret", f"{br.mean_return_pct:+.2f}% / {br.median_return_pct:+.2f}%" if br.mean_return_pct is not None else "–")
c[6].metric("Breadth score", f"{br.score:+.2f}")
if not br.authoritative:
    st.warning("Coverage below 80% — breadth is shown but excluded from signal scoring.")
t = br.table
if not t.empty:
    t = t.copy()
    t["ret_pct"] = t["ret_pct"].round(2)
    fig = px.bar(t.sort_values("ret_pct"), x="ret_pct", y="tradingsymbol", orientation="h", color="ret_pct", color_continuous_scale="RdYlGn",
                 title="Return vs previous close (%)", height=max(400, 14 * len(t)))
    fig.update_layout(template="plotly_dark", margin=dict(l=10, r=10, t=40, b=10), coloraxis_showscale=False)
    st.plotly_chart(fig, width="stretch")
    st.write(f"% at day high: {fmt(br.pct_at_day_high, 0, pct=True)} · % at day low: {fmt(br.pct_at_day_low, 0, pct=True)} · dispersion {fmt(br.dispersion_pct, 2, pct=True)}")
    st.dataframe(t[["tradingsymbol", "last_price", "close", "ret_pct", "from_open_pct"]].style.format({"last_price": "{:,.2f}", "close": "{:,.2f}", "ret_pct": "{:+.2f}", "from_open_pct": "{:+.2f}"}),
                 hide_index=True, width="stretch", height=400)
