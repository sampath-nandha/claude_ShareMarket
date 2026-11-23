"""Market pages: status, overview, pre-market & opening, price action, futures parity, breadth."""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
from fastapi import APIRouter, HTTPException, Query

from mmm.analytics.breadth import compute_breadth
from mmm.analytics.candles import resample
from mmm.analytics.futures import futures_snapshot
from mmm.analytics.levels import levels_frame
from mmm.analytics.opening import load_preopen
from mmm.analytics.pivots import floor_pivots
from mmm.analytics.structure import candle_anatomy, classify_structure, pivot_swings, swings_frame
from mmm.settings import get_config
from mmm.timeutil import is_market_open, now_ist
from mmm.web.data import (
    DEMO_NOTE,
    DERIVED_NOTE,
    bars_json,
    bundle,
    chart_time,
    clean,
    context,
    line_json,
    records,
    state_db,
)

router = APIRouter(prefix="/api")
TFS = ("minute", "3minute", "5minute", "15minute", "30minute", "60minute", "day")


def _ctx_or_404(b=None):
    ctx = context(b)
    if ctx is None:
        raise HTTPException(409, "not enough history to build context (need ~400 one-minute bars)")
    return ctx


def _levels(levels, drop_minor_round: bool = True) -> list[dict]:
    out = []
    for lv in levels:
        if drop_minor_round and lv.kind == "ROUND" and "major" not in lv.tags:
            continue
        out.append({"price": round(lv.price, 2), "kind": lv.kind, "tf": lv.source_tf, "strength": round(lv.strength, 1), "tags": lv.tags})
    return out


def _swings(swings) -> list[dict]:
    return [{"time": chart_time(s.ts), "kind": s.kind, "price": round(s.price, 2), "label": s.label or ""} for s in swings]


@router.get("/status")
def status():
    from mmm.broker.auth import auth_status

    b = bundle()
    hb = {h["component"]: {"status": h["status"], "ts": h["ts"], **h["detail"]} for h in state_db().heartbeats()}
    try:
        auth = auth_status()
    except Exception as exc:  # noqa: BLE001
        auth = {"error": str(exc)}
    return clean({"demo": b.is_demo, "demo_note": DEMO_NOTE if b.is_demo else None, "derived_note": DERIVED_NOTE,
                  "as_of": b.as_of, "now": now_ist(), "market_open": is_market_open(), "spot": b.spot, "vix": b.vix,
                  "futures_symbol": b.futures_symbol, "futures_last": b.futures_quote.get("last_price"),
                  "tokens": {"index": b.index_token, "vix": b.vix_token, **b.tokens}, "notes": b.notes, "auth": auth, "heartbeats": hb,
                  "source": "SYNTHETIC DEMO" if b.is_demo else "Zerodha Kite via local archive"})


