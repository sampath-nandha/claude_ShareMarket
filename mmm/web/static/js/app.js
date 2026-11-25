// App shell: navigation, live WebSocket, header ticker, page lifecycle.
import { api, clear, el, hhmm, num, signed, toast } from "./lib.js";

const PAGES = [
  ["overview", "Market Overview"], ["condition", "Market Condition"], ["premarket", "Pre-Market & Opening"], ["priceaction", "Price Action"], ["futures", "Futures & Basis"],
  ["options", "Options Intelligence"], ["breadth", "Breadth"], ["signals", "Live Signals"], ["paper", "Paper Trading"], ["risk", "Risk & Journal"],
  ["backtest", "Backtesting"], ["research", "Research & Validation"], ["replay", "Market Replay"], ["health", "System Health"],
];

const state = { status: null, last: new Map(), subs: new Map(), page: null, ws: null, latency: null, wsStats: null };

export const live = {
  last: (tok) => state.last.get(Number(tok)),
  subscribe(tok, fn) {
    const k = Number(tok);
    if (!state.subs.has(k)) state.subs.set(k, new Set());
    state.subs.get(k).add(fn);
    return () => state.subs.get(k)?.delete(fn);
  },
  status: () => state.status,
  stats: () => ({ latency: state.latency, ws: state.wsStats }),
};

function onTicks(ticks, srv) {
  const now = Date.now();
  for (const t of ticks) {
    state.last.set(Number(t.t), t);
    const fns = state.subs.get(Number(t.t));
    if (fns) fns.forEach((fn) => { try { fn(t); } catch (e) { console.error(e); } });
    if (t.r) state.latency = { tickToScreen: now - t.r, serverToScreen: now - srv };
  }
  renderTicker();
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws = ws;
  const dot = document.getElementById("ws-dot");
  ws.onopen = () => { dot.className = "dot on"; dot.title = "live feed connected"; };
  ws.onclose = () => { dot.className = "dot off"; dot.title = "live feed disconnected - retrying"; setTimeout(connect, 1500); };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "ticks") onTicks(m.d, m.srv);
    else if (m.type === "hello") onTicks(m.snapshot || [], m.srv);
    else if (m.type === "bar") { state.page?.onBar?.(); refreshStatus(); }
    else if (m.type === "status") { state.wsStats = m; renderTicker(); }
  };
  setInterval(() => { if (ws.readyState === 1) ws.send("ping"); }, 20000);
}

function renderTicker() {
  const s = state.status;
  if (!s) return;
  const tk = s.tokens || {};
  const idx = state.last.get(Number(tk.index));
  const spot = idx?.p ?? s.spot;
  const prev = idx?.c;
  const chg = prev ? spot - prev : null;
  document.getElementById("t-spot").textContent = num(spot, 2);
  const c = document.getElementById("t-chg");
  c.textContent = chg !== null ? `${signed(chg)} (${signed((chg / prev) * 100)}%)` : "";
  c.className = chg > 0 ? "pos" : chg < 0 ? "neg" : "";
  const vix = state.last.get(Number(tk.vix));
  document.getElementById("t-vix").textContent = num(vix?.p ?? s.vix, 2);
  const fut = tk.futures && tk.futures[0] ? state.last.get(Number(tk.futures[0].token)) : null;
  const fp = fut?.p ?? s.futures_last;
  document.getElementById("t-fut").textContent = fp ? `${num(fp, 2)} (basis ${signed(fp - spot, 1)})` : "–";
  const lat = state.latency;
  document.getElementById("t-lat").textContent = lat ? `${Math.max(0, lat.tickToScreen)} ms tick→screen · ${Math.max(0, lat.serverToScreen)} ms server→screen` : "waiting for ticks";
  const src = state.wsStats?.zmq_live ? "ZeroMQ" : (s.demo ? "demo" : "state DB poll");
  document.getElementById("t-src").textContent = `feed: ${src}`;
}

async function refreshStatus() {
  try {
    state.status = await api("/api/status");
  } catch (e) { toast(`status: ${e.message}`, "error"); return; }
  const s = state.status;
  const banner = document.getElementById("demo-banner");
  banner.hidden = !s.demo;
  banner.textContent = s.demo_note || "";
  document.getElementById("t-mkt").textContent = s.market_open ? "MARKET OPEN" : "MARKET CLOSED";
  document.getElementById("t-mkt").className = `pill ${s.market_open ? "ok" : ""}`;
  document.getElementById("t-asof").textContent = `data as of ${hhmm(s.as_of)} IST · ${s.source}`;
  const tokOk = s.auth && s.auth.token_valid_today;
  const a = document.getElementById("t-auth");
  a.textContent = s.demo ? "DEMO" : tokOk ? "Kite token OK" : "Kite login needed";
  a.className = `pill ${s.demo ? "bad" : tokOk ? "ok" : "warn"}`;
  renderTicker();
}

async function route() {
  const name = (location.hash.replace("#/", "") || "overview").split("?")[0];
  const known = PAGES.find(([k]) => k === name) ? name : "overview";
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === known));
  try { state.page?.unmount?.(); } catch (e) { console.error(e); }
  const view = clear(document.getElementById("view"));
  view.appendChild(el("div", { class: "loading" }, "loading…"));
  try {
    const mod = await import(`./pages/${known}.js`);
    clear(view);
    state.page = await mod.mount(view, { live, status: state.status, refreshStatus });
  } catch (e) {
    clear(view);
    view.appendChild(el("div", { class: "note error" }, `Could not load this page: ${e.message}`));
    console.error(e);
  }
}

function buildNav() {
  const nav = document.getElementById("nav");
  PAGES.forEach(([k, t]) => nav.appendChild(el("a", { href: `#/${k}`, "data-page": k }, t)));
}

window.addEventListener("hashchange", route);
window.addEventListener("DOMContentLoaded", async () => {
  buildNav();
  await refreshStatus();
  connect();
  route();
  setInterval(refreshStatus, 30000);
});
