"""Backtesting, walk-forward, calibration, option-leg evaluation, research and feature reports, registries."""

from __future__ import annotations

import json
import threading
from datetime import date

import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from mmm.backtest.engine import BacktestConfig, run_backtest
from mmm.backtest.metrics import breakdown, compute_stats, cost_sensitivity
from mmm.settings import get_config
from mmm.web.data import bundle, clean, records

router = APIRouter(prefix="/api")
_last: dict[str, object] = {}
_lock = threading.Lock()


class BacktestReq(BaseModel):
    strategies: list[str] | None = None
    start: date | None = None
    end: date | None = None
    point_value: float = 75.0
    cost_per_trade: float = 60.0
    slippage_points: float = 1.0
    max_hold_bars: int = 120


def _cfg(req: BacktestReq) -> BacktestConfig:
    return BacktestConfig(start=req.start, end=req.end, point_value=req.point_value, cost_per_trade=req.cost_per_trade,
                          slippage_points=req.slippage_points, max_hold_bars=req.max_hold_bars, strategies=req.strategies)


@router.get("/backtest/options")
def backtest_options():
    from mmm.strategies import load_strategies

    b = bundle(days=60)
    sessions = sorted(b.minute_bars["ts"].dt.date.unique())
    return clean({"strategies": [s.id for s in load_strategies()], "sessions": sessions, "demo": b.is_demo,
                  "validation": get_config().validation.model_dump()})


@router.post("/backtest")
def backtest(req: BacktestReq):
    b = bundle(days=60)
    from mmm.web.data import aux

    res = run_backtest(b.minute_bars, _cfg(req), aux=aux(b), log_every=0)
    t = res.trades
    with _lock:
        _last["bt"] = res
    out: dict = {"notes": res.notes, "sessions": res.sessions, "candidates": res.candidates, "trades_n": res.accepted, "demo": b.is_demo}
    if t.empty:
        return clean(out)
    s = compute_stats(t)
    eq = t.sort_values("exit_ts")
    t2 = t.assign(regime_dir=t["regime"].str.split("|").str[0])
    out.update({
        "stats": s.as_dict() | {"reasons": s.reasons},
        "equity": {"time": [int(pd.Timestamp(x).timestamp()) + 19800 for x in eq["exit_ts"]], "net": eq["pnl_net"].cumsum().round(0).tolist(),
                   "gross": eq["pnl_gross"].cumsum().round(0).tolist()},
        "by": {k: records(breakdown(t2 if k == "regime_dir" else t, k)) for k in ("strategy_id", "direction", "regime_dir", "hour", "weekday", "exit_reason")},
        "cost_sensitivity": records(cost_sensitivity(t)),
        "directional_hit": float(t["directional_hit"].mean()),
        "trades": records(t.sort_values("entry_ts", ascending=False).head(2000)),
    })
    return clean(out)


class WFReq(BacktestReq):
    train_days: int = 60
    test_days: int = 15
    holdout_days: int = 10


@router.post("/backtest/walkforward")
def walkforward(req: WFReq):
    from mmm.backtest.walkforward import walk_forward
    from mmm.web.data import aux

    b = bundle(days=60)
    folds, oos, notes, _res = walk_forward(b.minute_bars, _cfg(req), req.train_days, req.test_days, holdout_days=req.holdout_days, aux=aux(b))
    with _lock:
        _last["wf"] = folds
    return clean({"notes": notes, "folds": [{"test": f"{f.test_start}..{f.test_end}", "n": f.stats.n, "win_rate": f.stats.win_rate,
                                             "lb95": f.stats.win_rate_ci_low, "net": f.stats.net_pnl, "expectancy": f.stats.expectancy,
                                             "verdict": f.stats.verdict} for f in folds],
                  "pooled": (oos.as_dict() | {"reasons": oos.reasons}) if oos else None})


class CalReq(BaseModel):
    source: str = "walkforward"       # walkforward | backtest
    save: bool = False


@router.post("/backtest/calibration")
def calibration_route(req: CalReq):
    from mmm.backtest import calibration

    with _lock:
        wf, bt = _last.get("wf"), _last.get("bt")
    if req.source == "walkforward":
        if not wf:
            raise HTTPException(409, "run the walk-forward first")
        trades = pd.concat([f.trades for f in wf], ignore_index=True)
    else:
        if bt is None:
            raise HTTPException(409, "run a backtest first")
        trades = bt.trades
    if trades.empty:
        raise HTTPException(409, "no trades to calibrate on")
    trades = trades.assign(win=trades["pnl_net"] > 0)
    cal = calibration.fit(trades, score_col="pa_score", min_n=get_config().validation.min_trades_per_strategy)
    saved = False
    if req.save:
        if bundle().is_demo:
            raise HTTPException(400, "saving a calibrator is disabled on synthetic data")
        if req.source != "walkforward":
            raise HTTPException(400, "only out-of-sample (walk-forward) trades may be saved as the live calibrator")
        calibration.save(cal)
        saved = True
    return clean({"method": cal.method, "n": cal.n, "usable": cal.usable, "min_n": cal.min_n, "brier": cal.brier, "base_rate": cal.base_rate,
                  "reliability": cal.reliability, "saved": saved, "in_sample": req.source != "walkforward"})


class OLReq(BaseModel):
    itm_strikes: int = 0


@router.post("/backtest/optionleg")
def option_leg(req: OLReq):
    from mmm.backtest.option_leg import evaluate_option_leg

    with _lock:
        bt = _last.get("bt")
    if bt is None or bt.trades.empty:
        raise HTTPException(409, "run a backtest with trades first")
    ol = evaluate_option_leg(bt.trades, itm_strikes=req.itm_strikes)
    out: dict = {"evaluated": ol.evaluated, "not_evaluated": ol.not_evaluated, "reasons": ol.reasons}
    if ol.evaluated:
        s = compute_stats(ol.trades)
        out["stats"] = s.as_dict()
        out["trades"] = records(ol.trades)
    return clean(out)


def _latest(prefix: str) -> dict | None:
    from mmm.data import paths

    d = paths.data_dir() / "reports"
    files = sorted(d.glob(f"{prefix}_*.json")) if d.exists() else []
    files = [f for f in files if not f.name.endswith("_events.json")]
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8")) | {"file": str(files[-1])}
    except Exception:  # noqa: BLE001
        return None


@router.get("/research")
def research():
    from mmm.research import features, registry

    return clean({"strategy_status": registry.statuses(), "feature_status": features.statuses(),
                  "research_report": _latest("research"), "feature_report": _latest("features"),
                  "commands": ["mmm research run   (strategies + feature study)", "mmm research features   (feature study only)"]})
