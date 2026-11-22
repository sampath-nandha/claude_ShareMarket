import { api, bullets, card, clear, el, fmtCol, grid, input, lineChart, metrics, note, num, pct, post, radio, select, signCellClass, simpleGrid, tabs, txtCol, xyChart } from "../lib.js";

export async function mount(root) {
  root.appendChild(el("h2", {}, "Backtesting"));
  root.appendChild(el("div", { class: "subtitle" }, "Same strategy code as live, including futures parity and option-OI data where archived. Conservative fills, costs included, honest confidence intervals."));
  const o = await api("/api/backtest/options");
  if (o.demo) root.appendChild(note("Backtests on SYNTHETIC data say nothing about real markets. They only exercise the pipeline. Backfill real NIFTY minute history first.", "error"));
  const req = { strategies: o.strategies.slice(), start: o.sessions[0], end: o.sessions[o.sessions.length - 1], point_value: 75, cost_per_trade: 60, slippage_points: 1, max_hold_bars: 120 };
  const checks = el("div", { class: "flex" }, o.strategies.map((s) => el("label", { class: "flex" }, el("input", { type: "checkbox", checked: true, onchange: (e) => {
    req.strategies = e.target.checked ? [...req.strategies, s] : req.strategies.filter((x) => x !== s);
  } }), s)));
  const out = el("div");
  const runBtn = el("button", { class: "primary", onclick: () => run() }, "Run backtest");
  root.append(card("Configuration", checks, el("div", { class: "controls" },
    select(o.sessions, req.start, (v) => { req.start = v; }, "from"), select(o.sessions, req.end, (v) => { req.end = v; }, "to"),
    input("number", req.point_value, (v) => { req.point_value = v; }, "₹ per index point"), input("number", req.cost_per_trade, (v) => { req.cost_per_trade = v; }, "₹ cost / round trip"),
    input("number", req.slippage_points, (v) => { req.slippage_points = v; }, "slippage pts / side", { step: 0.5 }), input("number", req.max_hold_bars, (v) => { req.max_hold_bars = v; }, "max hold (min)", { step: 15 }), runBtn)), out);
  let charts = [];
  let last = null;

  async function run() {
    runBtn.disabled = true;
    clear(out).appendChild(el("div", { class: "loading" }, "running event-driven backtest…"));
    try { last = await post("/api/backtest", req); } catch (e) { clear(out).appendChild(note(e.message, "error")); runBtn.disabled = false; return; }
    runBtn.disabled = false;
    render();
  }

  function render() {
    charts.forEach((c) => c.remove()); charts = [];
    clear(out);
    const r = last;
    out.appendChild(el("div", { class: "small" }, r.notes.join(" · ")));
    out.appendChild(el("div", {}, `Sessions ${r.sessions} · candidates ${r.candidates} · trades ${r.trades_n}`));
    if (!r.stats) { out.appendChild(note("No trades were generated for this configuration.", "warn")); return; }
    const s = r.stats, v = o.validation;
    out.appendChild(note(`Verdict: ${s.verdict}`, s.verdict === "ACCEPTED" ? "ok" : s.verdict === "REJECTED" ? "error" : "warn"));
    out.appendChild(bullets(s.reasons));
    out.appendChild(metrics([["Trades", s.n, `${s.wins}W ${s.losses}L ${s.breakeven}BE`], ["Win rate", pct(s.win_rate, 1, true), `95% LB ${pct(s.win_rate_ci_low, 1, true)} (target >${pct(v.target_win_rate, 0, true)})`],
      ["Bootstrap LB", s.boot_ci_low !== null ? pct(s.boot_ci_low, 1, true) : "n/a", "session-block"], ["Net P&L", `₹${num(s.net_pnl, 0)}`, `gross ₹${num(s.gross_pnl, 0)}`],
      ["Expectancy", `₹${num(s.expectancy, 0)}/trade`], ["Profit factor", s.profit_factor !== null ? num(s.profit_factor) : "∞"], ["Max drawdown", `₹${num(s.max_drawdown, 0)}`],
      ["Avg win / loss", `${num(s.avg_win || 0, 0)} / ${num(s.avg_loss || 0, 0)}`]]));
    const ec = el("div", { class: "chart" });
    out.appendChild(card("Equity curve (₹)", ec));
    const lc = lineChart(ec, { height: 300 });
    const dedupe = (times, vals) => { const m = new Map(); times.forEach((t, i) => m.set(t, vals[i])); return [...m.entries()].sort((a, b) => a[0] - b[0]).map(([time, value]) => ({ time, value })); };
    lc.line("net", dedupe(r.equity.time, r.equity.net), "#26a69a"); lc.line("gross", dedupe(r.equity.time, r.equity.gross), "#90a4ae", { lineStyle: 2 });
    charts.push(lc);
    const fmt = (k) => [txtCol(k, k), fmtCol("n", "n", 0), pctCol("win_rate", "win rate"), fmtCol("net_pnl", "net ₹", 0, { cellClass: signCellClass }), fmtCol("expectancy", "exp ₹", 0, { cellClass: signCellClass }),
      fmtCol("profit_factor", "PF"), fmtCol("max_dd", "max DD", 0)];
    const pctCol = (f, h) => ({ field: f, headerName: h, type: "rightAligned", valueFormatter: (p) => pct(p.value, 1, true) });
    const g = (rows, cols) => () => { const e = el("div"); grid(e, cols, rows); return e; };
    out.appendChild(tabs([
      { label: "By strategy", render: () => { const e = el("div"); const a = el("div"), b = el("div"); e.append(a, b); grid(a, fmt("strategy_id"), r.by.strategy_id); grid(b, fmt("direction"), r.by.direction); return e; } },
      { label: "By regime", render: g(r.by.regime_dir, fmt("regime_dir")) }, { label: "By hour", render: g(r.by.hour, fmt("hour")) }, { label: "By weekday", render: g(r.by.weekday, fmt("weekday")) },
      { label: "By exit", render: () => { const e = el("div"); const a = el("div"); e.append(a, el("div", { class: "small" }, `Directional hit rate (T1 before stop): ${pct(r.directional_hit, 1, true)} - NOT the strategy win rate and NOT option P&L.`)); grid(a, fmt("exit_reason"), r.by.exit_reason); return e; } },
      { label: "Cost sensitivity", render: () => { const e = el("div"); simpleGrid(e, r.cost_sensitivity); return e; } },
      { label: "Trades", render: () => { const e = el("div"); const gg = el("div"); const api_ = simpleGrid(gg, r.trades, { height: 460 }); e.append(el("button", { onclick: () => api_.exportDataAsCsv({ fileName: "backtest_trades.csv" }) }, "Download CSV"), gg); return e; } },
      { label: "Walk-forward", render: wfTab }, { label: "Calibration", render: calTab }, { label: "Option leg", render: olTab },
    ]));
  }

  function wfTab() {
    const w = el("div"), res = el("div");
    const p = { train_days: 60, test_days: 15, holdout_days: 10 };
    w.append(el("div", { class: "small" }, "Chronological folds; the last N sessions are an untouched holdout. Rule-based strategies have no fitted parameters, so folds measure stability, not fit."),
      el("div", { class: "controls" }, input("number", p.train_days, (v) => { p.train_days = v; }, "train sessions"), input("number", p.test_days, (v) => { p.test_days = v; }, "test sessions"),
        input("number", p.holdout_days, (v) => { p.holdout_days = v; }, "holdout sessions"), el("button", { class: "primary", onclick: async () => {
          clear(res).appendChild(el("div", { class: "loading" }, "walking forward…"));
          try {
            const d = await post("/api/backtest/walkforward", { ...req, ...p });
            clear(res);
            d.notes.forEach((n) => res.appendChild(el("div", { class: "small" }, n)));
            if (d.folds.length) { const g = el("div"); res.appendChild(g); grid(g, [txtCol("test", "test window", { flex: 2 }), fmtCol("n", "n", 0), { field: "win_rate", headerName: "win rate", valueFormatter: (q) => pct(q.value, 1, true) },
              { field: "lb95", headerName: "LB95", valueFormatter: (q) => pct(q.value, 1, true) }, fmtCol("net", "net ₹", 0), fmtCol("expectancy", "exp ₹", 0), txtCol("verdict", "verdict")], d.folds); }
            if (d.pooled) res.append(note(`Pooled OOS: n=${d.pooled.n}, win rate ${pct(d.pooled.win_rate, 1, true)} (LB ${pct(d.pooled.win_rate_ci_low, 1, true)}), expectancy ₹${num(d.pooled.expectancy, 0)}, verdict ${d.pooled.verdict}`), bullets(d.pooled.reasons));
          } catch (e) { clear(res).appendChild(note(e.message, "error")); }
        } }, "Run walk-forward")), res);
    return w;
  }

  function calTab() {
    const w = el("div"), res = el("div");
    let source = "walkforward";
    const go = async (save) => {
      try {
        const d = await post("/api/backtest/calibration", { source, save });
        clear(res);
        res.appendChild(metrics([["Method", d.method], ["n", d.n, d.usable ? "usable" : `need ≥${d.min_n}`], ["Brier", d.brier !== null ? num(d.brier, 3) : "–"], ["Base win rate", pct(d.base_rate, 1, true)]]));
        if (d.in_sample) res.appendChild(note("In-sample fit, for illustration only.", "warn"));
        if (d.saved) res.appendChild(note("Saved. Live Signals show calibrated probabilities when usable.", "ok"));
        if (d.reliability?.length) {
          const c = el("div"); res.appendChild(c);
          setTimeout(() => xyChart(c, { series: [{ type: "line", x: d.reliability.map((b) => b.predicted), y: d.reliability.map((b) => b.observed), color: "#29b6f6", name: "observed" },
            { type: "line", x: [0, 1], y: [0, 1], color: "#6e7681", name: "perfect", dash: "4 4" }], height: 300, zero: false, xLabel: "predicted", yLabel: "observed" }));
          const g = el("div"); res.appendChild(g); simpleGrid(g, d.reliability);
        }
      } catch (e) { clear(res).appendChild(note(e.message, "error")); }
    };
    w.append(el("div", { class: "small" }, "Score → observed win rate on out-of-sample trades. A probability is only shown when the calibrator is usable (n ≥ minimum)."),
      el("div", { class: "controls" }, radio([["walkforward", "Walk-forward OOS trades (correct)"], ["backtest", "This backtest (in-sample)"]], source, (v) => { source = v; }),
        el("button", { class: "primary", onclick: () => go(false) }, "Fit"), el("button", { onclick: () => go(true) }, "Save as live calibrator")), res);
    return w;
  }

  function olTab() {
    const w = el("div"), res = el("div");
    let itm = 0;
    const go = async () => {
      try {
        const d = await post("/api/backtest/optionleg", { itm_strikes: itm });
        clear(res).appendChild(el("div", {}, `Evaluated ${d.evaluated} · not evaluated ${d.not_evaluated} · reasons: ${JSON.stringify(d.reasons)}`));
        if (d.stats) {
          res.appendChild(metrics([["Option win rate", pct(d.stats.win_rate, 1, true), `LB ${pct(d.stats.win_rate_ci_low, 1, true)}`], ["Net ₹", num(d.stats.net_pnl, 0)], ["Expectancy", num(d.stats.expectancy, 0)],
            ["PF", d.stats.profit_factor !== null ? num(d.stats.profit_factor) : "∞"], ["Verdict", d.stats.verdict]]));
          const g = el("div"); res.appendChild(g); simpleGrid(g, d.trades, { height: 400 });
        } else res.appendChild(note("No archived option candles for these trades. Run `mmm data backfill-options` while contracts are active, and keep the collector running.", "warn"));
      } catch (e) { clear(res).appendChild(note(e.message, "error")); }
    };
    w.append(el("div", { class: "small" }, "What buying the ATM option on each signal would have made, using archived option candles. Trades without option data are reported, not guessed."),
      el("div", { class: "controls" }, select([[0, "ATM"], [1, "1 strike ITM"], [2, "2 strikes ITM"]], 0, (v) => { itm = Number(v); }, "strike"), el("button", { class: "primary", onclick: go }, "Evaluate")), res);
    return w;
  }

  out.appendChild(note(`Acceptance requires ≥${o.validation.min_trades_per_strategy} trades, 95% lower bound of win rate >${pct(o.validation.target_win_rate, 0, true)} (Wilson and session bootstrap), positive expectancy and PF>1 on out-of-sample data. Even then, forward paper trading is required before FORWARD_VALIDATED.`));
  return { unmount: () => charts.forEach((c) => c.remove()) };
}
