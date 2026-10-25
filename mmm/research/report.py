"""`mmm research run`: walk-forward every enabled strategy on archived NIFTY minute bars, fit calibration on the
pooled out-of-sample trades, update the status registry, and write a Markdown + JSON report. Holdout untouched."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

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


def load_index_minutes(days: int | None = None) -> pd.DataFrame:
    cfg = get_config().universe
    inst = load_instruments()
    idx = inst[(inst["exchange"] == cfg.index_exchange) & (inst["tradingsymbol"] == cfg.index_symbol)] if not inst.empty else inst
    if idx.empty:
        return pd.DataFrame()
    start = date.today() - timedelta(days=days) if days else None
    return parquet_store.read_candles("minute", int(idx.iloc[0]["instrument_token"]), start=start)


def run(bars: pd.DataFrame | None = None, train_days: int = 120, test_days: int = 30, holdout_days: int = 60, state: StateDB | None = None,
        out_dir: Path | None = None) -> dict[str, Any]:
    state = state or StateDB()
    bars = load_index_minutes() if bars is None else bars
    if bars.empty:
        raise RuntimeError("no archived NIFTY minute candles; run `mmm data backfill` first")
    strategies = load_strategies(get_config().strategies.enabled, apply_registry=False)
    report: dict[str, Any] = {"generated_at": now_ist().isoformat(), "sessions": int(bars["ts"].dt.date.nunique()),
                              "range": [str(bars["ts"].iloc[0].date()), str(bars["ts"].iloc[-1].date())], "strategies": {}, "notes": []}
    pooled = []
    for s in strategies:
        folds, oos, notes = walk_forward(bars, BacktestConfig(strategies=[s.id]), train_days, test_days, holdout_days=holdout_days)
        entry: dict[str, Any] = {"folds": len(folds), "notes": notes}
        if oos is not None:
            entry["oos"] = oos.as_dict()
            trades = pd.concat([f.trades for f in folds], ignore_index=True)
            trades["win"] = trades["pnl_net"] > 0
            pooled.append(trades)
            entry["by_regime"] = breakdown(trades.assign(regime_dir=trades["regime"].str.split("|").str[0]), "regime_dir").to_dict("records")
            entry["cost_sensitivity"] = cost_sensitivity(trades).to_dict("records")
            ol = evaluate_option_leg(trades)
            entry["option_leg"] = {"evaluated": ol.evaluated, "not_evaluated": ol.not_evaluated, "reasons": ol.reasons,
                                   "stats": compute_stats(ol.trades).as_dict() if ol.evaluated else None}
            entry["status"] = registry.record_backtest(s.id, oos, state=state)
        else:
            entry["status"] = registry.statuses(state).get(s.id, {}).get("status", "EXPERIMENTAL")
            entry["oos"] = None
        report["strategies"][s.id] = entry
    if pooled:
        allt = pd.concat(pooled, ignore_index=True)
        cal = calibration.fit(allt, score_col="pa_score" if "pa_score" in allt else "score", min_n=get_config().validation.min_trades_per_strategy)
        calibration.save(cal, state)
        report["calibration"] = {"method": cal.method, "n": cal.n, "usable": cal.usable, "brier": cal.brier, "base_rate": cal.base_rate, "reliability": cal.reliability}
        report["pooled_oos"] = compute_stats(allt).as_dict()
    else:
        report["notes"].append("no out-of-sample trades produced; need more history or looser parameters")
    out_dir = out_dir or (paths.data_dir() / "reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_ist().strftime("%Y%m%d_%H%M")
    (out_dir / f"research_{stamp}.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (out_dir / f"research_{stamp}.md").write_text(_markdown(report), encoding="utf-8")
    report["paths"] = [str(out_dir / f"research_{stamp}.json"), str(out_dir / f"research_{stamp}.md")]
    return report


def _fmt(v: Any, pct: bool = False) -> str:
    if v is None:
        return "–"
    if isinstance(v, float):
        return f"{v:.1%}" if pct else f"{v:,.2f}"
    return str(v)


def _markdown(r: dict[str, Any]) -> str:
    lines = [f"# Research report {r['generated_at']}", "", f"Sessions: {r['sessions']} ({r['range'][0]} → {r['range'][1]}). Holdout untouched.", "",
             "| Strategy | OOS n | win rate | 95% LB | expectancy | PF | max DD | verdict | status |", "|---|---|---|---|---|---|---|---|---|"]
    for sid, e in r["strategies"].items():
        o = e.get("oos") or {}
        lines.append(f"| {sid} | {o.get('n', 0)} | {_fmt(o.get('win_rate'), True)} | {_fmt(o.get('win_rate_ci_low'), True)} | {_fmt(o.get('expectancy'))} | "
                     f"{_fmt(o.get('profit_factor'))} | {_fmt(o.get('max_drawdown'))} | {o.get('verdict', 'n/a')} | {e.get('status')} |")
    if "pooled_oos" in r:
        p = r["pooled_oos"]
        lines += ["", f"Pooled OOS: n={p['n']} win rate {_fmt(p['win_rate'], True)} (LB {_fmt(p['win_rate_ci_low'], True)}), expectancy {_fmt(p['expectancy'])}, verdict {p['verdict']}."]
    if "calibration" in r:
        c = r["calibration"]
        lines += ["", f"Calibration: {c['method']} on n={c['n']} ({'usable' if c['usable'] else 'NOT usable'}), Brier {_fmt(c['brier'])}, base rate {_fmt(c['base_rate'], True)}."]
        if c["reliability"]:
            lines += ["", "| score bin | n | predicted | observed | CI |", "|---|---|---|---|---|"]
            for b in c["reliability"]:
                lines.append(f"| {b['lo']:.0f}-{b['hi']:.0f} | {b['n']} | {b['predicted']:.1%} | {b['observed']:.1%} | {b['ci_lo']:.0%}–{b['ci_hi']:.0%} |")
    for sid, e in r["strategies"].items():
        ol = e.get("option_leg")
        if ol:
            lines.append(f"\n{sid} option leg: evaluated {ol['evaluated']}, not evaluated {ol['not_evaluated']} {ol['reasons'] or ''}")
    lines += ["", "Notes: " + "; ".join(r.get("notes", [])) if r.get("notes") else ""]
    return "\n".join(lines)
