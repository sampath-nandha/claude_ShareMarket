"""Live signals (two-stage engine), paper/manual journal, read-only account and risk limits."""

from __future__ import annotations

import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from mmm.analytics.breadth import compute_breadth
from mmm.analytics.futures import futures_snapshot
from mmm.analytics.options.chain import summarise_chain
from mmm.analytics.options.selector import best_option, rank_options
from mmm.data.quality import DQ
from mmm.paper.journal import Journal
from mmm.risk.limits import RiskState, check
from mmm.settings import get_config
from mmm.timeutil import now_ist
from mmm.web.data import bars_json, bundle, clean, context, records, state_db

router = APIRouter(prefix="/api")


def _confirmations(b, ctx, cand):
    from mmm.signals.engine import Confirmations

    cfg = get_config()
    d = 1 if cand.direction == "BULLISH" else -1
    fut = None
    if b.futures_quote:
        day = b.minute_bars[b.minute_bars["ts"].dt.date == b.as_of.date()]
        prev_oi = int(b.futures_quote.get("oi", 0) * 0.98) if b.is_demo and b.futures_quote.get("oi") else None
        fs = futures_snapshot(b.futures_quote, b.spot, prev_oi, day, b.futures_quote.get("volume", 0) / 1.1 if b.is_demo and b.futures_quote.get("volume") else None, d)
        fut = {"confirmation_score": fs.confirmation_score, "basis": fs.basis, "oi_change_pct": fs.oi_change_pct, "rel_volume": fs.rel_volume,
               "price_oi_label": fs.price_oi_label, "notes": fs.notes}
        bs = ctx.extras.get("basis")
        if bs is not None:
            fut["premium_vs_fair"] = bs.premium
            fut["premium_z"] = bs.z
            fut["divergence"] = bs.divergence.kind if bs.divergence else None
    opts, rec, alts, liq, prem_risk = None, None, [], None, None
    if not b.chain.empty and b.expiries:
        e = b.expiries[0]
        summ = summarise_chain(b.chain, b.spot, e, 2 * cfg.universe.option_strikes_each_side + 1)
        score = 0.0
        if summ.pcr_oi is not None:
            score += 0.3 * d * (1 if summ.pcr_oi > 1.1 else -1 if summ.pcr_oi < 0.8 else 0)
        if summ.pcr_oi_change is not None and summ.pcr_oi_change > 0:
            score += 0.2 * d * (1 if summ.pcr_oi_change > 1 else -1)
        opts = {"confirmation_score": max(min(score, 1), -1), "pcr_oi": summ.pcr_oi, "max_pain": summ.max_pain, "call_wall": summ.call_wall,
                "put_wall": summ.put_wall, "atm_iv": summ.atm_iv, "expected_move": summ.expected_move, "coverage": summ.coverage}
        oi = ctx.extras.get("oi")
        if oi is not None:
            opts["pcr_near_trend"] = oi.pcr_trend
            opts["call_wall_status"] = oi.call_wall.status if oi.call_wall else None
            opts["put_wall_status"] = oi.put_wall.status if oi.put_wall else None
        cands = rank_options(b.chain[pd.to_datetime(b.chain["expiry"]).dt.date == e], d, b.spot, cand.target1, cand.stop)
        bo = best_option(cands)
        alts = [c.as_dict() for c in cands]
        if bo:
            rec = bo.as_dict()
            liq = {"score": bo.liquidity_score / 100, "spread_pct": bo.spread_pct}
            prem_risk = (bo.premium - bo.expected_premium_at_stop) if bo.expected_premium_at_stop is not None else bo.premium * 0.4
    brd = None
    if not b.constituents.empty:
        bb = compute_breadth(b.constituents, b.universe_size)
        brd = {"score": bb.score, "authoritative": bb.authoritative, "coverage_pct": bb.coverage_pct, "advancing": bb.advancing, "declining": bb.declining}
    return Confirmations(fut, opts, brd, liq, rec, alts, DQ.PARTIAL if b.is_demo else DQ.VALID, prem_risk)


