import { api, card, clear, el, fmtCol, grid, inr, input, intCol, metrics, note, num, pct, post, radio, select, signCellClass, simpleGrid, toast, txtCol } from "../lib.js";

export async function mount(root) {
  const st = { mode: "ALL" };
  root.appendChild(el("h2", {}, "Risk & Journal"));
  root.appendChild(el("div", { class: "subtitle" }, "Read-only Zerodha account view, paper/manual journal and limit utilisation."));
  const acct = el("div"), body = el("div");
  root.append(card("Zerodha account (read-only)", acct), el("div", { class: "controls" }, radio(["ALL", "PAPER", "MANUAL"], st.mode, (v) => { st.mode = v; load(); })), body);

  async function loadAccount() {
    clear(acct).appendChild(el("div", { class: "loading" }, "loading account…"));
    try {
      const a = await api("/api/account");
      clear(acct);
      if (!a.available) { acct.appendChild(note(a.note, "warn")); return; }
      acct.appendChild(metrics([["Net", inr(a.net)], ["Available cash", inr(a.cash)], ["Live balance", inr(a.live_balance)], ["Utilised", inr(a.utilised)]]));
      if (a.positions.length) { const g = el("div"); acct.appendChild(g); simpleGrid(g, a.positions); } else acct.appendChild(el("div", {}, "No open positions."));
      const rec = el("div");
      acct.append(el("button", { onclick: async () => {
        const r = await api("/api/account/reconcile");
        clear(rec);
        if (!r.available) { rec.appendChild(note(r.note, "warn")); return; }
        Object.entries(r).filter(([k]) => k !== "available").forEach(([k, rows]) => { rec.appendChild(el("b", {}, `${k} (${rows.length})`)); if (rows.length) { const g = el("div"); rec.appendChild(g); simpleGrid(g, rows); } });
      } }, "Reconcile MANUAL journal vs broker fills (today)"), rec);
    } catch (e) { clear(acct).appendChild(note(e.message, "error")); }
  }

  async function load() {
    let d;
    try { d = await api(`/api/risk?mode=${st.mode}`); } catch (e) { clear(body).appendChild(note(e.message, "error")); return; }
    clear(body);
    const s = d.summary;
    body.appendChild(metrics([["Closed", s.closed || 0], ["Open", s.open || 0], ["Win rate", s.win_rate !== null && s.win_rate !== undefined ? pct(s.win_rate, 1, true) : "–"],
      ["Net P&L", inr(s.net_pnl || 0)], ["Costs", inr(s.costs || 0)], ["Avg win / loss", `${num(s.avg_win || 0, 0)} / ${num(s.avg_loss || 0, 0)}`]]));
    const g = el("div");
    body.appendChild(card("Journal", g));
    grid(g, [txtCol("trade_id", "id"), txtCol("mode", "mode"), txtCol("created_at", "created", { valueFormatter: (p) => new Date(p.value).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", hour12: false }) }),
      txtCol("instrument", "instrument", { flex: 2 }), txtCol("direction", "dir"), intCol("qty", "qty"), fmtCol("entry_price", "entry"), fmtCol("exit_price", "exit"), txtCol("exit_reason", "reason"),
      fmtCol("pnl_net", "net ₹", 0, { cellClass: signCellClass }), fmtCol("mfe", "MFE"), fmtCol("mae", "MAE"), txtCol("signal_id", "signal")], d.trades, { height: 360 });
    if (d.open.length) {
      const f = { trade_id: d.open[0], price: 0, reason: "MANUAL" };
      body.appendChild(card("Close an open trade (records the exit you made manually / paper exit)",
        el("div", { class: "controls" }, select(d.open, f.trade_id, (v) => { f.trade_id = v; }, "trade"), input("number", 0, (v) => { f.price = v; }, "exit price", { step: 0.05 }),
          select(["TARGET1", "TARGET2", "STOP", "TIME", "MANUAL", "SESSION_END"], f.reason, (v) => { f.reason = v; }, "reason"),
          el("button", { class: "primary", onclick: async () => { try { const t = await post("/api/journal/close", f); toast(`Closed ${f.trade_id}: net ₹${num(t.pnl_net, 0)}`, "ok"); load(); } catch (e) { toast(e.message, "error"); } } }, "Close"))));
    }
    const L = d.limits, u = L.utilisation;
    const bars = el("div");
    body.appendChild(card("Risk limits", metrics([["Daily loss used", pct(u.daily_loss_pct, 0), `₹${num(L.daily_pnl, 0)}`], ["Recommendations", pct(u.recommendations_pct, 0), `${L.recommendations_today}/${L.config.max_daily_recommendations}`],
      ["Open positions", pct(u.positions_pct, 0), `${L.open_positions}/${L.config.max_concurrent_positions}`], ["Consecutive losses", pct(u.consecutive_losses_pct, 0), `${L.consecutive_losses}/${L.config.max_consecutive_losses}`]]), bars,
      L.blocked.length ? note(`New recommendations are currently blocked: ${L.blocked.join("; ")}`, "error") : null,
      el("details", {}, el("summary", {}, "Configured limits (config/settings.yaml → risk)"), el("pre", { class: "json" }, JSON.stringify(L.config, null, 2)))));
    Object.entries(u).forEach(([k, v]) => bars.appendChild(el("div", { class: "flex" }, el("span", { style: { width: "200px" } }, k), el("div", { class: "bar", style: { flex: 1 } }, el("span", { style: { width: `${Math.min(100, v)}%` } })))));
  }
  loadAccount();
  await load();
  return { onBar: load };
}
