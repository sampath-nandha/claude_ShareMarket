import {
  api, bullets, card, clear, el, fmtCol, grid, heat, human, input, intCol, lakh, lineChart, metrics, note, num, pct, post, radio, row, select,
  signCellClass, signed, simpleGrid, tabs, txtCol, xyChart,
} from "../lib.js";

export async function mount(root, { live }) {
  const st = { expiry: null, width: 8 };
  root.appendChild(el("h2", {}, "Options Intelligence"));
  root.appendChild(el("div", { class: "subtitle" }, "Chain · OI structure · OI flow · IV & Greeks · ranked contracts · payoff builder · theta planner"));
  const top = el("div"), body = el("div");
  root.append(top, body);
  let d = null, unsubs = [], tabsEl = null, chainApi = null, cleanups = [];

  function liveChain(rows) {
    unsubs.forEach((u) => u()); unsubs = [];
    if (!chainApi) return;
    rows.forEach((r) => {
      for (const side of ["CE", "PE"]) {
        const tok = r[`${side}_token`];
        if (!tok) continue;
        unsubs.push(live.subscribe(tok, (t) => {
          const node = chainApi.getRowNode(String(r.strike));
          if (!node) return;
          if (t.p !== null && t.p !== undefined) node.setDataValue(`${side}_last_price`, t.p);
          if (t.oi) node.setDataValue(`${side}_oi`, t.oi);
          if (t.b) node.setDataValue(`${side}_bid`, t.b);
          if (t.a) node.setDataValue(`${side}_ask`, t.a);
        }));
      }
    });
  }

  function chainTab() {
    const w = el("div");
    const g = el("div");
    w.append(el("div", { class: "controls" }, select([3, 5, 8, 10, 12].map((n) => [n, `±${n} strikes`]), st.width, (v) => { st.width = Number(v); load(); })), g);
    const atm = d.summary.atm_strike;
    const BU = { LONG_BUILDUP: "LB", SHORT_BUILDUP: "SB", SHORT_COVERING: "SC", LONG_UNWINDING: "LU", NEUTRAL: "–" };
    const W = (w) => ({ flex: 0, width: w, minWidth: w });
    const L = (v) => (v === null || v === undefined ? "–" : lakh(v));
    const side = (s) => [
      { field: `${s}_oi`, headerName: "OI", type: "rightAligned", valueFormatter: (p) => L(p.value), enableCellChangeFlash: true, ...W(72) },
      { field: `${s}_oi_change`, headerName: "ΔOI", type: "rightAligned", valueFormatter: (p) => L(p.value), cellClass: signCellClass, ...W(72) },
      { field: `${s}_volume`, headerName: "vol", type: "rightAligned", valueFormatter: (p) => L(p.value), ...W(66) },
      fmtCol(`${s}_iv`, "IV%", 1, { valueGetter: (p) => (p.data[`${s}_iv`] ?? null) * 100, ...W(58) }), fmtCol(`${s}_delta`, "Δ", 2, W(68)),
      fmtCol(`${s}_last_price`, "LTP", 2, { enableCellChangeFlash: true, cellStyle: { fontWeight: 600 }, ...W(78) }),
      fmtCol(`${s}_bid`, "bid", 2, { enableCellChangeFlash: true, ...W(80) }), fmtCol(`${s}_ask`, "ask", 2, { enableCellChangeFlash: true, ...W(80) }),
      { field: `${s}_buildup`, headerName: "B/U", valueFormatter: (p) => BU[p.value] || "–", tooltipValueGetter: (p) => human(p.value), ...W(50) },
    ];
    const ce = side("CE"), pe = side("PE").reverse();
    chainApi = grid(g, [{ headerName: "CALLS", children: ce }, { headerName: "", children: [fmtCol("strike", "STRIKE", 0, { cellStyle: { fontWeight: 700, textAlign: "center", background: "#1c2230" }, ...W(84) })] },
      { headerName: "PUTS", children: pe }], d.chain, {
      height: 620, defaultColDef: { minWidth: 50 },
      gridOptions: { getRowId: (p) => String(p.data.strike), rowClassRules: { atm: (p) => p.data.strike === atm } },
    });
    liveChain(d.chain);
    w.appendChild(el("div", { class: "small" }, "Highlighted row = ATM. OI, ΔOI and volume in lakhs. LTP, bid, ask and OI update live from the tick feed; ΔOI is versus the previous snapshot/close. B/U: LB long build-up · SB short build-up · SC short covering · LU long unwinding."));
    return w;
  }

  function oiTab() {
    const w = el("div");
    const b = d.oi_bars;
    const c1 = el("div"), c2 = el("div"), bu = el("div");
    w.append(card("Open interest by strike", c1), card("OI change by strike", c2));
    const v = [{ x: d.spot, label: `spot ${num(d.spot, 0)}`, color: "#fff" }];
    if (d.summary.max_pain) v.push({ x: d.summary.max_pain, label: `max pain ${num(d.summary.max_pain, 0)}`, color: "#ffb300" });
    setTimeout(() => {
      xyChart(c1, { series: [{ type: "bar", x: b.strikes, y: b.ce_oi, color: "#ef5350", name: "Call OI" }, { type: "bar", x: b.strikes, y: b.pe_oi, color: "#26a69a", name: "Put OI" }], vlines: v, height: 300 });
      xyChart(c2, { series: [{ type: "bar", x: b.strikes, y: b.ce_chg, color: "#ef9a9a", name: "Call ΔOI" }, { type: "bar", x: b.strikes, y: b.pe_chg, color: "#80cbc4", name: "Put ΔOI" }], vlines: v, height: 240 });
    });
    const s = d.summary;
    w.append(row(el("div", {}, el("b", {}, "OI resistance (top call OI ≥ spot): "), d.walls.resistance.map(([k, oi]) => `${num(k, 0)} (${lakh(oi)})`).join(", ")),
      el("div", {}, el("b", {}, "OI support (top put OI ≤ spot): "), d.walls.support.map(([k, oi]) => `${num(k, 0)} (${lakh(oi)})`).join(", "))));
    w.append(el("div", { class: "small" }, `Total call OI ${lakh(s.call_oi_total)} (Δ ${lakh(s.call_oi_change)}) · total put OI ${lakh(s.put_oi_total)} (Δ ${lakh(s.put_oi_change)})`));
    w.append(card("Per-contract build-up (price vs ΔOI)", bu));
    grid(bu, [txtCol("tradingsymbol", "contract", { flex: 2 }), fmtCol("strike", "strike", 0), txtCol("option_type", "type"), fmtCol("last_price", "LTP"), intCol("oi", "OI"),
      intCol("oi_change", "ΔOI", { cellClass: signCellClass }), fmtCol("oi_change_pct", "ΔOI%", 1, { cellClass: signCellClass }), txtCol("buildup", "build-up", { valueFormatter: (p) => human(p.value) })],
    d.buildup, { height: 420 });
    w.append(note("OI walls are conventional support/resistance proxies and confirmation inputs, never a trade trigger on their own."));
    return w;
  }

  function flowTab() {
    const f = d.flow;
    const w = el("div");
    if (!f.available) { w.appendChild(note(f.note, "warn")); return w; }
    const s = f.state;
    const cw = s.call_wall, pw = s.put_wall;
    w.appendChild(metrics([
      ["Call wall", cw ? num(cw.strike, 0) : "–", cw ? `${human(cw.status)} · ${cw.oi_chg_30_pct !== null ? signed(cw.oi_chg_30_pct, 1) + "% 30m" : ""} · moved ${human(cw.migration)}` : "", cw?.status === "UNWINDING" ? "pos" : cw?.status === "DEFENDED" ? "neg" : ""],
      ["Put wall", pw ? num(pw.strike, 0) : "–", pw ? `${human(pw.status)} · ${pw.oi_chg_30_pct !== null ? signed(pw.oi_chg_30_pct, 1) + "% 30m" : ""} · moved ${human(pw.migration)}` : "", pw?.status === "UNWINDING" ? "neg" : pw?.status === "DEFENDED" ? "pos" : ""],
      ["Near-ATM PCR", num(s.pcr_near), `${human(s.pcr_trend)} · open ${num(s.pcr_near_open)} · 30m ago ${num(s.pcr_near_30)}`],
      ["PCR (all archived)", num(s.pcr_total)], ["Expiry", s.expiry], ["Coverage", s.strikes_present, s.coverage_note],
    ]));
    const wc = el("div", { class: "chart" });
    w.appendChild(card("Wall migration through the day", wc));
    const lc = lineChart(wc, { height: 280 });
    lc.line("spot", (f.walls || []).map((r) => ({ time: r.time, value: r.spot })), "#ffffff");
    lc.line("call wall", (f.walls || []).filter((r) => r.call_wall).map((r) => ({ time: r.time, value: r.call_wall })), "#ef5350", { lineType: 1 });
    lc.line("put wall", (f.walls || []).filter((r) => r.put_wall).map((r) => ({ time: r.time, value: r.put_wall })), "#26a69a", { lineType: 1 });
    cleanups.push(() => lc.remove());
    // heatmaps: strike rows x time columns, OI change since previous close
    for (const side of ["CE", "PE"]) {
      const cells = f.heatmap.filter((h) => h.side === side);
      const times = [...new Set(cells.map((c) => c.time))].sort((a, b) => a - b);
      const strikes = [...new Set(cells.map((c) => c.strike))].sort((a, b) => b - a);
      const max = Math.max(1, ...cells.map((c) => Math.abs(c.oi_chg || 0)));
      const rows = strikes.map((k) => { const r = { strike: k }; cells.filter((c) => c.strike === k).forEach((c) => { r[c.time] = c.oi_chg; }); return r; });
      const label = (t) => new Date((t - 19800) * 1000).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false, hour: "2-digit", minute: "2-digit" });
      const g = el("div");
      w.appendChild(card(`${side === "CE" ? "Call" : "Put"} OI change since previous close (heatmap)`, g));
      grid(g, [fmtCol("strike", "strike", 0, { pinned: "left", minWidth: 80 }), ...times.map((t) => ({ field: String(t), headerName: label(t), minWidth: 58, flex: 0, width: 62,
        valueFormatter: (p) => (p.value === null || p.value === undefined ? "" : lakh(p.value)), cellStyle: (p) => heat(p.value, max) }))], rows, { height: 360 });
    }
    const t = el("div");
    w.appendChild(card("Per-strike flow (30-minute change and build-up)", t));
    grid(t, [fmtCol("strike", "strike", 0), intCol("ce_oi", "CE OI"), fmtCol("ce_chg_30_pct", "CE ΔOI 30m%", 1, { cellClass: signCellClass }), fmtCol("ce_px_chg_30", "CE Δpx 30m", 2, { cellClass: signCellClass }),
      txtCol("ce_buildup", "CE build-up", { valueFormatter: (p) => human(p.value) }), intCol("pe_oi", "PE OI"), fmtCol("pe_chg_30_pct", "PE ΔOI 30m%", 1, { cellClass: signCellClass }),
      fmtCol("pe_px_chg_30", "PE Δpx 30m", 2, { cellClass: signCellClass }), txtCol("pe_buildup", "PE build-up", { valueFormatter: (p) => human(p.value) })], f.table, { height: 420 });
    w.appendChild(bullets(s.notes));
    return w;
  }

  function ivTab() {
    const w = el("div");
    const c = el("div"), term = el("div"), g = el("div");
    w.append(card("IV smile (derived, Black-Scholes)", c), card("Term structure", term), card("Greeks per contract", g));
    setTimeout(() => xyChart(c, { series: [{ type: "line", x: d.smile.ce.map((p) => p[0]), y: d.smile.ce.map((p) => p[1]), color: "#ef5350", name: "Call IV %" },
      { type: "line", x: d.smile.pe.map((p) => p[0]), y: d.smile.pe.map((p) => p[1]), color: "#26a69a", name: "Put IV %" }], vlines: [{ x: d.spot, label: "spot" }], height: 300, zero: false }));
    grid(term, [txtCol("expiry", "expiry"), fmtCol("atm_iv", "ATM IV %", 1), fmtCol("straddle", "straddle", 1), fmtCol("expected_move", "exp. move", 0), fmtCol("pcr", "PCR"), fmtCol("max_pain", "max pain", 0)], d.term);
    grid(g, [txtCol("tradingsymbol", "contract", { flex: 2 }), fmtCol("strike", "strike", 0), txtCol("option_type", "type"), fmtCol("mid", "mid"), fmtCol("iv", "IV%", 1), fmtCol("delta", "Δ", 3),
      fmtCol("gamma", "Γ", 5), fmtCol("theta", "Θ/day"), fmtCol("vega", "vega"), fmtCol("time_value", "time value", 1), fmtCol("spread_pct", "spread%"), fmtCol("liquidity_score", "liq", 0),
      fmtCol("theta_pct_day", "Θ % of premium", 1)], d.greeks, { height: 420 });
    w.append(note("Theta is per calendar day in premium points; theta as % of premium is the real cost of being wrong on timing."));
    return w;
  }

  function rankTab() {
    const w = el("div");
    const r = { direction: "BULLISH", target: Math.round(d.spot + 120), stop: Math.round(d.spot - 60), hold_hours: 1.5, delta_lo: 0.35, delta_hi: 0.65 };
    const out = el("div");
    const run = async () => {
      clear(out).appendChild(el("div", { class: "loading" }, "ranking…"));
      try {
        const res = await post("/api/options/rank", { ...r, expiry: d.expiry });
        clear(out);
        const b = res.best;
        if (b) {
          out.appendChild(note(`Best fit: ${b.tradingsymbol} @ ${num(b.premium)} · Δ ${num(b.delta)} · IV ${num(b.iv * 100, 1)}% · liquidity ${num(b.liquidity_score, 0)}/100 · premium at target ≈ ${num(b.expected_premium_at_target, 1)}, at stop ≈ ${num(b.expected_premium_at_stop, 1)} · premium R:R ${num(b.reward_risk_premium)}`, "ok"));
          const z = res.sizing;
          out.appendChild(el("div", {}, `Suggested size at ${z.risk_pct}% risk of ₹${num(z.capital, 0)}: ${z.lots} lot(s) (${z.qty} qty), premium at risk ≈ ₹${num(z.premium_at_risk, 0)}. Round-trip cost for 1 lot ≈ ₹${num(z.round_trip_cost_1lot, 0)} (${num(z.breakeven_premium_pts)} pts of premium).`));
        } else out.appendChild(note("NO SUITABLE OPTION - every candidate fails a hard filter (OI, volume, spread or delta band).", "error"));
        const g = el("div");
        out.appendChild(g);
        grid(g, [txtCol("tradingsymbol", "contract", { flex: 2 }), fmtCol("premium", "premium"), fmtCol("delta", "Δ"), fmtCol("iv", "IV%", 1, { valueGetter: (p) => p.data.iv * 100 }), fmtCol("theta", "Θ"),
          fmtCol("spread_pct", "spread%"), intCol("oi", "OI"), intCol("volume", "vol"), fmtCol("liquidity_score", "liq", 0), fmtCol("score", "score", 1), fmtCol("expected_premium_at_target", "@target", 1),
          fmtCol("expected_premium_at_stop", "@stop", 1), fmtCol("reward_risk_premium", "prem R:R"), txtCol("reasons", "reasons", { flex: 3 }), txtCol("rejections", "rejections", { flex: 3 })], res.candidates);
      } catch (e) { clear(out).appendChild(note(e.message, "error")); }
    };
    w.append(el("div", { class: "small" }, "Direction comes from price action first. This ranks contracts for a given view with transparent scoring and hard liquidity filters."),
      el("div", { class: "controls" }, radio(["BULLISH", "BEARISH"], r.direction, (v) => { r.direction = v; r.target = Math.round(d.spot + (v === "BULLISH" ? 120 : -120)); r.stop = Math.round(d.spot + (v === "BULLISH" ? -60 : 60)); run(); }),
        input("number", r.target, (v) => { r.target = v; }, "underlying target"), input("number", r.stop, (v) => { r.stop = v; }, "underlying stop"),
        input("number", r.hold_hours, (v) => { r.hold_hours = v; }, "hold (hours)", { step: 0.5 }), input("number", r.delta_lo, (v) => { r.delta_lo = v; }, "Δ low", { step: 0.05 }),
        input("number", r.delta_hi, (v) => { r.delta_hi = v; }, "Δ high", { step: 0.05 }), el("button", { class: "primary", onclick: run }, "Rank")), out,
      el("div", { class: "small" }, "Score: liquidity 35 · delta fit 25 · theta 15 · spread 15 · premium efficiency 10. Re-pricing keeps IV constant - a vol crush lowers both."));
    run();
    return w;
  }

  function builderTab() {
    const w = el("div");
    const legs = [];
    const leg = { strike: d.summary.atm_strike, kind: "CE", side: "BUY", lots: 1 };
    let days = 0;
    const out = el("div"), lg = el("div");
    const strikes = [...new Set(d.chain.map((r) => r.strike))].sort((a, b) => a - b);
    const run = async (preset) => {
      try {
        const res = await post("/api/options/payoff", { expiry: d.expiry, legs, preset: preset || null, days_forward: days });
        if (preset) { legs.length = 0; res.legs.forEach((l) => legs.push({ kind: l.kind, strike: l.strike, qty: l.qty })); }
        clear(out); clear(lg);
        if (!res.legs.length) { out.appendChild(note("Add legs or load a preset.")); return; }
        simpleGrid(lg, res.legs);
        const c = el("div");
        out.appendChild(c);
        const v = [{ x: res.spot, label: "spot", color: "#fff" }, ...res.breakevens.map((x) => ({ x, label: `BE ${num(x, 0)}`, color: "#66bb6a" }))];
        if (res.expected_move) v.push({ x: res.spot - res.expected_move, label: "-1σ", color: "#90a4ae" }, { x: res.spot + res.expected_move, label: "+1σ", color: "#90a4ae" });
        const series = [{ type: "line", x: res.grid.spot, y: res.grid.expiry, color: "#ffb300", name: "at expiry" }];
        if (res.grid.t0.length) series.push({ type: "line", x: res.grid.spot, y: res.grid.t0, color: "#29b6f6", name: `T+${days}d (model)`, dash: "5 4" });
        setTimeout(() => xyChart(c, { series, vlines: v, height: 380, xLabel: "NIFTY at expiry", yLabel: "P&L ₹" }));
        out.appendChild(metrics([["Net premium", `₹${num(res.net_premium, 0)}`, res.net_premium > 0 ? "credit" : "debit"],
          ["Max profit", typeof res.max_profit === "string" ? res.max_profit : `₹${num(res.max_profit, 0)}`], ["Max loss", typeof res.max_loss === "string" ? res.max_loss : `₹${num(res.max_loss, 0)}`],
          ["Breakevens", res.breakevens.map((x) => num(x, 0)).join(", ") || "–"], ["Δ / Γ", `${signed(res.greeks.delta, 1)} / ${signed(res.greeks.gamma, 4)}`],
          ["Θ/day · vega", `₹${signed(res.greeks.theta, 0)} · ₹${signed(res.greeks.vega, 0)}`]]));
        if (res.max_loss === "unlimited") out.appendChild(note("Undefined-risk position: naked short option exposure. Margin and tail risk are not modelled here.", "error"));
        res.notes.forEach((n) => out.appendChild(el("div", { class: "small" }, n)));
      } catch (e) { clear(out).appendChild(note(e.message, "error")); }
    };
    w.append(el("div", { class: "controls" },
      select(strikes, leg.strike, (v) => { leg.strike = Number(v); }, "strike"), select(["CE", "PE"], leg.kind, (v) => { leg.kind = v; }, "type"),
      select(["BUY", "SELL"], leg.side, (v) => { leg.side = v; }, "side"), input("number", 1, (v) => { leg.lots = v; }, "lots", { min: 1, max: 50 }),
      el("button", { onclick: () => { legs.push({ kind: leg.kind, strike: leg.strike, qty: leg.lots * (leg.side === "BUY" ? 1 : -1) }); run(); } }, "Add leg"),
      el("button", { onclick: () => { legs.length = 0; run(); } }, "Clear"),
      select([["", "preset…"], ["Long straddle"], ["Short straddle"], ["Bull call spread"], ["Bear put spread"], ["Iron condor"], ["Long strangle"]].map((p) => (p.length === 1 ? [p[0], p[0]] : p)), "", (v) => v && run(v), "preset"),
      input("number", 0, (v) => { days = v; run(); }, "T+ days", { step: 0.5, min: 0 })), lg, out);
    run();
    return w;
  }

  function thetaTab() {
    const w = el("div");
    const q = { kind: "CE", strike: d.summary.atm_strike, lots: 1 };
    const out = el("div");
    const strikes = [...new Set(d.chain.map((r) => r.strike))].sort((a, b) => a - b);
    const run = async () => {
      try {
        const res = await api(`/api/options/theta?kind=${q.kind}&strike=${q.strike}&lots=${q.lots}&expiry=${d.expiry}`);
        clear(out);
        const max = Math.max(1, ...res.table.flatMap((r) => res.moves.map((m) => Math.abs(r[`${m >= 0 ? "+" : ""}${m}`] || 0))));
        const g = el("div");
        out.appendChild(g);
        grid(g, [fmtCol("hours", "hours held", 2), ...res.moves.map((m) => { const k = `${m >= 0 ? "+" : ""}${m}`; return { field: k, headerName: `${k} pts`, type: "rightAligned", valueFormatter: (p) => signed(p.value, 0), cellStyle: (p) => heat(p.value, max) }; })], res.table);
        out.appendChild(el("div", { class: "small" }, `Net ₹ P&L for ${res.lots} lot(s) of ${num(res.strike, 0)} ${res.kind} bought at mid ${num(res.premium)} (IV held at ${num(res.iv, 1)}%), after Zerodha costs and slippage. Daily theta ≈ ${num(res.theta)} pts (₹${num(res.theta_rupees_per_day, 0)}/day).`));
      } catch (e) { clear(out).appendChild(note(e.message, "error")); }
    };
    w.append(el("div", { class: "small" }, "How much the underlying must move, by when, for a long option to beat decay and costs."),
      el("div", { class: "controls" }, select(["CE", "PE"], q.kind, (v) => { q.kind = v; run(); }, "option"), select(strikes, q.strike, (v) => { q.strike = Number(v); run(); }, "strike"),
        input("number", 1, (v) => { q.lots = v; run(); }, "lots", { min: 1, max: 20 })), out);
    run();
    return w;
  }

  async function load() {
    try { d = await api(`/api/options?width=${st.width}${st.expiry ? `&expiry=${st.expiry}` : ""}`); } catch (e) { clear(top).appendChild(note(e.message, "warn")); clear(body); return; }
    st.expiry = d.expiry;
    const s = d.summary;
    clear(top).append(el("div", { class: "controls" }, select(d.expiries.map((e) => [e.expiry, `${e.expiry} (${e.dte} DTE)`]), d.expiry, (v) => { st.expiry = v; load(); }, "expiry")),
      metrics([["Spot", num(d.spot)], ["ATM", num(s.atm_strike, 0)], ["ATM straddle", num(s.atm_straddle), s.expected_move ? `±${num(s.expected_move, 0)} (${num(s.expected_move_pct)}%) exp. move` : ""],
        ["ATM IV", s.atm_iv ? pct(s.atm_iv, 1, true) : "–", s.iv_skew_25d !== null ? `skew 25Δ P-C ${signed(s.iv_skew_25d * 100, 1)}%` : ""], ["PCR (OI)", num(s.pcr_oi), s.pcr_volume ? `vol PCR ${num(s.pcr_volume)}` : ""],
        ["Max pain", num(s.max_pain, 0)], ["Walls C / P", s.call_wall && s.put_wall ? `${num(s.call_wall, 0)} / ${num(s.put_wall, 0)}` : "–"], ["Coverage", s.coverage, `${s.strikes_present}/${s.strikes_expected} strikes`]]),
      el("div", { class: "small" }, `${d.iv_move ? `1σ move to expiry from ATM IV: ±${num(d.iv_move, 0)} pts. ` : ""}${d.derived_note}`), ...(s.notes || []).map((n) => note(n, "warn")));
    const keep = tabsEl ? tabsEl.active() : 0;
    cleanups.forEach((c) => c()); cleanups = [];
    clear(body);
    tabsEl = tabs([{ label: "Option chain (live)", render: chainTab }, { label: "OI structure", render: oiTab }, { label: "OI flow", render: flowTab }, { label: "IV & Greeks", render: ivTab },
      { label: "Ranked contracts", render: rankTab }, { label: "Strategy builder", render: builderTab }, { label: "Theta & cost planner", render: thetaTab }]);
    body.appendChild(tabsEl);
    if (keep) setTimeout(() => tabsEl.show(keep), 10);
  }
  await load();
  return { onBar: () => { if (!tabsEl || tabsEl.active() <= 3) load(); }, unmount: () => { unsubs.forEach((u) => u()); cleanups.forEach((c) => c()); } };
}
