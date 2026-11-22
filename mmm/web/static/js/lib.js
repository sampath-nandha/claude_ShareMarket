// Shared UI helpers: formatting, DOM, API, Lightweight Charts wrapper, AG Grid helpers, small SVG charts.
/* global LightweightCharts, agGrid */

export const IST = 19800;

// ---------- formatting ----------
export function num(v, d = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  return Number(v).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
}
export const int = (v) => num(v, 0);
export function signed(v, d = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  return (v > 0 ? "+" : "") + num(v, d);
}
export function pct(v, d = 1, frac = false) {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  return num(frac ? v * 100 : v, d) + "%";
}
export function inr(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  return "₹" + num(v, 0);
}
export function lakh(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  return num(v / 1e5, 1) + "L";
}
export function hhmm(isoOrEpoch) {
  if (!isoOrEpoch) return "–";
  const d = typeof isoOrEpoch === "number" ? new Date(isoOrEpoch) : new Date(isoOrEpoch);
  return d.toLocaleTimeString("en-IN", { hour12: false, timeZone: "Asia/Kolkata" });
}
export function dt(iso) {
  if (!iso) return "–";
  return new Date(iso).toLocaleString("en-IN", { hour12: false, timeZone: "Asia/Kolkata" });
}
export const human = (s) => (s || "").toString().replaceAll("_", " ").toLowerCase();

// ---------- DOM ----------
export function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(n.style, v);
    else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
    else if (k === "html") n.innerHTML = v;
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    n.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return n;
}
export function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); return n; }

export function metric(label, value, sub = "", tone = "") {
  return el("div", { class: `metric ${tone}` }, el("div", { class: "m-label" }, label), el("div", { class: "m-value" }, value ?? "–"),
    sub ? el("div", { class: "m-sub" }, sub) : null);
}
export function metrics(items) { return el("div", { class: "metrics" }, items.map((i) => metric(...i))); }
export function card(title, ...body) { return el("section", { class: "card" }, title ? el("h3", {}, title) : null, ...body); }
export function note(text, tone = "info") { return el("div", { class: `note ${tone}` }, text); }
export function bullets(items) { return el("ul", { class: "bullets" }, (items || []).map((i) => el("li", {}, i))); }
export function row(...cols) { return el("div", { class: "row" }, cols.map((c) => el("div", { class: "col" }, c))); }
export function tabs(defs) {
  const head = el("div", { class: "tabs" });
  const body = el("div", { class: "tab-body" });
  let active = null;
  const show = (i) => {
    [...head.children].forEach((b, j) => b.classList.toggle("active", i === j));
    clear(body);
    active = i;
    const r = defs[i].render();
    if (r instanceof Promise) r.then((n) => n && body.appendChild(n)); else if (r) body.appendChild(r);
  };
  defs.forEach((d, i) => head.appendChild(el("button", { class: "tab", onclick: () => show(i) }, d.label)));
  const wrap = el("div", {}, head, body);
  setTimeout(() => show(0));
  wrap.show = show;
  wrap.active = () => active;
  return wrap;
}
export function select(options, value, onchange, label) {
  const s = el("select", { onchange: (e) => onchange(e.target.value) },
    options.map((o) => {
      const [v, t] = Array.isArray(o) ? o : [o, o];
      return el("option", { value: v, selected: String(v) === String(value) }, t);
    }));
  return label ? el("label", { class: "field" }, label, s) : s;
}
export function input(type, value, onchange, label, attrs = {}) {
  const i = el("input", { type, value, ...attrs, onchange: (e) => onchange(type === "number" ? Number(e.target.value) : e.target.value) });
  return label ? el("label", { class: "field" }, label, i) : i;
}
export function radio(options, value, onchange) {
  const w = el("div", { class: "seg" });
  options.forEach((o) => {
    const [v, t] = Array.isArray(o) ? o : [o, o];
    w.appendChild(el("button", { class: String(v) === String(value) ? "active" : "", onclick: () => { [...w.children].forEach((c) => c.classList.remove("active")); w.querySelector(`[data-v="${v}"]`).classList.add("active"); onchange(v); }, "data-v": v }, t));
  });
  return w;
}
export function toast(msg, tone = "info") {
  const t = el("div", { class: `toast ${tone}` }, msg);
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 4500);
}

