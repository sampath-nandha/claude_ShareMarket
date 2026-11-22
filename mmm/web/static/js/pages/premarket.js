import { api, candleChart, card, clear, el, fmtCol, grid, human, metrics, note, num, pct, row, signed, txtCol } from "../lib.js";

export async function mount(root, { live }) {
  root.appendChild(el("h2", {}, "Pre-Market & Opening"));
  root.appendChild(el("div", { class: "subtitle" }, "Level sheet for today (pivots, CPR, confluence clusters) and the live opening read: gap, opening type, initial balance, pre-open auction."));
  const body = el("div");
  root.appendChild(body);
  let chart = null, unsub = null;

  async function load() {
    let d;
    try { d = await api("/api/premarket"); } catch (e) { clear(body).appendChild(note(e.message, "error")); return; }
    clear(body);
    const ps = d.previous_session, c = d.cpr, g = d.gap;
    body.appendChild(metrics([
      ["Previous close", num(ps?.close), ps ? `${ps.date} · H ${num(ps.high, 0)} L ${num(ps.low, 0)}` : ""],
      ["CPR", c ? `${num(c.bc, 0)} – ${num(c.tc, 0)}` : "–", c ? `${c.width_class} · width ${num(c.width_pct, 2)}% · rank ${pct(c.width_rank, 0, true)}` : "", c?.width_class === "NARROW" ? "pos" : ""],
      ["Gap", g ? human(g.kind) : "–", g ? `${signed(g.points, 0)} pts · ${signed(g.atr_multiple, 2)} daily ATR${g.filled ? " · filled" : ""}` : ""],
      ["Opening 15m", d.open15 ? human(d.open15.code) : "–", d.open15 ? (d.open15.direction > 0 ? "up" : d.open15.direction < 0 ? "down" : "two-sided") : ""],
      ["Opening 30m", d.open30 ? human(d.open30.code) : "–", d.open30 ? (d.open30.direction > 0 ? "up" : d.open30.direction < 0 ? "down" : "two-sided") : ""],
      ["IB 30m", d.ib30 ? `${num(d.ib30.low, 0)} – ${num(d.ib30.high, 0)}` : "–", d.ib30 ? human(d.ib30.status) : ""],
      ["IB 60m", d.ib60 ? `${num(d.ib60.low, 0)} – ${num(d.ib60.high, 0)}` : "–", d.ib60 ? `${human(d.ib60.status)} · ext ↑${num(d.ib60.ext_up, 2)} ↓${num(d.ib60.ext_down, 2)}` : ""],
    ]));
    const p = d.preopen || {};
    body.appendChild(card("Pre-open call auction (09:00–09:08)", p.available
      ? metrics([["Index pre-open", num(p.index_price)], ["vs previous close", signed(p.gap_points, 1)],
        ["Order imbalance", signed(p.imbalance, 2), `buy ${num(p.buy_qty, 0)} · sell ${num(p.sell_qty, 0)} · ${p.constituents_seen} stocks`]])
      : note(p.note || "not available", "warn"), p.note && p.available ? el("div", { class: "small" }, p.note) : null));
    const chartBox = el("div", { class: "chart" });
    body.appendChild(card("Today with the level sheet", chartBox));
    if (chart) chart.remove();
    chart = candleChart(chartBox, { height: 420 });
    chart.setBars(d.bars, "minute");
    chart.levels(d.levels.map((l) => ({ ...l, strength: 70 })));
    const sheet = el("div"), clus = el("div"), piv = el("div"), feats = el("div");
    body.appendChild(row(card("Level sheet (resistance above, support below)", sheet), el("div", {}, card("Confluence clusters", clus), card("Floor pivots", piv))));
    grid(sheet, [txtCol("side", "side", { maxWidth: 70 }), txtCol("kind", "kind"), fmtCol("price", "price", 0), fmtCol("distance", "from spot", 0, { cellClass: (q) => (q.value > 0 ? "neg" : "pos") }),
      fmtCol("strength", "strength", 0), txtCol("tags", "tags", { flex: 2 })], d.level_sheet, { height: 480 });
    grid(clus, [fmtCol("price", "price", 0), fmtCol("low", "low", 0), fmtCol("high", "high", 0), fmtCol("score", "score", 0),
      { field: "members", headerName: "members", flex: 3, valueFormatter: (q) => (q.value || []).join(" · ") }], d.clusters);
    grid(piv, [txtCol("k", "level"), fmtCol("v", "price", 0)], Object.entries(d.pivots || {}).map(([k, v]) => ({ k, v })));
    body.appendChild(card("Opening features (Phase-4 validation status)", feats));
    grid(feats, [txtCol("key", "feature"), txtCol("description", "hypothesis", { flex: 4 }), txtCol("status", "status"),
      { field: "active", headerName: "active now", valueFormatter: (q) => (q.value === null || q.value === undefined ? "–" : q.value > 0 ? "↑ up" : q.value < 0 ? "↓ down" : "no") }],
    d.features.filter((f) => f.phase === 1));
    body.appendChild(note("Only features marked PASS by `mmm research features` change a signal's score. Everything else here is information.", "info"));
    if (unsub) unsub();
    const tok = live.status()?.tokens?.index;
    if (tok) unsub = live.subscribe(tok, (t) => chart.tick(t.p, t.x || t.r || Date.now()));
  }
  await load();
  return { onBar: load, unmount: () => { if (unsub) unsub(); if (chart) chart.remove(); } };
}