@router.get("/overview")
def overview(tf: str = Query("5minute"), sessions: int = Query(3, ge=1, le=20), instrument: str = Query("spot")):
    if tf not in TFS:
        raise HTTPException(400, f"tf must be one of {TFS}")
    b = bundle()
    ctx = _ctx_or_404(b)
    days = sorted(b.minute_bars["ts"].dt.date.unique())[-sessions:]
    spot = b.minute_bars[b.minute_bars["ts"].dt.date.isin(days)]
    fut = b.futures_bars[b.futures_bars["ts"].dt.date.isin(days)] if not b.futures_bars.empty else pd.DataFrame()
    s_df = resample(spot, tf) if tf != "minute" else spot
    f_df = (resample(fut, tf) if tf != "minute" else fut) if not fut.empty else pd.DataFrame()
    main = f_df if instrument == "fut" and not f_df.empty else s_df
    out = {"tf": tf, "instrument": instrument if not (instrument == "fut" and f_df.empty) else "spot", "symbol": b.futures_symbol if instrument == "fut" else "NIFTY 50",
           "bars": bars_json(main), "ema20": line_json(main["ts"], main["close"].ewm(span=20, adjust=False).mean()) if not main.empty else [],
           "ema50": line_json(main["ts"], main["close"].ewm(span=50, adjust=False).mean()) if not main.empty else [],
           "overlay": line_json(f_df["ts"], f_df["close"]) if instrument == "overlay" and not f_df.empty else [],
           "levels": _levels(ctx.levels) if instrument != "fut" else [],
           "swings": _swings(pivot_swings(main.reset_index(drop=True), 3, 3)) if len(main) > 10 else []}
    out["metrics"] = {"spot": b.spot, "regime": ctx.regime.code, "direction": ctx.regime.direction, "strength": ctx.regime.strength,
                      "volatility": ctx.regime.volatility, "behaviour": ctx.regime.behaviour, "atr": ctx.atr,
                      "setup_structure": ctx.setup_structure.trend, "setup_event": ctx.setup_structure.last_event,
                      "context_structure": ctx.context_structure.trend, "context_event": ctx.context_structure.last_event}
    if b.futures_quote.get("last_price"):
        basis = float(b.futures_quote["last_price"]) - b.spot
        out["basis"] = {"symbol": b.futures_symbol, "last": b.futures_quote["last_price"], "basis": basis, "basis_pct": basis / b.spot * 100}
    rows = []
    for name, frame, left in (("60minute", ctx.trend, 2), ("15minute", ctx.context, 3), ("5minute", ctx.setup, 3)):
        st = classify_structure(frame, pivot_swings(frame, left, left))
        rows.append({"timeframe": name, "trend": st.trend, "last_event": st.last_event, "last_swing_high": st.last_high.price if st.last_high else None,
                     "last_swing_low": st.last_low.price if st.last_low else None, "labels": " ".join(st.labels[-6:])})
    if len(ctx.daily) > 10:
        d = ctx.daily.reset_index(drop=True)
        st = classify_structure(d, pivot_swings(d, 2, 2))
        rows.insert(0, {"timeframe": "day", "trend": st.trend, "last_event": st.last_event, "last_swing_high": st.last_high.price if st.last_high else None,
                        "last_swing_low": st.last_low.price if st.last_low else None, "labels": " ".join(st.labels[-6:])})
    out["structure"] = rows
    orr = []
    it = ctx.intraday_today
    if not it.empty:
        start = it["ts"].iloc[0]
        for m in (5, 15, 30):
            w = it[it["ts"] < start + pd.Timedelta(minutes=m)]
            orr.append({"minutes": m, "low": float(w["low"].min()), "high": float(w["high"].max()), "width": float(w["high"].max() - w["low"].min()),
                        "complete": bool(it["ts"].iloc[-1] >= start + pd.Timedelta(minutes=m - 1))})
    out["opening_ranges"] = orr
    lf = levels_frame(ctx.levels)
    if not lf.empty:
        lf = lf[~((lf["kind"] == "ROUND") & (lf["strength"] < 40))].copy()
        lf["side"] = ["R" if p > b.spot else "S" for p in lf["price"]]
        out["sr_table"] = records(lf.sort_values("price", ascending=False)[["side", "kind", "price", "strength", "touches", "tf"]])
    else:
        out["sr_table"] = []
    return clean(out)


@router.get("/premarket")
def premarket():
    """Level sheet for today's session plus live opening state (gap, opening type, initial balance, pre-open)."""
    from mmm.research.features import FEATURES, statuses

    b = bundle()
    ctx = _ctx_or_404(b)
    daily = ctx.daily
    out: dict = {"date": ctx.today, "as_of": b.as_of, "spot": b.spot}
    if not daily.empty:
        p = daily.iloc[-1]
        out["previous_session"] = {"date": p["ts"].date(), "open": p["open"], "high": p["high"], "low": p["low"], "close": p["close"],
                                   "range": p["high"] - p["low"]}
        out["pivots"] = floor_pivots(float(p["high"]), float(p["low"]), float(p["close"]))
    ex = ctx.extras
    out["cpr"] = ex.get("cpr")
    out["clusters"] = [c.as_dict() for c in (ex.get("clusters") or [])[:12]]
    out["gap"] = ex.get("gap")
    out["open15"], out["open30"] = ex.get("open15"), ex.get("open30")
    out["ib30"], out["ib60"] = ex.get("ib30"), ex.get("ib60")
    sheet = []
    for lv in ctx.levels:
        if lv.kind in ("ROUND",) and "major" not in lv.tags:
            continue
        if lv.kind.startswith(("ZONE",)) and lv.strength < 55:
            continue
        sheet.append({"price": round(lv.price, 2), "kind": lv.kind, "strength": round(lv.strength, 1), "distance": round(lv.price - b.spot, 1),
                      "side": "R" if lv.price > b.spot else "S", "tags": ", ".join(lv.tags[:4])})
    out["level_sheet"] = sorted(sheet, key=lambda r: -r["price"])
    if b.is_demo or b.index_token is None:
        out["preopen"] = {"available": False, "note": "demo data: pre-open is read from archived collector ticks"}
    else:
        cons = [int(t) for t in b.tokens.get("constituents", {})]
        out["preopen"] = load_preopen(ctx.today, b.index_token, cons, float(daily.iloc[-1]["close"]) if not daily.empty else None)
    reg = statuses()
    out["features"] = [{"key": f.key, "phase": f.phase, "description": f.description, "status": reg.get(f.key, {}).get("status", "UNTESTED"),
                        "active": f.rule(ctx) if f.group == "session" else None} for f in FEATURES]
    it = ctx.intraday_today
    out["bars"] = bars_json(it)
    out["levels"] = _levels([lv for lv in ctx.levels if lv.kind.startswith(("PIV_", "CPR_", "CONF_", "PD", "PW", "OR"))], False)
    return clean(out)


