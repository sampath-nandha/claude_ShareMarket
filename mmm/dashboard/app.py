"""Home page. Run with: streamlit run mmm/dashboard/app.py"""

from __future__ import annotations

import streamlit as st

from mmm import __version__
from mmm.dashboard.common import DERIVED_NOTE, banner, fmt, load_bundle, page_header
from mmm.timeutil import is_market_open, now_ist

st.set_page_config(page_title="MoneyMakingMachine · NIFTY Options Intelligence", page_icon="📈", layout="wide",
                   initial_sidebar_state="expanded")

page_header("MoneyMakingMachine", f"Zerodha-only NIFTY 50 price-action & options intelligence · v{__version__} · decision support, no order execution")
b = load_bundle()
banner(b)

c1, c2, c3, c4, c5 = st.columns(5)
day = b.minute_bars[b.minute_bars["ts"].dt.date == b.minute_bars["ts"].iloc[-1].date()]
prev_days = b.minute_bars[b.minute_bars["ts"].dt.date < day["ts"].iloc[0].date()]
prev_close = float(prev_days["close"].iloc[-1]) if not prev_days.empty else float(day["open"].iloc[0])
chg = b.spot - prev_close
c1.metric("NIFTY 50", fmt(b.spot), f"{chg:+,.2f} ({chg / prev_close * 100:+.2f}%)")
c2.metric("Session H / L", f"{day['high'].max():,.0f} / {day['low'].min():,.0f}")
c3.metric("Open / Prev close", f"{day['open'].iloc[0]:,.0f} / {prev_close:,.0f}")
c4.metric("India VIX", fmt(b.vix, 2) if b.vix else "n/a")
c5.metric("Market", "OPEN" if is_market_open() else "CLOSED", now_ist().strftime("%H:%M:%S IST"))

st.markdown(
    """
### Pages
| Page | What you get |
|---|---|
| **Market Overview** | Live NIFTY chart, multi-timeframe structure, regime, S/R, opening range, freshness |
| **Price Action** | Swings (HH/HL/LH/LL), BOS/CHoCH, zones, breakouts & failed breakouts, candidate setups |
| **Futures** | Basis, OI change, volume, VWAP, depth imbalance, confirmation score |
| **Options Intelligence** | Full chain with IV/Greeks, OI walls, PCR, max pain, straddle & expected move, IV smile/skew, ranked contracts, strategy payoff builder |
| **Breadth** | Equal-weight advance/decline, participation, relative strength |
| **Live Signals** | Two-stage signals with evidence, contradictions, option recommendation and *do-not-execute* reasons |
| **Risk & Positions** | Funds, positions (read-only), paper/manual journal, limit utilisation |
| **Backtesting** | Strategy backtests with Wilson/bootstrap CIs, expectancy, drawdown, cost sensitivity, acceptance verdict |
| **Market Replay** | Step through a captured session; decision engine sees only the past |
| **System Health** | Auth, collector heartbeat, data coverage, DQ incidents, resources |
"""
)
st.info(DERIVED_NOTE)
st.caption("No strategy is marked actionable until it passes the >51% lower-confidence-bound, expectancy and sample-size gates on out-of-sample data. "
           "Until then every signal is labelled research-only. This application never places, modifies or cancels orders.")
