from __future__ import annotations

import pandas as pd
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

tabs = st.tabs(["By strategy", "By regime", "By hour", "By weekday", "By exit", "Cost sensitivity", "Trades", "Walk-forward", "Calibration", "Option leg"])
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
with tabs[7]:
    from mmm.backtest.walkforward import walk_forward

    st.write("Chronological folds; the last N sessions are an untouched holdout. Rule-based strategies have no fitted parameters, so folds measure stability, not fit.")
    w = st.columns(4)
    tr_d = w[0].number_input("Train sessions", 20, 400, 60)
    te_d = w[1].number_input("Test sessions", 5, 120, 15)
    ho_d = w[2].number_input("Holdout sessions", 0, 200, 10)
    if w[3].button("Run walk-forward"):
        with st.spinner("Walking forward…"):
            folds, oos, notes = walk_forward(b.minute_bars, BacktestConfig(point_value=pv, cost_per_trade=cost, slippage_points=slip, max_hold_bars=hold, strategies=strats),
                                             int(tr_d), int(te_d), holdout_days=int(ho_d))
        st.session_state["wf"] = (folds, oos, notes)
    wf = st.session_state.get("wf")
    if wf:
        folds, oos, notes = wf
        for n_ in notes:
            st.caption(n_)
        if folds:
            st.dataframe(pd.DataFrame([{"test": f"{f.test_start}→{f.test_end}", "n": f.stats.n, "win_rate": f.stats.win_rate, "LB95": f.stats.win_rate_ci_low,
                                        "net": f.stats.net_pnl, "expectancy": f.stats.expectancy, "verdict": f.stats.verdict} for f in folds])
                         .style.format({"win_rate": "{:.1%}", "LB95": "{:.1%}", "net": "{:,.0f}", "expectancy": "{:,.0f}"}), hide_index=True, width="stretch")
        if oos:
            st.write(f"**Pooled OOS:** n={oos.n}, win rate {oos.win_rate:.1%} (LB {oos.win_rate_ci_low:.1%}), expectancy ₹{oos.expectancy:,.0f}, verdict **{oos.verdict}**")
            for r in oos.reasons:
                st.write("• " + r)
with tabs[8]:
    from mmm.backtest import calibration

    st.write("Score → observed win rate on out-of-sample trades. A probability is only shown when the calibrator is usable (n ≥ minimum).")
    src = st.radio("Fit on", ["Walk-forward OOS trades (correct)", "This backtest's trades (in-sample, for illustration only)"], index=0)
    trades_for_cal = None
    if src.startswith("Walk") and st.session_state.get("wf") and st.session_state["wf"][0]:
        trades_for_cal = pd.concat([f.trades for f in st.session_state["wf"][0]], ignore_index=True)
    elif src.startswith("This"):
        trades_for_cal = t
    if trades_for_cal is None or trades_for_cal.empty:
        st.info("Run the walk-forward tab first (or pick in-sample).")
    else:
        trades_for_cal = trades_for_cal.copy(); trades_for_cal["win"] = trades_for_cal["pnl_net"] > 0
        cal = calibration.fit(trades_for_cal, score_col="pa_score", min_n=v.min_trades_per_strategy)
        cc = st.columns(4)
        cc[0].metric("Method", cal.method); cc[1].metric("n", cal.n, "usable" if cal.usable else f"need ≥{cal.min_n}")
        cc[2].metric("Brier", f"{cal.brier:.3f}" if cal.brier is not None else "–"); cc[3].metric("Base win rate", f"{cal.base_rate:.1%}" if cal.base_rate is not None else "–")
        if cal.reliability:
            rel = pd.DataFrame(cal.reliability)
            figc = go.Figure()
            figc.add_trace(go.Scatter(x=rel["predicted"], y=rel["observed"], mode="markers+lines", name="bins", error_y=dict(type="data", symmetric=False,
                                      array=rel["ci_hi"] - rel["observed"], arrayminus=rel["observed"] - rel["ci_lo"]), text=[f"n={n_}" for n_ in rel["n"]]))
            figc.add_trace(go.Scatter(x=[0, 1], y=[0, 1], mode="lines", name="perfect", line=dict(dash="dash", color="grey")))
            figc.update_layout(title="Reliability diagram", xaxis_title="predicted", yaxis_title="observed", height=380, template="plotly_dark")
            st.plotly_chart(figc, width="stretch")
            st.dataframe(rel.style.format({"lo": "{:.0f}", "hi": "{:.0f}", "predicted": "{:.1%}", "observed": "{:.1%}", "ci_lo": "{:.1%}", "ci_hi": "{:.1%}"}), hide_index=True, width="stretch")
        if src.startswith("Walk") and not b.is_demo and st.button("Save as live calibrator"):
            calibration.save(cal)
            st.success("Saved. Live Signals will show calibrated probabilities when usable.")
        elif b.is_demo:
            st.caption("Saving is disabled on synthetic data.")
with tabs[9]:
    from mmm.backtest.option_leg import evaluate_option_leg

    st.write("What buying the ATM option on each signal would have made, using archived option candles. Trades without option data are reported, not guessed.")
    itm = st.selectbox("Strikes ITM", [0, 1, 2], index=0)
    ol = evaluate_option_leg(t, itm_strikes=int(itm))
    st.write(f"Evaluated {ol.evaluated} · not evaluated {ol.not_evaluated} · reasons: {ol.reasons or 'none'}")
    if ol.evaluated:
        os_ = compute_stats(ol.trades)
        oc = st.columns(5)
        oc[0].metric("Option win rate", f"{os_.win_rate:.1%}", f"LB {os_.win_rate_ci_low:.1%}"); oc[1].metric("Net ₹", f"{os_.net_pnl:,.0f}")
        oc[2].metric("Expectancy", f"{os_.expectancy:,.0f}"); oc[3].metric("PF", f"{os_.profit_factor:.2f}" if os_.profit_factor not in (None, float('inf')) else "∞")
        oc[4].metric("Verdict", os_.verdict)
        st.dataframe(ol.trades, hide_index=True, width="stretch", height=400)
    else:
        st.info("No archived option candles for these trades. Run `mmm data backfill-options` while contracts are active, and keep the collector running so history accumulates.")
st.caption(f"Acceptance requires ≥{v.min_trades_per_strategy} trades, 95% lower bound of win rate >{v.target_win_rate:.0%} (Wilson and session bootstrap), positive expectancy and PF>1 — "
           "on out-of-sample data. Even then, forward paper trading is required before a strategy is marked FORWARD VALIDATED.")