// ---------- API ----------
export async function api(path, opts = {}) {
  const init = { headers: { "Content-Type": "application/json" }, ...opts };
  if (opts.body && typeof opts.body !== "string") init.body = JSON.stringify(opts.body);
  const r = await fetch(path, init);
  let data = null;
  try { data = await r.json(); } catch (_e) { data = null; }
  if (!r.ok) throw new Error((data && (data.detail || data.error)) || `${r.status} ${r.statusText}`);
  return data;
}
export const post = (path, body) => api(path, { method: "POST", body });

// ---------- Lightweight Charts ----------
const LEVEL_COLORS = { PDH: "#ff9800", PDL: "#ff9800", PDC: "#9e9e9e", PWH: "#e040fb", PWL: "#e040fb", ORH: "#29b6f6", ORL: "#29b6f6",
  SH: "#cfd8dc", SL: "#cfd8dc", OPEN: "#cfd8dc", ZONE_R: "#ef5350", ZONE_S: "#26a69a", ROUND: "#616161", OI_R: "#ff5252", OI_S: "#69f0ae",
  CONF_R: "#ffd54f", CONF_S: "#ffd54f", CPR_TC: "#7986cb", CPR_BC: "#7986cb", PIV_P: "#b0bec5" };
export const levelColor = (k) => LEVEL_COLORS[k] || (k.startsWith("PIV_R") ? "#ef9a9a" : k.startsWith("PIV_S") ? "#a5d6a7" : "#90a4ae");

export function tfSeconds(tf) {
  return { minute: 60, "3minute": 180, "5minute": 300, "10minute": 600, "15minute": 900, "30minute": 1800, "60minute": 3600, day: 86400 }[tf] || 60;
}

export function candleChart(container, { height = 460, volume = false } = {}) {
  const chart = LightweightCharts.createChart(container, {
    height, autoSize: true,
    layout: { background: { type: "solid", color: "#0d1117" }, textColor: "#c9d1d9", fontSize: 11 },
    grid: { vertLines: { color: "#161b22" }, horzLines: { color: "#161b22" } },
    rightPriceScale: { borderColor: "#30363d" }, timeScale: { borderColor: "#30363d", timeVisible: true, secondsVisible: false },
    crosshair: { mode: 0 },
  });
  const candles = chart.addCandlestickSeries({ upColor: "#26a69a", downColor: "#ef5350", borderVisible: false, wickUpColor: "#26a69a", wickDownColor: "#ef5350" });
  let vol = null;
  if (volume) {
    vol = chart.addHistogramSeries({ priceScaleId: "vol", priceFormat: { type: "volume" }, color: "#455a64" });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  }
  let priceLines = [];
  let last = null;
  let tf = 60;
  const lines = {};
  const api_ = {
    chart, candles,
    setBars(bars, tfName = "minute") {
      tf = tfSeconds(tfName);
      candles.setData(bars.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })));
      if (vol) vol.setData(bars.map((b) => ({ time: b.time, value: b.volume || 0, color: b.close >= b.open ? "#26a69a55" : "#ef535055" })));
      last = bars.length ? { ...bars[bars.length - 1] } : null;
      chart.timeScale().fitContent();
    },
    line(name, data, color = "#90caf9", width = 1) {
      if (!lines[name]) lines[name] = chart.addLineSeries({ color, lineWidth: width, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
      lines[name].setData(data || []);
      return lines[name];
    },
    levels(levels, maxN = 40) {
      priceLines.forEach((pl) => candles.removePriceLine(pl));
      priceLines = [];
      (levels || []).slice(0, maxN).forEach((lv) => {
        priceLines.push(candles.createPriceLine({ price: lv.price, color: levelColor(lv.kind), lineWidth: lv.kind.startsWith("ZONE") ? 1 : 1,
          lineStyle: lv.kind.startsWith("ZONE") || lv.kind.startsWith("CONF") ? 2 : 0, axisLabelVisible: lv.strength >= 60, title: lv.title || lv.kind }));
      });
    },
    priceLine(price, color, title, style = 2) {
      const pl = candles.createPriceLine({ price, color, lineWidth: 2, lineStyle: style, axisLabelVisible: true, title });
      priceLines.push(pl);
      return pl;
    },
    markers(ms) { candles.setMarkers((ms || []).slice().sort((a, b) => a.time - b.time)); },
    swings(sw) {
      api_.markers((sw || []).map((s) => ({ time: s.time, position: s.kind === "H" ? "aboveBar" : "belowBar", color: s.kind === "H" ? "#ef5350" : "#26a69a",
        shape: s.kind === "H" ? "arrowDown" : "arrowUp", text: s.label || "" })));
    },
    tick(price, epochMs) {
      // live update of the forming candle, buckets anchored at 09:15 IST like the server's resample
      if (!last || price === null || price === undefined) return;
      const t = Math.floor(epochMs / 1000) + IST;
      let bucket;
      if (tf >= 86400) bucket = t - (t % 86400);
      else {
        const dayStart = t - (t % 86400) + 9 * 3600 + 15 * 60;
        bucket = dayStart + Math.floor((t - dayStart) / tf) * tf;
      }
      if (bucket < last.time) return;
      if (bucket === last.time) {
        last.high = Math.max(last.high, price); last.low = Math.min(last.low, price); last.close = price;
      } else {
        last = { time: bucket, open: price, high: price, low: price, close: price, volume: 0 };
      }
      candles.update({ time: last.time, open: last.open, high: last.high, low: last.low, close: last.close });
    },
    remove() { chart.remove(); },
  };
  return api_;
}

