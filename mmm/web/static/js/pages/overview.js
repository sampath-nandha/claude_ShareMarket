import { api, candleChart, card, clear, el, fmtCol, grid, human, metrics, note, num, radio, select, signed, txtCol } from "../lib.js";

export async function mount(root, { live }) {
  const st = { tf: "5minute", sessions: 3, instrument: "spot", levels: true, swings: true };
  root.appendChild(el("h2", {}, "Market Overview"));
  root.appendChild(el("div", { class: "subtitle" }, "NIFTY 50 across timeframes with structure, regime and key levels. The forming candle updates on every tick."));
  const head = el("div");
  const controls = el("div", { class: "controls" });
  const chartBox = el("div", { class: "chart" });
  const basis = el("div", { class: "small" });
  const lower = el("div", { class: "row" });
  root.append(head, controls, chartBox, basis, lower);
  const chart = candleChart(chartBox, { height: 520, volume: true });
  let unsub = null;

  controls.append(
    radio([["spot", "Spot (NIFTY 50)"], ["fut", "Futures (near month)"], ["overlay", "Spot + futures overlay"]], st.instrument, (v) => { st.instrument = v; load(); }),
    radio(["minute", "3minute", "5minute", "15minute", "30minute", "60minute", "day"], st.tf, (v) => { st.tf = v; load(); }),
    select([1, 2, 3, 5, 10, 20].map((n) => [n, `${n} session${n > 1 ? "s" : ""}`]), st.sessions, (v) => { st.sessions = Number(v); load(); }),
    el("label", { class: "flex" }, el("input", { type: "checkbox", checked: true, onchange: (e) => { st.levels = e.target.checked; load(); } }), "levels"),
    el("label", { class: "flex" }, el("input", { type: "checkbox", checked: true, onchange: (e) => { st.swings = e.target.checked; load(); } }), "swings"),
  );

  async function load() {
    let d;
    try { d = await api(`/api/overview?tf=${st.tf}&sessions=${st.sessions}&instrument=${st.instrument}`); } catch (e) { clear(head).appendChild(note(e.message, "error")); return; }
    const m = d.metrics;
    clear(head).appendChild(metrics([
      ["Spot", num(m.spot)], ["Regime", m.direction, `${m.strength} · ${m.volatility} vol`], ["Behaviour", human(m.behaviour)],
      ["ATR (5m)", num(m.atr, 1)], ["Setup TF", m.setup_structure, m.setup_event || ""], ["Context TF", m.context_structure, m.context_event || ""],
    ]));
    chart.setBars(d.bars, st.tf);
    chart.line("EMA20", d.ema20, "#ffb74d");
    chart.line("EMA50", d.ema50, "#64b5f6");
    chart.line("futures", d.overlay || [], "#ce93d8", 2);
    chart.levels(st.levels ? d.levels.filter((l) => !(l.kind === "ROUND" && l.strength < 40)) : []);
    chart.swings(st.swings ? d.swings : []);
    basis.textContent = d.basis ? `${d.basis.symbol}: ${num(d.basis.last)} · basis ${signed(d.basis.basis, 1)} pts (${signed(d.basis.basis_pct)}%) · futures carry volume/OI, the index does not` : "";
    if (d.instrument === "fut") basis.textContent += " · Levels are computed on the index and are not drawn on the futures chart.";
    clear(lower);
    const s = el("div"), o = el("div"), sr = el("div");
    lower.append(el("div", { class: "col" }, card("Multi-timeframe structure", s), card("Opening range", o)), el("div", { class: "col" }, card("Support / resistance", sr)));
    grid(s, [txtCol("timeframe", "TF"), txtCol("trend", "trend"), txtCol("last_event", "last event"), fmtCol("last_swing_high", "swing H", 0),
      fmtCol("last_swing_low", "swing L", 0), txtCol("labels", "recent labels", { flex: 2 })], d.structure);
    o.append(...(d.opening_ranges.length ? d.opening_ranges.map((r) => el("div", {}, `OR${r.minutes}: ${num(r.low, 0)} – ${num(r.high, 0)} (width ${num(r.width, 0)}) ${r.complete ? "✓" : "⏳ forming"}`)) : [note("session not started")]));
    grid(sr, [txtCol("side", "side", { maxWidth: 70 }), txtCol("kind", "kind"), fmtCol("price", "price", 0), fmtCol("strength", "strength", 0), fmtCol("touches", "touches", 0), txtCol("tf", "tf")],
      d.sr_table, { height: 440 });
    subscribeLive();
  }

  function subscribeLive() {
    if (unsub) unsub();
    const s = live.status();
    const tok = st.instrument === "fut" ? s?.tokens?.futures?.[0]?.token : s?.tokens?.index;
    if (!tok) return;
    unsub = live.subscribe(tok, (t) => chart.tick(t.p, t.x || t.r || Date.now()));
  }

  await load();
  return { onBar: load, unmount: () => { if (unsub) unsub(); chart.remove(); } };
}
