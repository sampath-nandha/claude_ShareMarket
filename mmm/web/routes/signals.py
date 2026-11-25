"""Live signals (two-stage engine), paper/manual journal, read-only account and risk limits."""

from __future__ import annotations

import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from mmm.data.quality import DQ
from mmm.paper.journal import Journal
from mmm.risk.limits import RiskState, check
from mmm.settings import get_config
from mmm.signals.confirm import confirmations
from mmm.timeutil import now_ist
from mmm.web.data import bars_json, bundle, clean, context, records, state_db

router = APIRouter(prefix="/api")


@router.get("/signals")
def signals():
    from mmm.signals.engine import stage1, stage2
    from mmm.strategies import load_strategies

    b = bundle()
    ctx = context(b)
    if ctx is None:
        raise HTTPException(409, "not enough history")
    sigs = [stage2(ctx, s, c, confirmations(b, ctx, c)) for s, c in stage1(ctx, load_strategies())]
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
