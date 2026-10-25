from __future__ import annotations

import pandas as pd
import streamlit as st

from mmm.dashboard.common import banner, load_bundle, page_header, state_db
from mmm.paper.journal import Journal
from mmm.risk.limits import RiskState, check
from mmm.data.quality import DQ
from mmm.settings import get_config
from mmm.timeutil import now_ist

st.set_page_config(page_title="Risk & Positions", layout="wide")
page_header("Risk & Positions", "Read-only account view, paper/manual journal and limit utilisation")
b = load_bundle()
banner(b)
cfg = get_config().risk
db = state_db()
j = Journal(db)

st.subheader("Zerodha account (read-only)")
try:
    from mmm.broker.auth import get_client

    k = get_client()
    marg = k.margins()
    pos = k.positions()
    eq = marg.get("equity", {})
    c = st.columns(4)
    c[0].metric("Net", f"₹{eq.get('net', 0):,.0f}")
    c[1].metric("Available cash", f"₹{eq.get('available', {}).get('cash', 0):,.0f}")
    c[2].metric("Live balance", f"₹{eq.get('available', {}).get('live_balance', 0):,.0f}")
    c[3].metric("Utilised", f"₹{eq.get('utilised', {}).get('debits', 0):,.0f}")
    net = pd.DataFrame(pos.get("net", []))
    if not net.empty:
        st.dataframe(net[["tradingsymbol", "quantity", "average_price", "last_price", "pnl", "m2m"]], hide_index=True, width="stretch")
        st.metric("Open position P&L", f"₹{net['pnl'].sum():,.0f}")
    else:
        st.write("No open positions.")
except Exception as exc:
    st.info(f"Account data unavailable ({exc}). Log in with `mmm auth login`.")

st.subheader("Journal")
mode = st.radio("Mode", ["PAPER", "MANUAL", "ALL"], horizontal=True)
mode_f = None if mode == "ALL" else mode
summary = j.summary(mode_f)
c = st.columns(6)
c[0].metric("Closed", summary.get("closed", 0)); c[1].metric("Open", summary.get("open", 0))
c[2].metric("Win rate", f"{summary['win_rate']*100:.1f}%" if summary.get("win_rate") is not None else "–")
c[3].metric("Net P&L", f"₹{summary.get('net_pnl', 0):,.0f}"); c[4].metric("Costs", f"₹{summary.get('costs', 0):,.0f}")
c[5].metric("Avg win / loss", f"{summary.get('avg_win', 0):,.0f} / {summary.get('avg_loss', 0):,.0f}")
df = j.frame(mode_f)
if not df.empty:
    st.dataframe(df[["trade_id", "mode", "created_at", "instrument", "direction", "qty", "entry_price", "exit_price", "exit_reason", "pnl_net", "mfe", "mae", "signal_id"]],
                 hide_index=True, width="stretch")
    open_t = [t for t in j.open_trades(mode_f)]
    if open_t:
        st.write("**Close an open trade** (records the exit you made manually / paper exit):")
        with st.form("close"):
            a = st.columns(4)
            tid = a[0].selectbox("Trade", [t["trade_id"] for t in open_t])
            px = a[1].number_input("Exit price", value=0.0)
            reason = a[2].selectbox("Reason", ["TARGET1", "TARGET2", "STOP", "TIME", "MANUAL", "SESSION_END"])
            if st.form_submit_button("Close"):
                t = j.close_trade(tid, float(px), reason)
                st.success(f"Closed {tid}: net ₹{t['pnl_net']:,.0f}")
            a[3].write("")
else:
    st.write("No trades recorded yet.")

st.subheader("Risk limits")
closed_today = df[(df.get("exit_price").notna()) & (pd.to_datetime(df["created_at"]).dt.date == now_ist().date())] if not df.empty else pd.DataFrame()
daily_pnl = float(closed_today["pnl_net"].sum()) if not closed_today.empty else 0.0
consec = 0
if not closed_today.empty:
    for v in closed_today.sort_values("exit_ts")["pnl_net"][::-1]:
        if v < 0:
            consec += 1
        else:
            break
state = RiskState(daily_pnl=daily_pnl, consecutive_losses=consec, recommendations_today=len(db.signals(since=now_ist().replace(hour=0, minute=0))),
                  open_positions=len(j.open_trades()))
rc = check(state, 1, 2.0, 30.0, 0.5, now_ist(), now_ist(), DQ.VALID)
u = rc.utilisation
c = st.columns(4)
c[0].metric("Daily loss used", f"{u['daily_loss_pct']:.0f}%", f"₹{daily_pnl:,.0f} of -₹{cfg.account_capital*cfg.max_daily_loss_pct/100:,.0f}")
c[1].metric("Recommendations", f"{u['recommendations_pct']:.0f}%", f"{state.recommendations_today}/{cfg.max_daily_recommendations}")
c[2].metric("Open positions", f"{u['positions_pct']:.0f}%", f"{state.open_positions}/{cfg.max_concurrent_positions}")
c[3].metric("Consecutive losses", f"{u['consecutive_losses_pct']:.0f}%", f"{consec}/{cfg.max_consecutive_losses}")
for k_, v_ in u.items():
    st.progress(min(int(v_), 100), text=k_)
if rc.reasons:
    st.error("New recommendations are currently blocked: " + "; ".join(rc.reasons))
with st.expander("Configured limits (config/settings.yaml → risk)"):
    st.json(cfg.model_dump())
