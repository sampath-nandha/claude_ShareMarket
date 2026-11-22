import { api, card, clear, el, grid, metrics, note, num, pct, simpleGrid, txtCol, fmtCol } from "../lib.js";

export async function mount(root, { live }) {
  root.appendChild(el("h2", {}, "System Health"));
  root.appendChild(el("div", { class: "subtitle" }, "Auth, collector, live feed, archive coverage, data-quality incidents, resources."));
  const body = el("div");
  root.appendChild(body);
  async function load() {
    let d;
    try { d = await api("/api/health"); } catch (e) { clear(body).appendChild(note(e.message, "error")); return; }
    clear(body);
    const a = d.auth || {};
    const stats = live.stats();
    body.appendChild(metrics([["Kite API key", a.api_key_configured ? "configured" : "MISSING", "", a.api_key_configured ? "pos" : "neg"],
      ["Access token", a.token_valid_today ? "valid today" : "NOT VALID", a.token_issued_on || "", a.token_valid_today ? "pos" : "warn"],
      ["Market", d.market_open ? "OPEN" : "CLOSED"], ["CPU", pct(d.cpu_pct, 0)], ["Memory", pct(d.mem_pct, 0), `${num(d.mem_used_gb, 1)} / ${num(d.mem_total_gb, 1)} GB`],
      ["Disk", pct(d.disk_pct, 0), `${num(d.disk_free_gb, 0)} GB free · ${d.data_dir}`],
      ["Live feed", stats.ws?.zmq_live ? "ZeroMQ" : "polling / idle", `${stats.ws?.clients ?? "–"} client(s) · ${d.tickbus}`],
      ["Latency", stats.latency ? `${stats.latency.tickToScreen} ms` : "–", "tick receipt → screen"]]));
    if (d.collector_alert) body.appendChild(note("Collector is not healthy during market hours.", "error"));
    const hb = el("div");
    body.appendChild(card("Component heartbeats", hb));
    if (d.heartbeats.length) simpleGrid(hb, d.heartbeats); else hb.appendChild(note("No heartbeats yet - the collector/scheduler have not run."));
    const cov = el("div");
    body.appendChild(card(`Archive coverage (NIFTY 50) · instrument rows ${num(d.instrument_rows, 0)} · snapshot ${d.snapshot_date || "n/a"}`, cov,
      el("div", { class: "small" }, Object.entries(d.partitions).map(([k, v]) => `${k}: ${v.count} partitions${v.first ? ` (${v.first} → ${v.last})` : ""}`).join(" · ")),
      el("div", { class: "small" }, d.aux_coverage ? `futures parity sessions ${d.aux_coverage.basis_days} · option-OI sessions ${d.aux_coverage.chain_days}` : "no futures/option history loaded (demo or empty archive)"),
      d.spool_files ? note(`${d.spool_files} spool files waiting to be replayed into the archive.`, "warn") : null));
    grid(cov, [txtCol("interval", "interval"), txtCol("first", "first"), txtCol("last", "last"), fmtCol("rows", "rows", 0)], d.coverage);
    for (const [title, rows] of [["Data-quality incidents", d.dq_incidents], ["Audit log", d.audit], ["Zerodha API capability registry (read-only endpoints)", d.registry], ["Derived fields (not Zerodha data)", d.derived_fields]]) {
      const g = el("div");
      body.appendChild(card(title, g));
      if (rows && rows.length) simpleGrid(g, rows, { height: rows.length > 12 ? 360 : undefined }); else g.appendChild(el("div", { class: "small" }, "none"));
    }
  }
  await load();
  const t = setInterval(load, 10000);
  return { unmount: () => clearInterval(t) };
}