export function lineChart(container, { height = 260 } = {}) {
  const chart = LightweightCharts.createChart(container, {
    height, autoSize: true,
    layout: { background: { type: "solid", color: "#0d1117" }, textColor: "#c9d1d9", fontSize: 11 },
    grid: { vertLines: { color: "#161b22" }, horzLines: { color: "#161b22" } },
    rightPriceScale: { borderColor: "#30363d" }, timeScale: { borderColor: "#30363d", timeVisible: true, secondsVisible: false },
  });
  const series = {};
  return {
    chart,
    line(name, data, color = "#90caf9", opts = {}) {
      if (!series[name]) series[name] = chart.addLineSeries({ color, lineWidth: 2, priceLineVisible: false, title: name, ...opts });
      series[name].setData(data || []);
      chart.timeScale().fitContent();
      return series[name];
    },
    remove() { chart.remove(); },
  };
}

// ---------- AG Grid ----------
export function grid(container, columnDefs, rowData, opts = {}) {
  container.classList.add("ag-theme-quartz-dark", "grid");
  if (opts.height) container.style.height = typeof opts.height === "number" ? `${opts.height}px` : opts.height;
  const auto = !opts.height;
  const g = agGrid.createGrid(container, {
    columnDefs, rowData: rowData || [], domLayout: auto ? "autoHeight" : "normal",
    defaultColDef: { sortable: true, resizable: true, filter: false, minWidth: 70, flex: 1, ...(opts.defaultColDef || {}) },
    animateRows: false, suppressCellFocus: true, ...opts.gridOptions,
  });
  return g;
}
export const fmtCol = (field, headerName, d = 2, extra = {}) => ({ field, headerName, type: "rightAligned", valueFormatter: (p) => num(p.value, d), ...extra });
export const intCol = (field, headerName, extra = {}) => fmtCol(field, headerName, 0, extra);
export const pctCol = (field, headerName, d = 1, frac = false, extra = {}) => ({ field, headerName, type: "rightAligned", valueFormatter: (p) => pct(p.value, d, frac), ...extra });
export const txtCol = (field, headerName, extra = {}) => ({ field, headerName, ...extra });
export const signCellClass = (p) => (p.value > 0 ? "pos" : p.value < 0 ? "neg" : "");
export function autoCols(rows, skip = []) {
  if (!rows || !rows.length) return [];
  return Object.keys(rows[0]).filter((k) => !skip.includes(k)).map((k) => (typeof rows[0][k] === "number" ? fmtCol(k, human(k)) : txtCol(k, human(k))));
}
export function simpleGrid(container, rows, opts = {}) { return grid(container, opts.cols || autoCols(rows, opts.skip), rows, opts); }

