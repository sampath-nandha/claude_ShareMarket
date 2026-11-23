// Bullish / bearish / sideways gauge boxes (shared by the Market Condition page and the Overview strip).
import { el } from "../lib.js";

const COLORS = { BULLISH: [38, 166, 154], BEARISH: [239, 83, 80], SIDEWAYS: [240, 180, 41] };
const ENDS = { BULLISH: ["none", "strong"], BEARISH: ["strong", "none"], SIDEWAYS: ["bear", "bull"] };
const prevLabels = {};

// hysteresis: keep the previous label unless the score has moved 3 points past the boundary
function stableLabel(key, score, label) {
  const bounds = [20, 40, 70];
  const prev = prevLabels[key];
  if (prev && prev !== label && bounds.some((b) => Math.abs(score - b) < 3)) return prev;
  prevLabels[key] = label;
  return label;
}

export function gaugeBox(g, { compact = false, dim = false, horizon = "intraday" } = {}) {
  const [r, gr, b] = COLORS[g.name];
  const a = 0.25 + 0.75 * Math.min(1, g.score / 100);
  const color = `rgba(${r},${gr},${b},${a})`;
  const lbl = stableLabel(`${horizon}:${g.name}`, g.score, g.label);
  const knob = el("div", { class: "g-knob", style: { left: `${g.knob}%`, background: color, boxShadow: g.score >= 40 ? `0 0 10px rgba(${r},${gr},${b},0.8)` : "none" } });
  const track = el("div", { class: `g-track g-${g.name.toLowerCase()}` }, el("div", { class: "g-mid" }), knob);
  return el("div", { class: `g-box${compact ? " compact" : ""}${dim ? " dim" : ""}`, style: { borderColor: g.score >= 40 ? color : "var(--border)" } },
    el("div", { class: "g-head" }, el("span", { class: "g-name", style: { color: `rgb(${r},${gr},${b})` } }, g.name),
      el("span", { class: "g-score" }, `${lbl} · ${Math.floor(g.score)}`)),
    track,
    el("div", { class: "g-ends" }, el("span", {}, ENDS[g.name][0]), g.name === "SIDEWAYS" ? el("span", {}, "range") : null, el("span", {}, ENDS[g.name][1])),
    !compact && g.sublabel ? el("div", { class: "g-sub" }, g.sublabel) : null,
    !compact ? el("ul", { class: "g-reasons" }, (g.reasons.length ? g.reasons : ["no supporting evidence"]).map((x) => el("li", {}, x))) : null);
}

export function gaugeRow(state, opts = {}) {
  const dim = ["STALE", "FORMING"].includes(state.state);
  return el("div", { class: "g-row" }, state.gauges.map((g) => gaugeBox(g, { ...opts, dim, horizon: state.horizon })));
}

export function stateChip(state) {
  const txt = { LIVE: "live", FORMING: "forming (before 09:30: provisional)", AS_OF_CLOSE: "as of close", STALE: "STALE data - values frozen", DEMO: "demo data" }[state.state] || state.state;
  return el("span", { class: `pill ${state.state === "LIVE" ? "ok" : state.state === "STALE" ? "bad" : "warn"}` }, txt);
}
