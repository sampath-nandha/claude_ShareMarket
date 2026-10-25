from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from mmm.backtest.engine import BacktestConfig, run_backtest
from mmm.backtest.metrics import breakdown, compute_stats, cost_sensitivity
from mmm.dashboard.common import banner, load_bundle, page_header
from mmm.settings import get_config
from mmm.strategies import load_strategies

st.set_page_config(page_title="Backtesting", layout="wide")
page_header("Backtesting", "Same strategy code as live. Conservative fills, costs included, honest confidence intervals.")
b = load_bundle(days=60)
banner(b)
v = get_config().validation
if b.is_demo:
    st.error("Backtests on SYNTHETIC data say nothing about real markets. They only exercise the pipeline. Backfill real NIFTY minute history first.")

sessions = sorted(b.minute_bars["ts"].dt.date.unique())
c = st.columns(5)
strats = c[0].multiselect("Strategies", [s.id for s in load_strategies()], default=[s.id for s in load_strategies()])
start, end = c[1].select_slider("Date range", options=sessions, value=(sessions[0], sessions[-1]))
pv = c[2].number_input("₹ per index point", 1.0, 1000.0, 75.0, 5.0)
cost = c[3].number_input("₹ cost per round trip", 0.0, 1000.0, 60.0, 5.0)
slip = c[4].number_input("Slippage pts / side", 0.0, 10.0, 1.0, 0.5)
hold = st.slider("Max hold (minutes)", 15, 240, 120, 15)

if st.button("Run backtest", type="primary"):
    with st.spinner("Running event-driven backtest…"):
        res = run_backtest(b.minute_bars, BacktestConfig(start=start, end=end, point_value=pv, cost_per_trade=cost, slippage_points=slip, max_hold_bars=hold, strategies=strats))
    st.session_state["bt"] = res
res = st.session_state.get("bt")
if res is None:
    st.stop()
t = res.trades
st.caption(" · ".join(res.notes))
st.write(f"Sessions {res.sessions} · candidates {res.candidates} · trades {res.accepted}")
if t.empty:
    st.warning("No trades were generated for this configuration.")
    st.stop()
s = compute_stats(t)
verdict_color = {"ACCEPTED": "🟢", "REJECTED": "🔴", "INSUFFICIENT_EVIDENCE": "🟡"}[s.verdict]
st.subheader(f"{verdict_color} Verdict: {s.verdict}")
for r in s.reasons:
    st.write("• " + r)
c = st.columns(8)
c[0].metric("Trades", s.n, f"{s.wins}W {s.losses}L {s.breakeven}BE")
c[1].metric("Win rate", f"{s.win_rate*100:.1f}%", f"95% LB {s.win_rate_ci_low*100:.1f}% (target >{v.target_win_rate*100:.0f}%)")
c[2].metric("Bootstrap LB", f"{s.boot_ci_low*100:.1f}%" if s.boot_ci_low is not None else "n/a", "session-block")
c[3].metric("Net P&L", f"₹{s.net_pnl:,.0f}", f"gross ₹{s.gross_pnl:,.0f}")
c[4].metric("Expectancy", f"₹{s.expectancy:,.0f}/trade")
c[5].metric("Profit factor", f"{s.profit_factor:.2f}" if s.profit_factor not in (None, float('inf')) else "∞")
c[6].metric("Max drawdown", f"₹{s.max_drawdown:,.0f}")
c[7].metric("Avg win / loss", f"{s.avg_win or 0:,.0f} / {s.avg_loss or 0:,.0f}")

eq = t.sort_values("exit_ts")
fig = go.Figure()
fig.add_trace(go.Scatter(x=eq["exit_ts"], y=eq["pnl_net"].cumsum(), name="net", line=dict(color="#26a69a")))
fig.add_trace(go.Scatter(x=eq["exit_ts"], y=eq["pnl_gross"].cumsum(), name="gross", line=dict(color="#90a4ae", dash="dot")))
fig.update_layout(title="Equity curve (₹)", height=360, template="plotly_dark", margin=dict(l=10, r=10, t=40, b=10))
st.plotly_chart(fig, width="stretch")

tabs = st.tabs(["By strategy", "By regime", "By hour", "By weekday", "By exit", "Cost sensitivity", "Trades"])
fmt = {"win_rate": "{:.1%}", "net_pnl": "{:,.0f}", "expectancy": "{:,.0f}", "profit_factor": "{:.2f}", "max_dd": "{:,.0f}"}
with tabs[0]:
    st.dataframe(breakdown(t, "strategy_id").style.format(fmt), hide_index=True, width="stretch")
    st.dataframe(breakdown(t, "direction").style.format(fmt), hide_index=True, width="stretch")
with tabs[1]:
    t2 = t.copy(); t2["regime_dir"] = t2["regime"].str.split("|").str[0]
    st.dataframe(breakdown(t2, "regime_dir").style.format(fmt), hide_index=True, width="stretch")
    st.dataframe(breakdown(t, "regime").style.format(fmt), hide_index=True, width="stretch", height=300)
with tabs[2]:
    st.dataframe(breakdown(t, "hour").style.format(fmt), hide_index=True, width="stretch")
with tabs[3]:
    st.dataframe(breakdown(t, "weekday").style.format(fmt), hide_index=True, width="stretch")
with tabs[4]:
    st.dataframe(breakdown(t, "exit_reason").style.format(fmt), hide_index=True, width="stretch")
    st.write(f"Directional hit rate (T1 before stop): {t['directional_hit'].mean()*100:.1f}% — this is NOT the strategy win rate and NOT option P&L.")
with tabs[5]:
    st.dataframe(cost_sensitivity(t).style.format({"win_rate": "{:.1%}", "net_pnl": "{:,.0f}", "expectancy": "{:,.0f}"}), hide_index=True, width="stretch")
with tabs[6]:
    st.dataframe(t.sort_values("entry_ts", ascending=False), hide_index=True, width="stretch", height=500)
    st.download_button("Download trades CSV", t.to_csv(index=False).encode(), "backtest_trades.csv", "text/csv")
st.caption(f"Acceptance requires ≥{v.min_trades_per_strategy} trades, 95% lower bound of win rate >{v.target_win_rate:.0%} (Wilson and session bootstrap), positive expectancy and PF>1 — "
           "on out-of-sample data. Even then, forward paper trading is required before a strategy is marked FORWARD VALIDATED.")