// ---------- small SVG charts (numeric x axis: strikes, spot levels) ----------
export function xyChart(container, { series = [], vlines = [], height = 300, xLabel = "", yLabel = "", zero = true, barGroup = true } = {}) {
  const W = Math.max(container.clientWidth || 800, 320), H = height, m = { l: 64, r: 16, t: 14, b: 34 };
  const xs = series.flatMap((s) => s.x), ys = series.flatMap((s) => s.y).filter((v) => v !== null && v !== undefined && !Number.isNaN(v));
  if (!xs.length || !ys.length) { container.appendChild(note("no data")); return; }
  let x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys, zero ? 0 : Infinity), y1 = Math.max(...ys, zero ? 0 : -Infinity);
  vlines.forEach((v) => { x0 = Math.min(x0, v.x); x1 = Math.max(x1, v.x); });
  if (x1 === x0) x1 = x0 + 1;
  if (y1 === y0) y1 = y0 + 1;
  const pad = (y1 - y0) * 0.06; y0 -= pad; y1 += pad;
  const X = (x) => m.l + ((x - x0) / (x1 - x0)) * (W - m.l - m.r), Y = (y) => H - m.b - ((y - y0) / (y1 - y0)) * (H - m.t - m.b);
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("class", "xy");
  const add = (tag, attrs, text) => { const n = document.createElementNS(ns, tag); Object.entries(attrs).forEach(([k, v]) => n.setAttribute(k, v)); if (text !== undefined) n.textContent = text; svg.appendChild(n); return n; };
  for (let i = 0; i <= 4; i++) {
    const yv = y0 + ((y1 - y0) * i) / 4;
    add("line", { x1: m.l, x2: W - m.r, y1: Y(yv), y2: Y(yv), stroke: "#21262d" });
    add("text", { x: m.l - 6, y: Y(yv) + 4, "text-anchor": "end", class: "ax" }, Math.abs(yv) >= 1e5 ? lakh(yv) : num(yv, Math.abs(yv) < 10 ? 2 : 0));
  }
  const ux = [...new Set(xs)].sort((a, b) => a - b);
  const step = Math.max(1, Math.ceil(ux.length / 12));
  ux.forEach((xv, i) => { if (i % step === 0) add("text", { x: X(xv), y: H - m.b + 16, "text-anchor": "middle", class: "ax" }, num(xv, 0)); });
  if (zero && y0 < 0 && y1 > 0) add("line", { x1: m.l, x2: W - m.r, y1: Y(0), y2: Y(0), stroke: "#6e7681" });
  const bars = series.filter((s) => s.type === "bar");
  const minGap = ux.length > 1 ? Math.min(...ux.slice(1).map((v, i) => v - ux[i])) : 1;
  const bw = Math.max(2, ((minGap / (x1 - x0)) * (W - m.l - m.r)) * 0.8 / (barGroup ? Math.max(bars.length, 1) : 1));
  series.forEach((s, si) => {
    if (s.type === "bar") {
      const bi = bars.indexOf(s);
      s.x.forEach((xv, i) => {
        const yv = s.y[i]; if (yv === null || yv === undefined || Number.isNaN(yv)) return;
        const off = barGroup ? (bi - (bars.length - 1) / 2) * bw : 0;
        add("rect", { x: X(xv) - bw / 2 + off, y: Math.min(Y(yv), Y(0)), width: bw, height: Math.abs(Y(yv) - Y(0)), fill: s.color, opacity: 0.85 });
      });
    } else {
      const pts = s.x.map((xv, i) => [xv, s.y[i]]).filter(([, yv]) => yv !== null && yv !== undefined && !Number.isNaN(yv));
      add("path", { d: pts.map(([xv, yv], i) => `${i ? "L" : "M"}${X(xv)},${Y(yv)}`).join(""), fill: "none", stroke: s.color, "stroke-width": s.width || 2, "stroke-dasharray": s.dash || "" });
    }
    add("text", { x: m.l + 8 + si * 130, y: m.t + 4, class: "lg", fill: s.color }, s.name || "");
  });
  vlines.forEach((v) => {
    add("line", { x1: X(v.x), x2: X(v.x), y1: m.t, y2: H - m.b, stroke: v.color || "#fff", "stroke-dasharray": "4 3" });
    add("text", { x: X(v.x) + 4, y: m.t + 18, class: "lg", fill: v.color || "#fff" }, v.label || "");
  });
  if (xLabel) add("text", { x: W / 2, y: H - 4, "text-anchor": "middle", class: "ax" }, xLabel);
  if (yLabel) add("text", { x: 12, y: m.t + 10, class: "ax" }, yLabel);
  container.appendChild(svg);
}

export function heat(v, max) {
  if (v === null || v === undefined || !max) return {};
  const a = Math.min(1, Math.abs(v) / max);
  return { backgroundColor: v >= 0 ? `rgba(38,166,154,${0.15 + 0.7 * a})` : `rgba(239,83,80,${0.15 + 0.7 * a})`, color: "#fff" };
}