@router.get("/priceaction")
def priceaction():
    from mmm.signals.engine import stage1
    from mmm.strategies import load_strategies

    b = bundle()
    ctx = _ctx_or_404(b)
    strategies = load_strategies()
    cands = stage1(ctx, strategies)
    days = sorted(ctx.setup["ts"].dt.date.unique())[-2:]
    view = ctx.setup[ctx.setup["ts"].dt.date.isin(days)]
    sw = [s for s in ctx.setup_swings if not view.empty and s.ts >= view["ts"].iloc[0]]
    sf = swings_frame(ctx.setup_swings).tail(12)
    an = candle_anatomy(ctx.setup).tail(8).reset_index(drop=True)
    last = ctx.setup[["ts", "open", "high", "low", "close"]].tail(8).reset_index(drop=True)
    pats = []
    for _, r in an.iterrows():
        pats.append(", ".join(k for k in ("inside", "outside", "bull_engulf", "bear_engulf", "pin_bull", "pin_bear", "momentum", "narrow", "wide") if bool(r[k])))
    anatomy = pd.concat([last, an[["body_ratio", "upper_wick", "lower_wick", "close_loc"]]], axis=1).assign(patterns=pats)
    patterns = _patterns(ctx, view)
    return clean({
        "patterns": patterns,
        "bars": bars_json(view), "swings": _swings(sw), "levels": _levels(ctx.levels),
        "structure": {"setup": [ctx.setup_structure.trend, ctx.setup_structure.last_event, " ".join(ctx.setup_structure.labels[-8:])],
                      "context": [ctx.context_structure.trend, ctx.context_structure.last_event], "trend": [ctx.trend_structure.trend, ctx.trend_structure.last_event],
                      "regime": ctx.regime.code},
        "swing_table": records(sf.assign(confirmed_bars_later=sf["confirmed_at"] - sf["idx"])[["ts", "kind", "label", "price", "confirmed_bars_later"]]) if not sf.empty else [],
        "anatomy": records(anatomy),
        "candidates": [{"strategy": s.id, "name": s.name, "status": s.status, **c.model_dump(mode="json"), "risk": c.risk, "reward_risk": c.reward_risk}
                       for s, c in cands],
        "library": [s.spec() for s in strategies]})


def _patterns(ctx, view) -> dict:
    """Candlestick pattern trial: markers on the visible 5m bars and the patterns on the decision bar."""
    from mmm.analytics.patterns import DESCRIPTIONS, FAMILY, contextual_direction, latest, pattern_frame
    from mmm.research.features import statuses

    cfg = get_config().patterns
    if not cfg.enabled:
        return {"enabled": False}
    pf = pattern_frame(ctx.setup)
    pv = pf.iloc[len(ctx.setup) - len(view):] if len(view) else pf.iloc[0:0]
    colors = {"momentum": "#ffb74d", "reversal": "#ba68c8", "indecision": "#90a4ae"}
    marks = []
    for (i, row), ts in zip(pv.iterrows(), view["ts"]):
        hit = [k for k, v in row.items() if v]
        if not hit:
            continue
        fam = FAMILY[hit[0]]
        marks.append({"time": chart_time(ts), "text": "/".join(k.split("_")[0][:4] for k in hit[:2]), "family": fam, "color": colors[fam],
                      "direction": int(row[hit[0]]) if fam != "indecision" else 0})
    reg = statuses()
    raw = latest(ctx)
    now = [{"pattern": k, "family": FAMILY[k], "raw_direction": int(v), "in_context_direction": contextual_direction(k, v, ctx),
            "status": reg.get(f"pat_{k}", {}).get("status", "UNTESTED"), "description": DESCRIPTIONS[k]} for k, v in raw.items() if v]
    return {"enabled": True, "markers": marks[-120:], "latest": now, "observe_from": cfg.observe_from}