@router.get("/signals")
def signals():
    from mmm.signals.engine import stage1, stage2
    from mmm.strategies import load_strategies

    b = bundle()
    ctx = context(b)
    if ctx is None:
        raise HTTPException(409, "not enough history")
    sigs = [stage2(ctx, s, c, _confirmations(b, ctx, c)) for s, c in stage1(ctx, load_strategies())]
    db = state_db()
    if not b.is_demo:
        for s in sigs:
            db.upsert_signal(s.to_row())
    hist = db.signals(100)
    view = ctx.setup.tail(80)
    return clean({"decision_bar": ctx.setup["ts"].iloc[-1], "regime": ctx.regime.code, "demo": b.is_demo,
                  "signals": [s.to_row() for s in sigs], "bars": bars_json(view),
                  "recent": [{k: h.get(k) for k in ("created_at", "strategy_id", "direction", "status", "score", "entry", "stop", "target1", "regime")} for h in hist]})


class OpenReq(BaseModel):
    mode: str = "PAPER"
    instrument: str
    direction: str = "BUY"
    qty: int = 75
    price: float
    signal_id: str | None = None
    stop: float | None = None
    target1: float | None = None
    target2: float | None = None
    notes: str = ""


class CloseReq(BaseModel):
    trade_id: str
    price: float
    reason: str = "MANUAL"


@router.post("/journal/open")
def journal_open(req: OpenReq):
    if req.mode not in ("PAPER", "MANUAL"):
        raise HTTPException(400, "mode must be PAPER or MANUAL")
    b = bundle()
    tid = Journal(state_db()).open_trade(req.mode, req.instrument, req.direction, int(req.qty), float(req.price), stop=req.stop, target1=req.target1,
                                         target2=req.target2, signal_id=req.signal_id, notes=req.notes, payload={"demo": b.is_demo})
    return {"trade_id": tid}


@router.post("/journal/close")
def journal_close(req: CloseReq):
    try:
        t = Journal(state_db()).close_trade(req.trade_id, float(req.price), req.reason)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return clean(t)


@router.get("/risk")
def risk(mode: str = "ALL"):
    cfg = get_config().risk
    db = state_db()
    j = Journal(db)
    mf = None if mode == "ALL" else mode
    df = j.frame(mf)
    out: dict = {"summary": j.summary(mf), "trades": records(df), "open": [t["trade_id"] for t in j.open_trades(mf)]}
    closed_today = df[(df["exit_price"].notna()) & (pd.to_datetime(df["created_at"]).dt.date == now_ist().date())] if not df.empty else pd.DataFrame()
    daily = float(closed_today["pnl_net"].sum()) if not closed_today.empty else 0.0
    consec = 0
    if not closed_today.empty:
        for v in closed_today.sort_values("exit_ts")["pnl_net"][::-1]:
            if v < 0:
                consec += 1
            else:
                break
    st = RiskState(daily_pnl=daily, consecutive_losses=consec, recommendations_today=len(db.signals(since=now_ist().replace(hour=0, minute=0))),
                   open_positions=len(j.open_trades()))
    rc = check(st, 1, 2.0, 30.0, 0.5, now_ist(), now_ist(), DQ.VALID)
    out["limits"] = {"utilisation": rc.utilisation, "blocked": rc.reasons, "daily_pnl": daily, "consecutive_losses": consec,
                     "recommendations_today": st.recommendations_today, "open_positions": st.open_positions, "config": cfg.model_dump()}
    return clean(out)


@router.get("/account")
def account():
    """Read-only Zerodha margins and positions (needs today's login)."""
    try:
        from mmm.broker.auth import get_client

        k = get_client()
        marg, pos = k.margins(), k.positions()
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "note": f"account data unavailable ({exc}); log in with `mmm auth login`"}
    eq = marg.get("equity", {})
    net = pd.DataFrame(pos.get("net", []))
    return clean({"available": True, "net": eq.get("net"), "cash": eq.get("available", {}).get("cash"),
                  "live_balance": eq.get("available", {}).get("live_balance"), "utilised": eq.get("utilised", {}).get("debits"),
                  "positions": records(net[["tradingsymbol", "quantity", "average_price", "last_price", "pnl", "m2m"]]) if not net.empty else []})


@router.get("/account/reconcile")
def reconcile():
    try:
        from mmm.broker.auth import get_client
        from mmm.paper.reconcile import reconcile as _rec

        res = _rec(get_client(), state_db())
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "note": str(exc)}
    return clean({"available": True, **{k: records(v) for k, v in res.items()}})
