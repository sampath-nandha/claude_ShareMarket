from __future__ import annotations

import pandas as pd
import psutil
import streamlit as st

from mmm.broker.auth import auth_status
from mmm.broker.capability_registry import DERIVED_FIELDS, REGISTRY
from mmm.dashboard.common import page_header, state_db
from mmm.data import parquet_store, paths
from mmm.data.instruments import load_instruments
from mmm.settings import get_config
from mmm.timeutil import is_market_open, now_ist

st.set_page_config(page_title="System Health", layout="wide")
page_header("System Health", "Auth, collector, archive coverage, data-quality incidents, resources")
db = state_db()
cfg = get_config()

c = st.columns(5)
a = auth_status()
c[0].metric("Kite API key", "configured" if a["api_key_configured"] else "MISSING")
c[1].metric("Access token", "valid today" if a["token_valid_today"] else "NOT VALID", a["token_issued_on"] or "")
c[2].metric("Market", "OPEN" if is_market_open() else "CLOSED", now_ist().strftime("%H:%M:%S"))
c[3].metric("CPU", f"{psutil.cpu_percent():.0f}%")
mem = psutil.virtual_memory()
c[4].metric("Memory", f"{mem.percent:.0f}%", f"{mem.used/1e9:.1f} / {mem.total/1e9:.1f} GB")
du = psutil.disk_usage(str(paths.data_dir()))
st.progress(int(du.percent), text=f"Data disk {paths.data_dir()} — {du.percent:.0f}% used ({du.free/1e9:.0f} GB free)")

st.subheader("Component heartbeats")
hb = db.heartbeats()
if hb:
    rows = []
    for h in hb:
        age = (now_ist() - pd.Timestamp(h["ts"])).total_seconds()
        rows.append({"component": h["component"], "status": h["status"], "age_s": round(age), **{k: v for k, v in h["detail"].items() if k != "detail"}})
    df = pd.DataFrame(rows)
    st.dataframe(df, hide_index=True, width="stretch")
    col = df[df["component"] == "collector"]
    if not col.empty and is_market_open() and (col.iloc[0]["status"] != "UP" or col.iloc[0]["age_s"] > 60):
        st.error("Collector is not healthy during market hours.")
else:
    st.info("No heartbeats yet — the collector/scheduler have not run.")

st.subheader("Archive coverage")
inst = load_instruments()
if inst.empty:
    st.warning("No instrument snapshot. Run `mmm data instruments`.")
else:
    idx = inst[(inst["exchange"] == cfg.universe.index_exchange) & (inst["tradingsymbol"] == cfg.universe.index_symbol)]
    st.write(f"Instrument snapshot rows: {len(inst):,} · latest snapshot: {inst['snapshot_date'].max().date() if 'snapshot_date' in inst else 'n/a'}")
    if not idx.empty:
        tok = int(idx.iloc[0]["instrument_token"])
        rows = []
        for iv in cfg.historical.intervals:
            f, l, n = parquet_store.candle_coverage(iv, tok)
            rows.append({"interval": iv, "first": f, "last": l, "rows": n})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
for kind in ("ticks", "depth", "option_chain"):
    ds = parquet_store.available_dates(kind)
    st.write(f"**{kind}** partitions: {len(ds)}" + (f" ({ds[0]} → {ds[-1]})" if ds else ""))
spool = list(paths.spool_dir().glob("*.parquet"))
if spool:
    st.warning(f"{len(spool)} spool files waiting to be replayed into the archive.")

st.subheader("Data-quality incidents")
inc = db.dq_incidents(100)
st.dataframe(pd.DataFrame(inc), hide_index=True, width="stretch") if inc else st.write("none")
st.subheader("Audit log")
aud = db.recent_audit(50)
st.dataframe(pd.DataFrame(aud), hide_index=True, width="stretch") if aud else st.write("none")

with st.expander("Zerodha API capability registry (read-only endpoints used)"):
    st.dataframe(pd.DataFrame([{"endpoint": e.name, "method": e.method, "path": e.path, "rate limit": e.rate_limit, "availability": e.availability, "consumers": ", ".join(e.consumers)}
                               for e in REGISTRY.values()]), hide_index=True, width="stretch")
    st.write("**Derived fields (not Zerodha data):**")
    st.table(pd.DataFrame([{"field": k, "definition": v} for k, v in DERIVED_FIELDS.items()]))
