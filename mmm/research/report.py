"""`mmm research run`: one research-mode walk-forward pass over archived NIFTY minute bars (all strategies simulated
independently), per-strategy out-of-sample statistics, calibration fit on pooled OOS trades, status registry update,
Markdown + JSON report. The final holdout window is never touched here."""

from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import structlog

from mmm.backtest import calibration
from mmm.backtest.engine import BacktestConfig
from mmm.backtest.metrics import breakdown, compute_stats, cost_sensitivity
from mmm.backtest.option_leg import evaluate_option_leg
from mmm.backtest.walkforward import walk_forward
from mmm.data import parquet_store, paths
from mmm.data.instruments import load_instruments
from mmm.data.state_db import StateDB
from mmm.research import registry
from mmm.settings import get_config
from mmm.strategies import load_strategies
from mmm.timeutil import now_ist

log = structlog.get_logger(__name__)


def load_index_minutes(days: int | None = None) -> pd.DataFrame:
    cfg = get_config().universe
    inst = load_instruments()
    idx = inst[(inst["exchange"] == cfg.index_exchange) & (inst["tradingsymbol"] == cfg.index_symbol)] if not inst.empty else inst
    if idx.empty:
        return pd.DataFrame()
    start = date.today() - timedelta(days=days) if days else None
    return parquet_store.read_candles("minute", int(idx.iloc[0]["instrument_token"]), start=start)


def run(bars: pd.DataFrame | None = None, train_days: int = 120, test_days: int = 30, holdout_days: int = 60, days: int | None = None,
        state: StateDB | None = None, out_dir: Path | None = None, progress=None, workers: int = 1) -> dict[str, Any]:
    state = state or StateDB()
    t0 = time.monotonic()
    bars = load_index_minutes(days) if bars is None else bars
    if bars.empty:
        raise RuntimeError("no archived NIFTY minute candles; run `mmm data backfill` first")
    n_sessions = int(bars["ts"].dt.date.nunique())
    log.info("research_start", sessions=n_sessions, rows=len(bars), first=str(bars["ts"].iloc[0].date()), last=str(bars["ts"].iloc[-1].date()))
    strategies = load_strategies(get_config().strategies.enabled, apply_registry=False)
    report: dict[str, Any] = {"generated_at": now_ist().isoformat(), "sessions": n_sessions,
                              "range": [str(bars["ts"].iloc[0].date()), str(bars["ts"].iloc[-1].date())], "strategies": {}, "notes": []}
    folds, _pooled_oos, notes, res = walk_forward(bars, BacktestConfig(strategies=[s.id for s in strategies]), train_days, test_days,
                                                   holdout_days=holdout_days, research_mode=True, progress=progress, workers=workers)
    report["notes"] += notes
    if res is None or not folds:
        report["notes"].append("walk-forward could not run (not enough sessions). Try --days larger, or smaller --train-days/--test-days/--holdout-days.")
        for s in strategies:
            report["strategies"][s.id] = {"oos": None, "status": registry.statuses(state).get(s.id, {}).get("status", "EXPERIMENTAL")}
        return _finish(report, out_dir, t0)
    oos_all = pd.concat([f.trades for f in folds], ignore_index=True)
    oos_all["win"] = oos_all["pnl_net"] > 0
    report["candidates"], report["trades_total"] = res.candidates, res.accepted
    for s in strategies:
        t = oos_all[oos_all["strategy_id"] == s.id]
        entry: dict[str, Any] = {"folds": len(folds)}
        if t.empty:
            entry["oos"] = compute_stats(t).as_dict()
            entry["status"] = registry.record_backtest(s.id, compute_stats(t), state=state)
            report["strategies"][s.id] = entry
            continue
        stats = compute_stats(t)
        entry["oos"] = stats.as_dict()
        entry["fold_win_rates"] = [compute_stats(f.trades[f.trades["strategy_id"] == s.id]).win_rate for f in folds]
        entry["by_regime"] = breakdown(t.assign(regime_dir=t["regime"].str.split("|").str[0]), "regime_dir").to_dict("records")
        entry["by_hour"] = breakdown(t, "hour").to_dict("records")
        entry["by_exit"] = breakdown(t, "exit_reason").to_dict("records")
        entry["cost_sensitivity"] = cost_sensitivity(t).to_dict("records")
        ol = evaluate_option_leg(t)
        entry["option_leg"] = {"evaluated": ol.evaluated, "not_evaluated": ol.not_evaluated, "reasons": ol.reasons,
                               "stats": compute_stats(ol.trades).as_dict() if ol.evaluated else None}
        entry["status"] = registry.record_backtest(s.id, stats, state=state)
        report["strategies"][s.id] = entry
        log.info("strategy_result", strategy=s.id, n=stats.n, win_rate=stats.win_rate, lb=stats.win_rate_ci_low, verdict=stats.verdict, status=entry["status"])
    cal = calibration.fit(oos_all, score_col="pa_score", min_n=get_config().validation.min_trades_per_strategy)
    calibration.save(cal, state)
    report["calibration"] = {"method": cal.method, "n": cal.n, "usable": cal.usable, "brier": cal.brier, "base_rate": cal.base_rate, "reliability": cal.reliability}
    report["pooled_oos"] = compute_stats(oos_all).as_dict()
    report["pooled_oos_note"] = "pooled across independently simulated strategies; overlapping exposure is NOT netted - use per-strategy rows for decisions"
    return _finish(report, out_dir, t0)


