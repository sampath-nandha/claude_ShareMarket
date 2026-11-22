import { api, bullets, candleChart, card, clear, el, metrics, note, num, select } from "../lib.js";

export async function mount(root) {
  root.appendChild(el("h2", {}, "Market Replay"));
  root.appendChild(el("div", { class: "subtitle" }, "Step through a session; the engine only ever sees bars before the cursor. Outcomes are revealed here, never to the engine."));
  const s = await api("/api/replay/sessions");
  const st = { day: s.sessions[0]?.date, i: 60, playing: null };
  const info = el("div"), box = el("div", { class: "chart" }), cands = el("div");
  const slider = el("input", { type: "range", min: 30, max: 375, value: st.i, style: { flex: 1 }, oninput: (e) => { st.i = Number(e.target.value); load(); } });
  const label = el("span", { class: "small" });
  root.append(el("div", { class: "controls" }, select(s.sessions.map((x) => [x.date, `${x.date} (${x.bars} bars)`]), st.day, (v) => { st.day = v; st.i = 60; load(true); }, "session"),
    el("button", { onclick: () => { st.i = 30; load(); } }, "⏮ start"), el("button", { onclick: () => { st.i = Math.max(30, st.i - 5); load(); } }, "◀ -5m"),
    el("button", { onclick: () => { st.i += 5; load(); } }, "▶ +5m"),
    el("button", { onclick: (e) => { if (st.playing) { clearInterval(st.playing); st.playing = null; e.target.textContent = "⏵ play"; } else { st.playing = setInterval(() => { st.i += 1; load(); }, 700); e.target.textContent = "⏸ pause"; } } }, "⏵ play")),
  el("div", { class: "flex" }, slider, label), info, box, cands);
  const chart = candleChart(box, { height: 480 });
  let busy = false;

  async function load(reset = false) {
    if (busy || !st.day) return;
    busy = true;
    try {
      const d = await api(`/api/replay?day=${st.day}&i=${st.i}`);
      st.i = d.i;
      slider.max = d.n; slider.value = d.i;
      label.textContent = `minute ${d.i}/${d.n} · cursor ${new Date(d.cursor).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false })}`;
      chart.setBars(d.bars, "minute");
      chart.levels((d.levels || []).map((l) => ({ ...l, title: l.kind })));
      (d.candidates || []).forEach((c) => { chart.priceLine(c.entry, "#fff", `${c.strategy} entry`); chart.priceLine(c.stop, "#ef5350", "stop"); chart.priceLine(c.target1, "#26a69a", "T1"); });
      clear(info).appendChild(d.regime ? metrics([["Regime", d.regime], ["Setup structure", d.setup_structure], ["Context structure", d.context_structure], ["ATR", num(d.atr, 1)], ["Candidates", d.candidates.length]]) : note(d.note || ""));
      clear(cands);
      (d.candidates || []).forEach((c) => cands.appendChild(card(`${c.strategy} ${c.direction} ${c.trigger_event} · entry ${num(c.entry, 0)} stop ${num(c.stop, 0)} T1 ${num(c.target1, 0)} T2 ${num(c.target2, 0)} · R:R ${num(c.reward_risk)}`,
        bullets(c.evidence), note(`Outcome (revealed only here): ${c.outcome}`, c.outcome.startsWith("T1") ? "ok" : c.outcome.startsWith("STOP") ? "error" : "info"))));
      if (st.playing && d.i >= d.n) { clearInterval(st.playing); st.playing = null; }
    } catch (e) { clear(info).appendChild(note(e.message, "warn")); if (reset) chart.setBars([], "minute"); }
    busy = false;
  }
  if (!st.day) root.appendChild(note("No sessions archived yet.", "warn")); else await load(true);
  return { unmount: () => { if (st.playing) clearInterval(st.playing); chart.remove(); } };
}
