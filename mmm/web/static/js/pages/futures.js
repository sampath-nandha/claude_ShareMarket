import { api, bullets, candleChart, card, clear, el, grid, human, lineChart, metrics, note, num, radio, row, signed, txtCol } from "../lib.js";

export async function mount(root, { live }) {
  const st = { direction: "BULLISH" };
  root.appendChild(el("h2", {}, "Futures & Spot-Futures Parity"));
  root.appendChild(el("div", { class: "subtitle" }, "Near-month futures: basis versus fair value, premium z-score, divergence, OI, VWAP and depth (all parity values DERIVED)."));
  const controls = el("div", { class: "controls" }, el("span", { class: "small" }, "Evaluate confirmation for a hypothetical"),
    radio(["BULLISH", "BEARISH"], st.direction, (v) => { st.direction = v; load(); }));
  const body = el("div");
  root.append(controls, body);
  let charts = [], unsub = null;

  async function load() {
    let d;
    try { d = await api(`/api/futures?direction=${st.direction}`); } catch (e) { clear(body).appendChild(note(e.message, "error")); return; }
    charts.forEach((c) => c.remove()); charts = [];
    clear(body);
    const s = d.snapshot;
    if (!s) { body.appendChild(note("No futures quote captured yet (collector not running).", "warn")); return; }
    body.appendChild(metrics([
      ["Futures", num(s.last), s.basis !== null ? `basis ${signed(s.basis, 1)} (${signed(s.basis_pct)}%)` : ""],
      ["OI", num(s.oi, 0), s.oi_change !== null ? `${signed(s.oi_change, 0)} (${signed(s.oi_change_pct, 1)}%)` : ""],
      ["Volume", num(s.volume, 0), s.rel_volume ? `rel ${num(s.rel_volume)}x` : ""], ["Price/OI label", human(s.price_oi_label)],
      ["VWAP (approx)", num(s.vwap)], ["Confirmation", signed(s.confirmation_score), st.direction], ["Spread", s.spread !== null ? `${num(s.spread)} (${num(s.spread_bps, 1)} bps)` : "–"],
      ["Depth imbalance", signed(s.depth_imbalance)],
    ]));
    const b = d.basis_state;
    if (b) {
      body.appendChild(metrics([
        ["Premium vs fair", signed(b.premium, 1), `${signed(b.premium_bps, 1)} bps · fair ${num(b.fair, 1)}`, b.premium > 0 ? "pos" : "neg"],
        ["Premium z (2 sessions)", signed(b.z), b.z !== null && Math.abs(b.z) >= 2 ? "extreme" : ""], ["Premium change 30m", signed(b.premium_change_30, 1)],
        ["Futures OI change 30m", b.oi_change_30_pct !== null ? `${signed(b.oi_change_30_pct, 2)}%` : "–"],
        ["Divergence", b.divergence ? b.divergence.kind : "none", b.divergence ? `strength ${num(b.divergence.strength)}` : "", b.divergence ? (b.divergence.kind === "BEARISH" ? "neg" : "pos") : ""],
        ["Expiry", b.expiry],
      ]));
      if (b.divergence) body.appendChild(card("Divergence evidence", bullets(b.divergence.evidence)));
    }
    if (d.series) {
      const c1 = el("div", { class: "chart" }), c2 = el("div", { class: "chart" }), c3 = el("div", { class: "chart" });
      body.appendChild(row(card("Basis, fair value and premium (pts)", c1), card("Premium z-score", c2)));
      const lc = lineChart(c1, { height: 280 });
      lc.line("basis", d.series.basis, "#ce93d8"); lc.line("fair value", d.series.fair, "#90a4ae", { lineStyle: 2 });
      const prem = lc.line("premium", d.series.premium, "#ffd54f");
      prem.setMarkers((d.divergences || []).map((m) => ({ time: m.time, position: m.kind === "BEARISH" ? "aboveBar" : "belowBar", color: m.kind === "BEARISH" ? "#ef5350" : "#26a69a",
        shape: m.kind === "BEARISH" ? "arrowDown" : "arrowUp", text: m.text })));
      const lz = lineChart(c2, { height: 280 });
      lz.line("z", d.series.z, "#4fc3f7");
      body.appendChild(card(`${d.symbol} with OI`, c3));
      const cc = candleChart(c3, { height: 380, volume: true });
      cc.setBars(d.bars, "minute");
      const oiSeries = cc.chart.addLineSeries({ priceScaleId: "oi", color: "#ffb74d", lineWidth: 1, title: "OI" });
      cc.chart.priceScale("oi").applyOptions({ scaleMargins: { top: 0.05, bottom: 0.7 } });
      oiSeries.setData(d.series.oi);
      charts.push(lc, lz, cc);
      if (unsub) unsub();
      const tok = live.status()?.tokens?.futures?.[0]?.token;
      if (tok) unsub = live.subscribe(tok, (t) => cc.tick(t.p, t.x || t.r || Date.now()));
    } else {
      body.appendChild(note("No futures minute candles for today yet - parity series appear once the collector has captured the session.", "warn"));
    }
    const g = el("div");
    body.appendChild(card("Price / OI interpretation grid (conventions, not proof of positioning)", g));
    grid(g, [txtCol("price", "price"), txtCol("oi", "OI"), txtCol("label", "label"), txtCol("read", "conventional read")], d.grid);
    body.appendChild(bullets([...(s.notes || []), ...(d.notes || [])]));
  }
  await load();
  return { onBar: load, unmount: () => { if (unsub) unsub(); charts.forEach((c) => c.remove()); } };
}
