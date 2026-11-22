import { api, bullets, candleChart, card, clear, el, grid, human, input, metrics, note, num, post, row, select, signed, toast, txtCol, fmtCol } from "../lib.js";

export async function mount(root, { live }) {
  root.appendChild(el("h2", {}, "Live Signals"));
  root.appendChild(el("div", { class: "subtitle" }, "Stage 1 price action → Stage 2 confirmation (validated features only) → risk gate → option selection. NO TRADE is the default."));
  const body = el("div");
  root.appendChild(body);
  let charts = [], unsubs = [];

  async function load() {
    let d;
    try { d = await api("/api/signals"); } catch (e) { clear(body).appendChild(note(e.message, "error")); return; }
    charts.forEach((c) => c.remove()); charts = []; unsubs.forEach((u) => u()); unsubs = [];
    clear(body);
    body.appendChild(el("h3", {}, `Decision bar ${new Date(d.decision_bar).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false })} · regime ${d.regime} · ${d.signals.length} signal(s)`));
    if (!d.signals.length) body.appendChild(note("NO TRADE. No strategy found a valid Stage-1 setup on the latest completed 5-minute bar."));
    for (const s of d.signals) {
      const box = el("div", { class: "chart" });
      const ro = s.recommended_option;
      const form = { mode: "PAPER", instrument: ro?.tradingsymbol || "NIFTY", qty: 75, price: ro?.premium ?? s.entry };
      const conf = [];
      if (s.futures_confirmation) {
        const f = s.futures_confirmation;
        conf.push(`Futures ${signed(f.confirmation_score)} · ${human(f.price_oi_label)} · basis ${num(f.basis, 1)}${f.premium_vs_fair !== undefined ? ` · premium vs fair ${signed(f.premium_vs_fair, 1)}` : ""}${f.divergence ? ` · ${f.divergence} divergence` : ""}`);
      }
      if (s.options_confirmation) {
        const o = s.options_confirmation;
        conf.push(`Options ${signed(o.confirmation_score)} · PCR ${num(o.pcr_oi)} · max pain ${num(o.max_pain, 0)} · walls ${num(o.put_wall, 0)}/${num(o.call_wall, 0)}${o.pcr_near_trend ? ` · near-ATM PCR ${human(o.pcr_near_trend)}` : ""}`);
      }
      if (s.breadth_confirmation) {
        const b = s.breadth_confirmation;
        conf.push(`Breadth ${signed(b.score)} (${b.advancing}↑ ${b.declining}↓, coverage ${num(b.coverage_pct, 0)}%${b.authoritative ? "" : ", not authoritative"})`);
      }
      const c = el("div", { class: `card signal ${s.status}` },
        el("h3", {}, `${human(s.status).toUpperCase()} · ${s.direction} · ${s.strategy_id} · ${s.trigger_event}`),
        metrics([["Score", `${num(s.score, 0)}/100`], ["Entry", num(s.entry, 0)], ["Stop", num(s.stop, 0)], ["T1 / T2", `${num(s.target1, 0)} / ${num(s.target2, 0)}`], ["R:R", num(s.reward_risk)],
          ["Win probability", s.win_probability !== null ? num(s.win_probability, 3) : "n/a", s.win_probability_note]]),
        row(el("div", {}, el("b", {}, "Evidence"), bullets(s.evidence), el("b", {}, "Contradictions"), bullets(s.contradictions.length ? s.contradictions : ["none"]),
          el("div", { class: "small" }, `Invalidation: ${s.invalidation} · expires: ${s.expiry_conditions} · data quality: ${s.data_quality}`),
          el("div", { class: "small" }, "Component scores: " + Object.entries(s.component_scores).map(([k, v]) => `${k} ${v}`).join(", "))),
        el("div", {}, ro ? el("div", {}, el("b", {}, "Recommended option: "), `${ro.tradingsymbol} @ ${num(ro.premium)} · Δ ${num(ro.delta)} · IV ${num(ro.iv * 100, 1)}% · spread ${num(ro.spread_pct)}% · liq ${num(ro.liquidity_score, 0)}`,
          el("div", { class: "small" }, `Premium at T1 ≈ ${num(ro.expected_premium_at_target, 1)} · at stop ≈ ${num(ro.expected_premium_at_stop, 1)} · premium R:R ${num(ro.reward_risk_premium)}`),
          el("div", {}, `Suggested size: ${s.suggested_lots || 0} lot(s), max premium risk ≈ ₹${num(s.suggested_max_risk_rupees || 0, 0)}`)) : el("div", {}, el("b", {}, "Recommended option: "), "NO SUITABLE OPTION"),
        bullets(conf), s.do_not_execute_reasons.length ? note(el("div", {}, el("b", {}, "Do not execute because:"), bullets(s.do_not_execute_reasons)), "error") : note("Risk gate passed.", "ok"))),
        box,
        el("div", { class: "controls" }, el("span", { class: "small" }, "This app never sends orders. If you execute manually in Kite, record it so the journal can reconcile."),
          select(["PAPER", "MANUAL"], form.mode, (v) => { form.mode = v; }, "mode"), input("text", form.instrument, (v) => { form.instrument = v; }, "instrument"),
          input("number", form.qty, (v) => { form.qty = v; }, "qty", { step: 75, min: 75 }), input("number", form.price, (v) => { form.price = v; }, "fill price", { step: 0.05 }),
          el("button", { class: "primary", onclick: async () => {
            try { const r = await post("/api/journal/open", { ...form, signal_id: s.signal_id, stop: s.stop, target1: s.target1, target2: s.target2 }); toast(`Recorded ${form.mode} trade ${r.trade_id}`, "ok"); } catch (e) { toast(e.message, "error"); }
          } }, "Record")));
      body.appendChild(c);
      const ch = candleChart(box, { height: 320 });
      ch.setBars(d.bars, "5minute");
      ch.priceLine(s.entry, "#ffffff", "entry"); ch.priceLine(s.stop, "#ef5350", "stop"); ch.priceLine(s.target1, "#26a69a", "T1"); ch.priceLine(s.target2, "#66bb6a", "T2");
      charts.push(ch);
      const tok = live.status()?.tokens?.index;
      if (tok) unsubs.push(live.subscribe(tok, (t) => ch.tick(t.p, t.x || t.r || Date.now())));
    }
    const g = el("div");
    body.appendChild(card("Recent signals (state DB)", g));
    grid(g, [txtCol("created_at", "created", { valueFormatter: (p) => new Date(p.value).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", hour12: false }) }), txtCol("strategy_id", "strategy"),
      txtCol("direction", "dir"), txtCol("status", "status"), fmtCol("score", "score", 0), fmtCol("entry", "entry", 0), fmtCol("stop", "stop", 0), fmtCol("target1", "T1", 0), txtCol("regime", "regime", { flex: 2 })],
    d.recent, { height: 320 });
  }
  await load();
  return { onBar: load, unmount: () => { charts.forEach((c) => c.remove()); unsubs.forEach((u) => u()); } };
}
