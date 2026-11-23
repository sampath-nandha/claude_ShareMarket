import { api, bullets, candleChart, card, clear, el, fmtCol, grid, metrics, note, num, row, txtCol } from "../lib.js";

export async function mount(root, { live }) {
  root.appendChild(el("h2", {}, "Price Action"));
  root.appendChild(el("div", { class: "subtitle" }, "Swings, structure events, zones, candle behaviour and Stage-1 setup candidates on the latest completed 5-minute bar."));
  const body = el("div");
  root.appendChild(body);
  let chart = null, unsub = null;

  async function load() {
    let d;
    try { d = await api("/api/priceaction"); } catch (e) { clear(body).appendChild(note(e.message, "error")); return; }
    clear(body);
    const s = d.structure;
    body.appendChild(metrics([["Setup TF", s.setup[0], s.setup[1] || ""], ["Context TF", s.context[0], s.context[1] || ""], ["Trend TF", s.trend[0], s.trend[1] || ""],
      ["Regime", s.regime], ["Candidates", d.candidates.length, d.candidates.length ? "" : "NO TRADE is the default"]]));
    const box = el("div", { class: "chart" });
    body.appendChild(card("Setup timeframe (5m) with confirmed swings and zones", box, el("div", { class: "small" }, `labels: ${s.setup[2]}`)));
    if (chart) chart.remove();
    chart = candleChart(box, { height: 520 });
    chart.setBars(d.bars, "5minute");
    chart.levels(d.levels);
    const swingMarks = d.swings.map((x) => ({ time: x.time, position: x.kind === "H" ? "aboveBar" : "belowBar", color: x.kind === "H" ? "#ef5350" : "#26a69a",
      shape: x.kind === "H" ? "arrowDown" : "arrowUp", text: x.label || "" }));
    const pat = d.patterns || {};
    let showPat = true;
    try { showPat = localStorage.getItem("mmm.showPatterns") !== "0"; } catch (_e) { /* storage unavailable */ }
    const lastDay = d.bars.length ? Math.floor(d.bars[d.bars.length - 1].time / 86400) : null;
    const patMarks = () => (pat.enabled && showPat ? pat.markers.filter((m) => Math.floor(m.time / 86400) === lastDay).map((m) => ({
      time: m.time, position: m.direction < 0 ? "aboveBar" : "belowBar", color: m.color, shape: "circle", size: 0.6, text: m.family === "indecision" ? "" : m.text })) : []);
    chart.markers([...swingMarks, ...patMarks()]);
    if (pat.enabled) {
      box.parentNode.insertBefore(el("label", { class: "flex small" }, el("input", { type: "checkbox", checked: showPat, onchange: (e) => {
        showPat = e.target.checked;
        try { localStorage.setItem("mmm.showPatterns", showPat ? "1" : "0"); } catch (_e) { /* ignore */ }
        chart.markers([...swingMarks, ...patMarks()]);
      } }), "show candlestick patterns (latest session)"), box);
    }
    d.candidates.forEach((c) => {
      chart.priceLine(c.entry, "#ffffff", `${c.strategy} entry`);
      chart.priceLine(c.stop, "#ef5350", "stop");
      chart.priceLine(c.target1, "#26a69a", "T1");
      chart.priceLine(c.target2, "#66bb6a", "T2");
    });
    const sw = el("div"), an = el("div");
    body.appendChild(row(card("Recent swings", sw), card("Last candles (anatomy - context only, never a trigger)", an)));
    grid(sw, [txtCol("ts", "time", { valueFormatter: (q) => new Date(q.value).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", hour12: false }) }), txtCol("kind", "kind"), txtCol("label", "label"),
      fmtCol("price", "price", 0), fmtCol("confirmed_bars_later", "confirmed +bars", 0)], d.swing_table);
    grid(an, [txtCol("ts", "time", { valueFormatter: (q) => new Date(q.value).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false }) }), fmtCol("close", "close", 0),
      fmtCol("body_ratio", "body"), fmtCol("upper_wick", "upper wick"), fmtCol("lower_wick", "lower wick"), fmtCol("close_loc", "close loc"), txtCol("patterns", "patterns", { flex: 2 })], d.anatomy);
    if (pat.enabled) {
      const pg = el("div");
      body.appendChild(card(`Candlestick patterns on the decision bar (trial since ${pat.observe_from})`, pg,
        el("div", { class: "small" }, "Chart dots (latest session): orange momentum/continuation · purple reversal · grey indecision (unlabelled). A pattern only affects a signal's score after it PASSES the feature study; until then it is shown here and listed as 'not scored'.")));
      if (pat.latest.length) {
        const dir = (v) => (v > 0 ? "↑ up" : v < 0 ? "↓ down" : "–");
        grid(pg, [txtCol("pattern", "pattern"), txtCol("family", "family"), { field: "raw_direction", headerName: "pattern says", valueFormatter: (q) => dir(q.value) },
          { field: "in_context_direction", headerName: "in its context", valueFormatter: (q) => (q.value ? dir(q.value) : "out of context") },
          txtCol("status", "validation", { cellClass: (q) => (q.value === "PASS" ? "pos" : q.value === "FAIL" ? "neg" : "") }), txtCol("description", "definition", { flex: 4 })], pat.latest);
      } else pg.appendChild(note("No pattern on the latest completed 5-minute bar."));
    }
    const cands = el("div");
    body.appendChild(card(`Stage-1 setup candidates (${d.candidates.length})`, cands));
    if (!d.candidates.length) cands.appendChild(note("No strategy found a valid price-action setup on the decision bar. NO TRADE is the default."));
    d.candidates.forEach((c) => cands.appendChild(el("div", { class: "card" },
      el("h3", {}, `${c.strategy} ${c.name} · ${c.direction} · ${c.trigger_event} · PA score ${num(c.pa_score, 0)} · R:R ${num(c.reward_risk)}`),
      metrics([["Entry", num(c.entry, 0)], ["Stop", num(c.stop, 0)], ["T1", num(c.target1, 0)], ["T2", num(c.target2, 0)], ["Risk (pts)", num(c.risk, 0)], ["Status", c.status]]),
      el("b", {}, "Evidence"), bullets(c.evidence), c.contradictions.length ? el("b", {}, "Contradictions") : null, bullets(c.contradictions),
      el("div", { class: "small" }, `Invalidation: ${c.invalidation} · expires after ${c.expires_after_bars} bars`))));
    const lib = el("div");
    body.appendChild(card("Strategy library", lib));
    grid(lib, [txtCol("id", "id", { maxWidth: 80 }), txtCol("name", "name"), txtCol("version", "ver", { maxWidth: 80 }), txtCol("status", "status"),
      txtCol("eligible_regimes", "regimes"), txtCol("doc", "rule", { flex: 5, wrapText: true, autoHeight: true })], d.library);
    if (unsub) unsub();
    const tok = live.status()?.tokens?.index;
    if (tok) unsub = live.subscribe(tok, (t) => chart.tick(t.p, t.x || t.r || Date.now()));
  }
  await load();
  return { onBar: load, unmount: () => { if (unsub) unsub(); if (chart) chart.remove(); } };
}
