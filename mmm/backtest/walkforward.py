"""Chronological walk-forward evaluation with an untouched final holdout and a purge gap between folds."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from mmm.backtest.engine import BacktestConfig, run_backtest
from mmm.backtest.metrics import Stats, compute_stats


@dataclass
class Fold:
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    stats: Stats
    trades: pd.DataFrame


def walk_forward(minute_bars: pd.DataFrame, cfg: BacktestConfig, train_days: int = 120, test_days: int = 30,
                 purge_days: int = 2, holdout_days: int = 60) -> tuple[list[Fold], Stats | None, list[str]]:
    """Rolling folds over all sessions except the last `holdout_days`, which stay untouched.

    Rule-based strategies have no fitted parameters, so 'train' here only serves to report in-sample vs
    out-of-sample consistency; parameter sweeps (if ever added) must only use the train window.
    """
    sessions = sorted(minute_bars["ts"].dt.date.unique())
    notes: list[str] = []
    if len(sessions) < train_days + test_days + holdout_days:
        notes.append(f"only {len(sessions)} sessions: not enough for train {train_days} + test {test_days} + holdout {holdout_days}")
        return [], None, notes
    work = sessions[: len(sessions) - holdout_days]
    holdout = sessions[len(sessions) - holdout_days:]
    folds: list[Fold] = []
    i = 0
    while i + train_days + purge_days + test_days <= len(work):
        tr = work[i : i + train_days]
        te = work[i + train_days + purge_days : i + train_days + purge_days + test_days]
        c = BacktestConfig(**{**cfg.__dict__, "start": te[0], "end": te[-1]})
        # extend bars backwards so warm-up history exists
        sub = minute_bars[(minute_bars["ts"].dt.date >= te[0] - timedelta(days=30)) & (minute_bars["ts"].dt.date <= te[-1])]
        res = run_backtest(sub, c)
        folds.append(Fold(tr[0], tr[-1], te[0], te[-1], compute_stats(res.trades), res.trades))
        i += test_days
    oos = pd.concat([f.trades for f in folds], ignore_index=True) if folds else pd.DataFrame()
    oos_stats = compute_stats(oos) if not oos.empty else None
    notes.append(f"holdout {holdout[0]}..{holdout[-1]} untouched ({len(holdout)} sessions); run `evaluate_holdout` once, at the end")
    return folds, oos_stats, notes


def evaluate_holdout(minute_bars: pd.DataFrame, cfg: BacktestConfig, holdout_days: int = 60) -> Stats:
    sessions = sorted(minute_bars["ts"].dt.date.unique())
    hold = sessions[len(sessions) - holdout_days:]
    c = BacktestConfig(**{**cfg.__dict__, "start": hold[0], "end": hold[-1]})
    sub = minute_bars[minute_bars["ts"].dt.date >= hold[0] - timedelta(days=30)]
    return compute_stats(run_backtest(sub, c).trades)
