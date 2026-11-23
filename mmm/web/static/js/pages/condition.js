import { api, card, clear, el, fmtCol, grid, lineChart, note, radio, txtCol } from "../lib.js";
import { gaugeRow, stateChip } from "../widgets/gauges.js";

export async function mount(root) {
  const st = { horizon: "intraday" };
  root.appendChild(el("h2", {}, "Market Condition"));
  root.appendChild(el("div", { class: "subtitle" }, "How bullish, bearish and sideways the market is right now. Bullish is strongest at the right, bearish at the left, sideways at the centre. Descriptive, not a forecast."));
  const head = el("div", { class: "controls" }), body = el("div");
  root.append(head, body);
  let chart = null;
  head.append(radio([["intraday", "Intraday (today)"], ["60minute", "60-minute"], ["day", "Daily"]], st.horizon, (v) => { st.horizon = v; load(); }));
  const chip = el("span");
  head.append(chip);

  async function load() {
    let d;
    try { d = await api(`/api/condition?horizon=${st.horizon}&with_path=${st.horizon === "intraday"}`); } catch (e) { clear(body).appendChild(note(e.message, "error")); return; }
    clear(chip).append(stateChip(d), el("span", { class: "small" }, `  as of ${d.as_of_text} IST · direction ${d.direction > 0 ? "+" : ""}${Math.round(d.direction)} · sideways ${Math.round(d.sideways)}`));
    if (chart) { chart.remove(); chart = null; }
    clear(body);
    body.appendChild(gaugeRow(d));
    if (d.chips.length) body.appendChild(el("div", { class: "flex", style: { marginBottom: "12px" } }, d.chips.map((c) => el("span", { class: `pill ${c.tone === "ok" ? "ok" : c.tone === "warn" ? "warn" : ""}` }, c.text))));
    if (d.path && d.path.bullish) {
      const box = el("div", { class: "chart" });
      body.appendChild(card("Today so far (every 15 minutes)", box));
      chart = lineChart(box, { height: 240 });
      chart.line("bullish", d.path.bullish, "#26a69a"); chart.line("bearish", d.path.bearish, "#ef5350"); chart.line("sideways", d.path.sideways, "#f0b429");
    }
    const g = el("div");
    body.appendChild(card("What drives the gauges", g, el("div", { class: "small" }, "Direction components score -1 (bearish) to +1 (bullish); sideways components score 0 to 1. Components marked n/a are left out and the weights re-normalised.")));
    grid(g, [txtCol("group", "group", { maxWidth: 110 }), txtCol("name", "component"), fmtCol("value", "value", 2, { cellClass: (p) => (p.data.group === "direction" ? (p.value > 0 ? "pos" : p.value < 0 ? "neg" : "") : "") }),
      fmtCol("weight", "weight", 2), txtCol("text", "reading", { flex: 3 })], d.components);
    d.notes.forEach((n) => body.appendChild(el("div", { class: "small" }, n)));
  }
  await load();
  return { onBar: load, unmount: () => { if (chart) chart.remove(); } };
}
