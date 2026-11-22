"""Market replay and system health."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import psutil
from fastapi import APIRouter, HTTPException

from mmm.broker.capability_registry import DERIVED_FIELDS, REGISTRY
from mmm.data import parquet_store, paths
from mmm.data.instruments import load_instruments
from mmm.settings import get_config
from mmm.timeutil import is_market_open, now_ist
from mmm.web.data import aux, bars_json, bundle, clean, records, state_db

router = APIRouter(prefix="/api")
MIN_BARS = 31


@router.get("/replay/sessions")
def replay_sessions():
    b = bundle(days=60)
    days = sorted(b.minute_bars["ts"].dt.date.unique())
    counts = b.minute_bars.groupby(b.minute_bars["ts"].dt.date).size()
    return clean({"sessions": [{"date": d, "bars": int(counts[d])} for d in reversed(days)], "demo": b.is_demo})


@router.get("/replay")
def replay(day: date, i: int = 60):
    from mmm.signals.engine import build_context, stage1
    from mmm.strategies import load_strategies

    b = bundle(days=60)
    bars = b.minute_bars[b.minute_bars["ts"].dt.date == day].reset_index(drop=True)
    n = len(bars)
    if n < MIN_BARS:
        raise HTTPException(409, f"session {day} has only {n} captured minute bars (need {MIN_BARS})")
    i = int(min(max(i, 30), n))
    cursor = bars["ts"].iloc[i - 1] + timedelta(minutes=1)
    past = b.minute_bars[b.minute_bars["ts"] < cursor]
    ctx = build_context(past, cursor.to_pydatetime(), aux=aux(b))
    out: dict = {"day": day, "i": i, "n": n, "cursor": cursor, "bars": bars_json(bars.iloc[:i])}
    if ctx is None:
        out["note"] = "not enough history before this point"
        return clean(out)
    cands = stage1(ctx, load_strategies())
    future = bars.iloc[i:]
    rows = []
    for s, c in cands:
        outcome = "neither hit by session end"
        if not future.empty:
            d = 1 if c.direction == "BULLISH" else -1
            hs = (future["low"] <= c.stop) if d > 0 else (future["high"] >= c.stop)
            ht = (future["high"] >= c.target1) if d > 0 else (future["low"] <= c.target1)
            fs = hs.idxmax() if hs.any() else None
            ft = ht.idxmax() if ht.any() else None
            if fs is not None and (ft is None or fs <= ft):
                outcome = "STOP first"
            elif ft is not None:
                outcome = "T1 first"
        rows.append({"strategy": s.id, **c.model_dump(mode="json"), "reward_risk": c.reward_risk, "outcome": outcome})
    out.update({"regime": ctx.regime.code, "setup_structure": ctx.setup_structure.trend, "context_structure": ctx.context_structure.trend,
                "atr": ctx.atr, "levels": [{"price": lv.price, "kind": lv.kind, "strength": lv.strength} for lv in ctx.levels
                                           if not (lv.kind == "ROUND" and "major" not in lv.tags)], "candidates": rows,
                "note": "outcomes are revealed only here, never to the engine"})
    return clean(out)


@router.get("/health")
def health():
    from mmm.broker.auth import auth_status

    cfg = get_config()
    db = state_db()
    try:
        a = auth_status()
    except Exception as exc:  # noqa: BLE001
        a = {"error": str(exc)}
    mem = psutil.virtual_memory()
    du = psutil.disk_usage(str(paths.data_dir()))
    hb = []
    for h in db.heartbeats():
        age = (now_ist() - pd.Timestamp(h["ts"])).total_seconds()
        hb.append({"component": h["component"], "status": h["status"], "age_s": round(age), **{k: v for k, v in h["detail"].items() if k != "detail"}})
    inst = load_instruments()
    cov = []
    snap = None
    if not inst.empty:
        snap = inst["snapshot_date"].max() if "snapshot_date" in inst else None
        idx = inst[(inst["exchange"] == cfg.universe.index_exchange) & (inst["tradingsymbol"] == cfg.universe.index_symbol)]
        if not idx.empty:
            tok = int(idx.iloc[0]["instrument_token"])
            for iv in cfg.historical.intervals:
                f, last, n = parquet_store.candle_coverage(iv, tok)
                cov.append({"interval": iv, "first": f, "last": last, "rows": n})
    parts = {}
    for kind in ("ticks", "depth", "option_chain"):
        ds = parquet_store.available_dates(kind)
        parts[kind] = {"count": len(ds), "first": ds[0] if ds else None, "last": ds[-1] if ds else None}
    b = bundle()
    ax = aux(b)
    collector = next((h for h in hb if h["component"] == "collector"), None)
    alert = bool(collector and is_market_open() and (collector["status"] != "UP" or collector["age_s"] > 60))
    return clean({
        "auth": a, "market_open": is_market_open(), "now": now_ist(), "cpu_pct": psutil.cpu_percent(), "mem_pct": mem.percent,
        "mem_used_gb": mem.used / 1e9, "mem_total_gb": mem.total / 1e9, "disk_pct": du.percent, "disk_free_gb": du.free / 1e9,
        "data_dir": str(paths.data_dir()), "heartbeats": hb, "collector_alert": alert, "instrument_rows": len(inst), "snapshot_date": snap,
        "coverage": cov, "partitions": parts, "spool_files": len(list(paths.spool_dir().glob("*.parquet"))),
        "aux_coverage": ax.coverage() if ax is not None else None,
        "dq_incidents": db.dq_incidents(100), "audit": db.recent_audit(50),
        "registry": [{"endpoint": e.name, "method": e.method, "path": e.path, "rate_limit": e.rate_limit, "availability": e.availability,
                      "consumers": ", ".join(e.consumers)} for e in REGISTRY.values()],
        "derived_fields": [{"field": k, "definition": v} for k, v in DERIVED_FIELDS.items()],
        "tickbus": "ZeroMQ " + __import__("mmm.data.tickbus", fromlist=["ADDRESS"]).ADDRESS})


@router.get("/coverage/detail")
def coverage_detail():
    """Per-instrument candle coverage (same data as `mmm data inventory --detail`)."""
    import glob

    import duckdb

    inst = load_instruments()
    names = dict(zip(inst["instrument_token"].astype(int), inst["tradingsymbol"])) if not inst.empty else {}
    con = duckdb.connect()
    con.execute("SET TimeZone='Asia/Kolkata'")
    rows = []
    base = paths.data_dir() / "candles"
    for ivdir in sorted(base.glob("interval=*")) if base.exists() else []:
        for tdir in sorted(ivdir.glob("token=*")):
            pat = str(tdir / "*.parquet")
            if not glob.glob(pat):
                continue
            n, lo, hi = con.execute(f"SELECT count(*), min(ts), max(ts) FROM read_parquet('{pat}')").fetchone()
            tok = int(tdir.name.split("=", 1)[1])
            rows.append({"interval": ivdir.name.split("=", 1)[1], "token": tok, "symbol": names.get(tok, ""), "rows": n, "first": lo, "last": hi})
    con.close()
    return clean({"rows": records(pd.DataFrame(rows))})
