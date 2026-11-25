"""Paper Trading tab: automatic paper trader state, trades, detailed log, equity, and the backtest of its rules."""

from __future__ import annotations

import json
import threading
from dataclasses import asdict

import pandas as pd
from fastapi import APIRouter
from pydantic import BaseModel

from mmm.settings import get_config
from mmm.timeutil import is_market_open, now_ist
from mmm.web.data import bundle, clean, records, state_db

router = APIRouter(prefix="/api/paper")
_bt_lock = threading.Lock()


def _trader():
    from mmm.paper.auto import PaperTrader

    return PaperTrader(state=state_db())


@router.get("")
def paper(log_limit: int = 400):
    tr = _trader()
    cfg = tr.cfg
    t = tr.trades()
    closed = t[t["exit_price"].notna()].copy() if not t.empty else pd.DataFrame()
    pos = tr.position()
    position = None
    if pos is not None:
        q = state_db().quotes([pos.token])
        q = q[0] if q else {}
        mark = q.get("bid") or q.get("last_price")
        position = asdict(pos) | {"mark": mark, "ltp": q.get("last_price"), "quote_ts": q.get("received_ts"),
                                  "unrealised": (mark - pos.entry_price) * pos.qty if mark else None, "invested": pos.entry_price * pos.qty}
    summary = {"capital": cfg.capital, "equity": tr.equity(), "realised": float(closed["pnl_net"].sum()) if not closed.empty else 0.0,
               "closed": len(closed), "wins": int((closed["pnl_net"] > 0).sum()) if not closed.empty else 0,
               "win_rate": float((closed["pnl_net"] > 0).mean()) if not closed.empty else None,
               "costs": float(closed["costs"].sum()) if not closed.empty else 0.0,
               "best": float(closed["pnl_net"].max()) if not closed.empty else None, "worst": float(closed["pnl_net"].min()) if not closed.empty else None}
    if not closed.empty:
        today = now_ist().date()
        cd = pd.to_datetime(closed["exit_ts"], utc=True).dt.tz_convert("Asia/Kolkata").dt.date
        summary["today"] = float(closed.loc[cd == today, "pnl_net"].sum())
        eq = closed.assign(_t=pd.to_datetime(closed["exit_ts"], utc=True)).sort_values("_t")
        first = pd.to_datetime(eq["entry_ts"], utc=True).min()
        equity = [{"time": int(first.timestamp()) + 19800, "value": round(cfg.capital, 2)}]
        for x, v in zip(eq["_t"], eq["pnl_net"].cumsum()):
            tt = int(x.timestamp()) + 19800
            if tt <= equity[-1]["time"]:
                tt = equity[-1]["time"] + 1
            equity.append({"time": tt, "value": round(cfg.capital + v, 2)})
    else:
        summary["today"] = 0.0
        equity = []
    b = bundle()
    if b.is_demo:
        status = "DEMO - waiting for real captured data"
    elif tr.paused():
        status = "PAUSED"
    elif not cfg.enabled:
        status = "DISABLED (paper.enabled: false)"
    elif is_market_open():
        status = "RUNNING"
    else:
        status = "MARKET CLOSED"
    cols = ["trade_id", "entry_ts", "exit_ts", "instrument", "qty", "entry_price", "exit_price", "exit_reason", "pnl_gross", "costs", "pnl_net",
            "mfe", "mae", "stop", "target1", "notes"]
    trades = t[[c for c in cols if c in t.columns]].sort_values("entry_ts", ascending=False) if not t.empty else pd.DataFrame()
    bt = state_db().get_kv("paper_backtest")
    return clean({"status": status, "paused": tr.paused(), "config": cfg.model_dump(), "summary": summary, "position": position,
                  "trades": records(trades), "log": state_db().paper_logs(log_limit), "equity": equity,
                  "backtest": bt, "backtest_status": state_db().get_kv("paper_backtest_status"),
                  "note": "Simulated trades only - this system never sends orders. Fills: live ask on entry, bid on exit, Zerodha costs."})


class PauseReq(BaseModel):
    paused: bool


@router.post("/pause")
def pause(req: PauseReq):
    _trader().set_paused(req.paused)
    return {"paused": req.paused}


class BTReq(BaseModel):
    days: int = 365


@router.post("/backtest")
def run_backtest(req: BTReq):
    """Start the backtest of the paper rules in the background; the page polls /api/paper for the result."""
    if not _bt_lock.acquire(blocking=False):
        return {"started": False, "note": "a backtest is already running"}
    db = state_db()
    db.set_kv("paper_backtest_status", {"state": "RUNNING", "started": now_ist().isoformat(), "days": req.days})

    def _run():
        try:
            from mmm.paper.auto import backtest_paper
            from mmm.research.report import load_index_minutes
            from mmm.signals.auxdata import AuxData

            b = bundle()
            bars = b.minute_bars if b.is_demo else load_index_minutes(req.days or None)
            aux = None if b.is_demo else AuxData.from_archive(bars)
            r = backtest_paper(bars, aux)
            r["demo"] = b.is_demo
            db.set_kv("paper_backtest", json.loads(json.dumps(r, default=str)))
            db.set_kv("paper_backtest_status", {"state": "DONE", "finished": now_ist().isoformat(), "days": req.days})
        except Exception as exc:  # noqa: BLE001 - report the failure on the page
            db.set_kv("paper_backtest_status", {"state": "ERROR", "error": str(exc)})
        finally:
            _bt_lock.release()

    threading.Thread(target=_run, daemon=True).start()
    return {"started": True, "capital": get_config().paper.capital}