def _finish(report: dict[str, Any], out_dir: Path | None, t0: float) -> dict[str, Any]:
    report["elapsed_s"] = round(time.monotonic() - t0)
    out_dir = out_dir or (paths.data_dir() / "reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_ist().strftime("%Y%m%d_%H%M")
    (out_dir / f"research_{stamp}.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (out_dir / f"research_{stamp}.md").write_text(_markdown(report), encoding="utf-8")
    report["paths"] = [str(out_dir / f"research_{stamp}.json"), str(out_dir / f"research_{stamp}.md")]
    log.info("research_done", elapsed_s=report["elapsed_s"], paths=report["paths"])
    return report


def _fmt(v: Any, pct: bool = False) -> str:
    if v is None:
        return "–"
    if isinstance(v, float):
        return f"{v:.1%}" if pct else f"{v:,.2f}"
    return str(v)


def _markdown(r: dict[str, Any]) -> str:
    lines = [f"# Research report {r['generated_at']}", "", f"Sessions: {r['sessions']} ({r['range'][0]} → {r['range'][1]}). Elapsed {r.get('elapsed_s', '?')}s. Holdout untouched.", "",
             "| Strategy | OOS n | win rate | 95% LB | expectancy ₹ | PF | max DD ₹ | verdict | status |", "|---|---|---|---|---|---|---|---|---|"]
    for sid, e in r["strategies"].items():
        o = e.get("oos") or {}
        lines.append(f"| {sid} | {o.get('n', 0)} | {_fmt(o.get('win_rate'), True)} | {_fmt(o.get('win_rate_ci_low'), True)} | {_fmt(o.get('expectancy'))} | "
                     f"{_fmt(o.get('profit_factor'))} | {_fmt(o.get('max_drawdown'))} | {o.get('verdict', 'n/a')} | {e.get('status')} |")
    if "pooled_oos" in r:
        p = r["pooled_oos"]
        lines += ["", f"Pooled OOS: n={p['n']} win rate {_fmt(p['win_rate'], True)} (LB {_fmt(p['win_rate_ci_low'], True)}), expectancy {_fmt(p['expectancy'])}, verdict {p['verdict']}. "
                  f"({r.get('pooled_oos_note', '')})"]
    if "calibration" in r:
        c = r["calibration"]
        lines += ["", f"Calibration: {c['method']} on n={c['n']} ({'usable' if c['usable'] else 'NOT usable'}), Brier {_fmt(c['brier'])}, base rate {_fmt(c['base_rate'], True)}."]
        if c["reliability"]:
            lines += ["", "| PA score bin | n | predicted | observed | 95% CI |", "|---|---|---|---|---|"]
            for b in c["reliability"]:
                lines.append(f"| {b['lo']:.0f}-{b['hi']:.0f} | {b['n']} | {b['predicted']:.1%} | {b['observed']:.1%} | {b['ci_lo']:.0%}–{b['ci_hi']:.0%} |")
    for sid, e in r["strategies"].items():
        ol = e.get("option_leg")
        if ol:
            lines.append(f"\n{sid} option leg: evaluated {ol['evaluated']}, not evaluated {ol['not_evaluated']} {ol['reasons'] or ''}")
        if e.get("fold_win_rates"):
            lines.append(f"{sid} fold win rates: " + ", ".join("–" if w is None else f"{w:.0%}" for w in e["fold_win_rates"]))
    if r.get("notes"):
        lines += ["", "Notes: " + "; ".join(r["notes"])]
    return "\n".join(lines)