@router.get("/patterns/watch")
def pattern_watch():
    """Live watch of the pattern trial: every in-context pattern event since `patterns.observe_from`, with its
    30-minute outcome after costs. Small samples - read with the historical feature study, not instead of it."""
    from datetime import date as _date

    from mmm.research.features import PATTERN_FEATURES, collect_events, statuses
    from mmm.web.data import aux as _aux
    from mmm.web.data import cached

    cfg = get_config().patterns
    if not cfg.enabled:
        return {"enabled": False, "note": "pattern trial disabled (patterns.enabled: false)"}
    b = bundle(days=60)
    start = _date.fromisoformat(cfg.observe_from)
    days = [d for d in sorted(b.minute_bars["ts"].dt.date.unique()) if d >= start]

    def _run():
        return collect_events(b.minute_bars, _aux(b), days, features=PATTERN_FEATURES) if days else pd.DataFrame()

    ev = cached(("pattern_watch", b.as_of.replace(second=0, microsecond=0), tuple(days)), 300, _run)
    reg = statuses()
    summary = []
    for f in PATTERN_FEATURES:
        e = ev[ev["feature"] == f.key] if not ev.empty else ev
        n = len(e)
        summary.append({"pattern": f.key[4:], "status": reg.get(f.key, {}).get("status", "UNTESTED"), "events": n,
                        "hits": int(e["hit"].sum()) if n else 0, "hit_rate": float(e["hit"].mean()) if n else None,
                        "mean_net_pts": float(e["net"].mean()) if n else None, "sum_net_pts": float(e["net"].sum()) if n else None})
    return clean({"enabled": True, "observe_from": cfg.observe_from, "sessions": days, "demo": b.is_demo, "summary": summary,
                  "events": records(ev.assign(pattern=ev["feature"].str[4:]).drop(columns="feature").sort_values("ts", ascending=False)) if not ev.empty else [],
                  "note": "30-minute outcome after a 2-point round-trip cost, same definition as the feature study. One week is a small sample."})


@router.get("/futures")
def futures(direction: str = Query("BULLISH")):
    from mmm.analytics.basis import basis_frame, basis_state

    b = bundle()
    q = b.futures_quote
    today = b.minute_bars["ts"].iloc[-1].date()
    day = b.minute_bars[b.minute_bars["ts"].dt.date == today]
    out: dict = {"symbol": b.futures_symbol, "direction": direction}
    d = 1 if direction == "BULLISH" else -1
    if q:
        prev_oi = int(q.get("oi", 0) * 0.98) if b.is_demo and q.get("oi") else (q.get("prev_oi") or None)
        snap = futures_snapshot(q, b.spot, prev_oi, day, q.get("volume", 0) / 1.1 if b.is_demo and q.get("volume") else None, d)
        out["snapshot"] = snap.__dict__
    # parity series for today (DERIVED)
    fut = b.futures_bars
    if not fut.empty:
        fd = fut[fut["ts"].dt.date == today].copy()
        if not fd.empty:
            exp = None
            if not b.is_demo:
                from mmm.data.futures_series import futures_contracts

                fc = futures_contracts()
                row = fc[fc["tradingsymbol"] == b.futures_symbol]
                exp = row["expiry"].iloc[0] if not row.empty else None
            from mmm.data.futures_series import monthly_expiry

            fd["expiry"] = exp or monthly_expiry(today)
            bf = basis_frame(b.minute_bars[b.minute_bars["ts"].dt.date >= today - timedelta(days=4)],
                             pd.concat([fut[fut["ts"].dt.date < today].assign(expiry=fd["expiry"].iloc[0]), fd]), z_min=60)
            bt = bf[bf["ts"].dt.date == today]
            st = basis_state(bt)
            out["basis_state"] = st
            out["series"] = {"basis": line_json(bt["ts"], bt["basis"]), "fair": line_json(bt["ts"], bt["fair"]),
                             "premium": line_json(bt["ts"], bt["premium"]), "z": line_json(bt["ts"], bt["z"]),
                             "oi": line_json(fd["ts"], fd["oi"])}
            out["bars"] = bars_json(fd)
            marks = []
            for i in range(35, len(bt), 5):
                from mmm.analytics.basis import divergence

                dv = divergence(bt.iloc[: i + 1], 30)
                if dv is not None:
                    marks.append({"time": chart_time(bt["ts"].iloc[i]), "kind": dv.kind, "text": f"{dv.kind[:4]} div {dv.premium_change:+.1f}"})
            out["divergences"] = marks
    out["grid"] = [{"price": "up", "oi": "up", "label": "LONG_BUILDUP", "read": "new longs"},
                   {"price": "down", "oi": "up", "label": "SHORT_BUILDUP", "read": "new shorts"},
                   {"price": "up", "oi": "down", "label": "SHORT_COVERING", "read": "shorts exiting"},
                   {"price": "down", "oi": "down", "label": "LONG_UNWINDING", "read": "longs exiting"}]
    out["notes"] = ["Price/OI labels are conventions, not proof of who is positioned.", "Basis vs fair value is DERIVED with configured r/q."]
    return clean(out)


@router.get("/breadth")
def breadth():
    b = bundle()
    if b.constituents.empty:
        return {"available": False, "note": "no constituent quotes captured yet"}
    br = compute_breadth(b.constituents, b.universe_size)
    t = br.table.copy() if br.table is not None else pd.DataFrame()
    return clean({"available": True, "summary": {k: v for k, v in br.__dict__.items() if k != "table"},
                  "table": records(t.sort_values("ret_pct")) if not t.empty else [], "universe": get_config().universe.constituents})
