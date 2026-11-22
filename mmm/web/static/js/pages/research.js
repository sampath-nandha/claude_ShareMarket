import { api, bullets, card, clear, el, fmtCol, grid, note, pct, txtCol } from "../lib.js";

export async function mount(root) {
  root.appendChild(el("h2", {}, "Research & Validation"));
  root.appendChild(el("div", { class: "subtitle" }, "Strategy status registry and the Release-1 feature study. Nothing becomes actionable without passing the out-of-sample gates."));
  const body = el("div");
  root.appendChild(body);
  const d = await api("/api/research");
  body.appendChild(note(`Run on your PC: ${d.commands.join("  ·  ")}`));
  const ss = el("div");
  body.appendChild(card("Strategy status (H10 registry)", ss));
  const srows = Object.entries(d.strategy_status || {}).map(([id, v]) => ({ id, status: v.status, reason: v.reason, updated_at: v.updated_at }));
  if (srows.length) grid(ss, [txtCol("id", "strategy", { maxWidth: 100 }), txtCol("status", "status"), txtCol("reason", "reason", { flex: 4, wrapText: true, autoHeight: true }), txtCol("updated_at", "updated")], srows);
  else ss.appendChild(note("No research run recorded yet - every strategy is EXPERIMENTAL.", "warn"));
  const fs = el("div");
  body.appendChild(card("Feature study (Phase 4) - only PASS features change a signal's score", fs));
  const frows = Object.entries(d.feature_status || {}).map(([k, v]) => ({ key: k, phase: v.phase, horizon: v.horizon || "close", status: v.status, oos_n: v.oos?.n, oos_hit: v.oos?.hit_rate, oos_lb: v.oos?.lb,
    oos_mean: v.oos?.mean_net_pts, is_n: v.in_sample?.n, is_hit: v.in_sample?.hit_rate, description: v.description }));
  if (frows.length) {
    const pc = (f, h) => ({ field: f, headerName: h, type: "rightAligned", valueFormatter: (p) => pct(p.value, 1, true) });
    grid(fs, [txtCol("key", "feature"), fmtCol("phase", "phase", 0), txtCol("horizon", "horizon (min)"), txtCol("status", "status", { cellClass: (p) => (p.value === "PASS" ? "pos" : p.value === "FAIL" ? "neg" : "") }),
      fmtCol("is_n", "IS n", 0), pc("is_hit", "IS hit"), fmtCol("oos_n", "OOS n", 0), pc("oos_hit", "OOS hit"), pc("oos_lb", "OOS LB95"), fmtCol("oos_mean", "OOS mean pts", 1),
      txtCol("description", "hypothesis", { flex: 4, wrapText: true, autoHeight: true })], frows);
  } else fs.appendChild(note("No feature study yet. Run `mmm research features` (or `mmm research run`).", "warn"));
  const rr = d.research_report;
  if (rr) {
    const g = el("div");
    body.appendChild(card(`Latest research report · ${rr.generated_at} · ${rr.sessions} sessions (${rr.range?.join(" → ")})`, g, bullets(rr.notes || []), el("div", { class: "small" }, rr.file)));
    grid(g, [txtCol("id", "strategy"), fmtCol("n", "OOS n", 0), { field: "win_rate", headerName: "win rate", valueFormatter: (p) => pct(p.value, 1, true) },
      { field: "lb", headerName: "95% LB", valueFormatter: (p) => pct(p.value, 1, true) }, fmtCol("expectancy", "expectancy ₹", 0), fmtCol("pf", "PF"), txtCol("verdict", "verdict"), txtCol("status", "status")],
    Object.entries(rr.strategies || {}).map(([id, e]) => ({ id, n: e.oos?.n, win_rate: e.oos?.win_rate, lb: e.oos?.win_rate_ci_low, expectancy: e.oos?.expectancy, pf: e.oos?.profit_factor, verdict: e.oos?.verdict, status: e.status })));
  }
  const fr = d.feature_report;
  if (fr) body.appendChild(card(`Latest feature report · ${fr.generated_at}`, el("div", {}, `${fr.sessions} work sessions, first ${fr.train_days} in-sample, holdout ${fr.holdout_sessions} untouched, cost ${fr.cost_points} pts/event, PASS needs ≥${fr.min_events} OOS events.`),
    el("div", { class: "small" }, fr.file)));
  return {};
}
