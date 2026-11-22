import { api, card, clear, el, fmtCol, grid, metrics, note, pct, signCellClass, signed, txtCol } from "../lib.js";

export async function mount(root) {
  root.appendChild(el("h2", {}, "Constituent Breadth"));
  root.appendChild(el("div", { class: "subtitle" }, "EQUAL-WEIGHT basket statistics (Kite does not publish index weights)."));
  const body = el("div");
  root.appendChild(body);
  async function load() {
    let d;
    try { d = await api("/api/breadth"); } catch (e) { clear(body).appendChild(note(e.message, "error")); return; }
    clear(body);
    if (!d.available) { body.appendChild(note(d.note, "warn")); return; }
    const s = d.summary;
    body.appendChild(metrics([["Coverage", pct(s.coverage_pct, 0), s.authoritative ? "authoritative" : "NOT authoritative", s.authoritative ? "pos" : "warn"],
      ["Advancing", s.advancing], ["Declining", s.declining], ["% above prev close", pct(s.pct_above_prev_close, 0)], ["% above open", pct(s.pct_above_open, 0)],
      ["Mean / median ret", s.mean_return_pct !== null ? `${signed(s.mean_return_pct)}% / ${signed(s.median_return_pct)}%` : "–"], ["Breadth score", signed(s.score)]]));
    if (!s.authoritative) body.appendChild(note("Coverage below 80% - breadth is shown but excluded from signal scoring.", "warn"));
    const bars = el("div");
    body.appendChild(card("Return versus previous close (%)", bars));
    const rows = d.table.slice().sort((a, b) => b.ret_pct - a.ret_pct);
    const max = Math.max(0.01, ...rows.map((r) => Math.abs(r.ret_pct)));
    rows.forEach((r) => bars.appendChild(el("div", { class: "flex", style: { margin: "1px 0" } },
      el("span", { style: { width: "110px", display: "inline-block" } }, r.tradingsymbol),
      el("div", { style: { flex: 1, position: "relative", height: "12px", background: "#161b22" } },
        el("div", { style: { position: "absolute", left: r.ret_pct >= 0 ? "50%" : `${50 - (Math.abs(r.ret_pct) / max) * 50}%`, width: `${(Math.abs(r.ret_pct) / max) * 50}%`, height: "100%", background: r.ret_pct >= 0 ? "#26a69a" : "#ef5350" } })),
      el("span", { class: r.ret_pct >= 0 ? "pos" : "neg", style: { width: "60px", textAlign: "right" } }, `${signed(r.ret_pct)}%`))));
    body.appendChild(el("div", { class: "small" }, `% at day high ${pct(s.pct_at_day_high, 0)} · % at day low ${pct(s.pct_at_day_low, 0)} · dispersion ${pct(s.dispersion_pct, 2)}`));
    const g = el("div");
    body.appendChild(card("Constituents", g));
    grid(g, [txtCol("tradingsymbol", "symbol"), fmtCol("last_price", "LTP"), fmtCol("close", "prev close"), fmtCol("ret_pct", "ret %", 2, { cellClass: signCellClass }),
      fmtCol("from_open_pct", "from open %", 2, { cellClass: signCellClass })], d.table, { height: 420 });
  }
  await load();
  return { onBar: load };
}
