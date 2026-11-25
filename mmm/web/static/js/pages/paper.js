import { api, bullets, card, clear, el, fmtCol, grid, inr, lineChart, metrics, note, num, pct, post, select, signCellClass, signed, simpleGrid, txtCol } from "../lib.js";

const EVENTS = ["ALL", "ENTRY", "EXIT", "SCAN", "SKIP", "MARK", "EOD", "WARN", "ERROR", "CONTROL"];

export async function mount(root, { live }) {
  const st = { filter: "ALL", days: 365 };
  root.appendChild(el("h2", {}, "Paper Trading"));
  root.appendChild(el("div", { class: "subtitle" }, "Automatic simulated trading of NIFTY options on the nearest expiry, within the configured capital. No order is ever sent to Zerodha."));
  const head = el("div", { class: "controls" }), body = el("div"), btBox = el("div");
  root.append(head, body, btBox);
  let charts = [], unsub = null, poll = null;

  async function load() {
    let d;
    try { d = await api("/api/paper"); } catch (e) { clear(body).appendChild(note(e.message, "error")); return; }
    charts.forEach((c) => c.remove()); charts = [];
    if (unsub) { unsub(); unsub = null; }
    clear(head).append(el("span", { class: `pill ${d.status === "RUNNING" ? "ok" : d.status.startsWith("DEMO") || d.status === "PAUSED" ? "warn" : ""}` }, d.status),
      el("button", { onclick: async () => { await post("/api/paper/pause", { paused: !d.paused }); load(); } }, d.paused ? "Resume" : "Pause"),
      el("span", { class: "small" }, `capital Rs ${num(d.config.capital, 0)} · up to ${d.config.max_trades_per_day} trades/day · daily loss limit Rs ${num(d.config.max_daily_loss, 0)} · entries ${d.config.entry_window.join("-")} · exit by ${d.config.force_exit} · premium stop ${d.config.premium_stop_pct}%`));
    clear(body);
    const s = d.summary;
    body.appendChild(metrics([["Capital", inr(s.capital)], ["Equity", inr(s.equity), signed(s.equity - s.capital, 0), s.equity >= s.capital ? "pos" : "neg"],
      ["Realised P&L", inr(s.realised), "", s.realised >= 0 ? "pos" : "neg"], ["Today", inr(s.today), "", s.today >= 0 ? "pos" : "neg"],
      ["Closed trades", s.closed, s.closed ? `${s.wins} won` : ""], ["Win rate", s.win_rate !== null ? pct(s.win_rate, 1, true) : "–", "after costs"],
      ["Costs paid", inr(s.costs)], ["Best / worst", s.best !== null ? `${num(s.best, 0)} / ${num(s.worst, 0)}` : "–"]]));
    // open position, marked live from the tick feed
    const pc = el("div");
    body.appendChild(card("Open position", pc));
    if (d.position) {
      const p = d.position;
      const markEl = el("b", {}, num(p.mark)), pnlEl = el("b", {}, inr(p.unrealised));
      pc.append(metrics([["Contract", p.tradingsymbol, `expiry ${p.expiry}`], ["Quantity", `${p.lots} lot(s) · ${p.qty}`], ["Entry", num(p.entry_price), `invested ${inr(p.invested)}`],
        ["Mark (bid)", markEl], ["Unrealised", pnlEl], ["Underlying stop / T1", `${num(p.stop, 0)} / ${num(p.target1, 0)}`],
        ["Premium stop", num(p.premium_stop)], ["Time exit", new Date(p.deadline).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false })]]),
      el("div", { class: "small" }, `${p.strategy_id} ${p.direction} · entered ${new Date(p.entry_ts).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false })} at spot ${num(p.underlying_entry, 0)}`));
      unsub = live.subscribe(p.token, (t) => {
        const m = t.b || t.p;
        if (!m) return;
        markEl.textContent = num(m);
        const u = (m - p.entry_price) * p.qty;
        pnlEl.textContent = inr(u);
        pnlEl.className = u >= 0 ? "pos" : "neg";
      });
    } else pc.appendChild(note("No open position."));
    if (d.equity.length) {
      const ec = el("div", { class: "chart" });
      body.appendChild(card("Equity (realised, after costs)", ec));
      const lc = lineChart(ec, { height: 240 });
      lc.line("equity", d.equity, "#26a69a");
      charts.push(lc);
    }
    const tg = el("div");
    body.appendChild(card(`Paper trades (${d.trades.length})`, tg));
    if (d.trades.length) {
      const t = (f, h) => txtCol(f, h, { valueFormatter: (p) => (p.value ? new Date(p.value).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", hour12: false }) : "open") });
      grid(tg, [t("entry_ts", "entry"), t("exit_ts", "exit"), txtCol("instrument", "contract", { flex: 2 }), fmtCol("qty", "qty", 0), fmtCol("entry_price", "buy"), fmtCol("exit_price", "sell"),
        txtCol("exit_reason", "exit"), fmtCol("pnl_gross", "gross Rs", 0, { cellClass: signCellClass }), fmtCol("costs", "costs Rs", 0), fmtCol("pnl_net", "net Rs", 0, { cellClass: signCellClass }),
        fmtCol("mfe", "best move"), fmtCol("mae", "worst move"), txtCol("notes", "strategy")], d.trades, { height: Math.min(420, 60 + 27 * d.trades.length) });
    } else tg.appendChild(note("No paper trades yet. The trader acts on 5-minute bar closes between the entry-window times, during market hours, with real captured data."));
    const lg = el("div");
    const rows = d.log.filter((r) => st.filter === "ALL" || r.event === st.filter);
    body.appendChild(card("Detailed log", el("div", { class: "controls" }, select(EVENTS, st.filter, (v) => { st.filter = v; load(); }, "event"),
      el("span", { class: "small" }, `${d.log.length} most recent entries · newest first`)), lg));
    grid(lg, [txtCol("ts", "time", { valueFormatter: (p) => new Date(p.value).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", hour12: false }), maxWidth: 190 }),
      txtCol("event", "event", { maxWidth: 100, cellClass: (p) => (p.value === "EXIT" || p.value === "ENTRY" ? "pos" : p.value === "ERROR" || p.value === "WARN" ? "neg" : "") }),
      txtCol("message", "what happened", { flex: 4, wrapText: true, autoHeight: true }),
      { field: "detail", headerName: "detail", flex: 3, wrapText: true, autoHeight: true, valueFormatter: (p) => summarise(p.value) }], rows, { height: 460 });
    renderBacktest(d);
  }

  function summarise(det) {
    if (!det || !Object.keys(det).length) return "";
    const keep = ["strategy", "direction", "trigger", "pa_score", "score", "status", "reason", "pnl_net", "costs", "mark", "spot", "reasons", "contradictions"];
    return keep.filter((k) => det[k] !== undefined && det[k] !== null && !(Array.isArray(det[k]) && !det[k].length))
      .map((k) => `${k}: ${Array.isArray(det[k]) ? det[k].join("; ") : typeof det[k] === "number" ? num(det[k], 2) : det[k]}`).join(" · ");
  }

  function renderBacktest(d) {
    clear(btBox);
    const bs = d.backtest_status || {};
    const r = d.backtest;
    const out = el("div");
    btBox.appendChild(card("Backtest of these paper rules - trade confidence", el("div", { class: "controls" },
      select([[90, "last 90 days"], [180, "last 180 days"], [365, "last year"], [730, "last 2 years"], [0, "all history"]], st.days, (v) => { st.days = Number(v); }, "history"),
      el("button", { class: "primary", disabled: bs.state === "RUNNING", onclick: async () => { await post("/api/paper/backtest", { days: st.days }); startPoll(); load(); } }, bs.state === "RUNNING" ? "running…" : "Run backtest"),
      el("span", { class: "small" }, bs.state === "ERROR" ? `last run failed: ${bs.error}` : bs.state === "RUNNING" ? `started ${new Date(bs.started).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false })}` : "or run `mmm paper backtest` on your PC")), out));
    if (!r) { out.appendChild(note("No backtest yet.")); return; }
    if (r.demo) out.appendChild(note("This backtest ran on SYNTHETIC demo data - it says nothing about real markets.", "error"));
    const dd = r.direction, os = r.option.stats;
    const verdictTone = (v) => (v === "ACCEPTED" ? "pos" : v === "REJECTED" ? "neg" : "warn");
    out.appendChild(el("h3", {}, `Direction confidence (index moves, ${r.range ? r.range.join(" → ") : ""}, ${r.sessions} sessions)`));
    out.appendChild(dd ? metrics([["Trades", dd.n], ["Win rate", pct(dd.win_rate, 1, true)], ["95% lower bound", pct(dd.win_rate_ci_low, 1, true), "target > 51%", dd.win_rate_ci_low > 0.51 ? "pos" : "neg"],
      ["Expectancy", `Rs ${num(dd.expectancy, 0)}/trade`, "futures-like proxy"], ["Profit factor", dd.profit_factor !== null ? num(dd.profit_factor) : "∞"], ["Verdict", dd.verdict, "", verdictTone(dd.verdict)]]) : note("No trades."));
    if (dd) out.appendChild(bullets(dd.reasons));
    out.appendChild(el("h3", {}, `Option confidence (Rs ${num(d.config.capital, 0)} per trade, nearest expiry, archived option prices)`));
    out.appendChild(os ? metrics([["Trades", os.n, `${r.option.not_evaluated} not evaluated`], ["Win rate", pct(os.win_rate, 1, true)], ["95% lower bound", pct(os.win_rate_ci_low, 1, true), "", os.win_rate_ci_low > 0.51 ? "pos" : "neg"],
      ["Net P&L", inr(os.net_pnl), "", os.net_pnl >= 0 ? "pos" : "neg"], ["Expectancy", `Rs ${num(os.expectancy, 0)}/trade`], ["Max drawdown", inr(os.max_drawdown)], ["Verdict", os.verdict, "", verdictTone(os.verdict)]])
      : note(`No option trades could be replayed (${JSON.stringify(r.option.reasons)}). Option history only exists for contracts archived while active.`, "warn"));
    if (os) out.appendChild(bullets(os.reasons));
    out.appendChild(el("div", { class: "small" }, `Calibrated win probability: ${r.calibration.usable ? `available (${r.calibration.method}, n=${r.calibration.n})` : "not yet - needs ≥200 out-of-sample trades from `mmm research run`"}. Generated ${new Date(r.generated_at).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", hour12: false })}.`));
    out.appendChild(bullets(r.notes));
    if (r.option.trades.length) { const g = el("div"); out.appendChild(g); simpleGrid(g, r.option.trades, { height: 360 }); }
  }

  function startPoll() {
    if (poll) clearInterval(poll);
    poll = setInterval(async () => {
      const d = await api("/api/paper?log_limit=1");
      if (!d.backtest_status || d.backtest_status.state !== "RUNNING") { clearInterval(poll); poll = null; load(); }
    }, 3000);
  }

  await load();
  const timer = setInterval(load, 20000);
  return { onBar: load, unmount: () => { clearInterval(timer); if (poll) clearInterval(poll); if (unsub) unsub(); charts.forEach((c) => c.remove()); } };
}
