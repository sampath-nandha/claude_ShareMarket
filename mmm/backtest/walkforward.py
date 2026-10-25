"""Chronological walk-forward evaluation with an untouched final holdout.

The strategies are rule-based with no fitted parameters, so a 'train' window fits nothing; it exists to make
in-sample vs out-of-sample reporting explicit and to leave room for parameter sweeps later (which must only
touch the train window). Because nothing is fitted, one backtest pass over the work range is sliced into folds
instead of re-running the engine per fold - identical trades, ~25x faster.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from mmm.backtest.engine import WARMUP_SESSIONS, BacktestConfig, BacktestResult, run_backtest
from mmm.backtest.metrics import Stats, compute_stats


@dataclass
class Fold:
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    stats: Stats
    trades: pd.DataFrame


def split_sessions(sessions: list[date], train_days: int, test_days: int, purge_days: int, holdout_days: int) -> tuple[list[tuple[list[date], list[date]]], list[date]]:
    work = sessions[: len(sessions) - holdout_days] if holdout_days else sessions
    holdout = sessions[len(sessions) - holdout_days:] if holdout_days else []
    folds = []
    i = 0
    while i + train_days + purge_days + test_days <= len(work):
        tr = work[i : i + train_days]
        te = work[i + train_days + purge_days : i + train_days + purge_days + test_days]
        folds.append((tr, te))
        i += test_days
    return folds, holdout


def walk_forward(minute_bars: pd.DataFrame, cfg: BacktestConfig, train_days: int = 120, test_days: int = 30, purge_days: int = 2,
                 holdout_days: int = 60, research_mode: bool = False, progress=None, workers: int = 1) -> tuple[list[Fold], Stats | None, list[str], BacktestResult | None]:
    """Returns (folds, pooled OOS stats, notes, full backtest result over the work range)."""
    sessions = sorted(minute_bars["ts"].dt.date.unique())
    notes: list[str] = []
    usable = sessions[WARMUP_SESSIONS:]
    if len(usable) < train_days + purge_days + test_days + holdout_days:
        notes.append(f"only {len(usable)} usable sessions: not enough for train {train_days} + purge {purge_days} + test {test_days} + holdout {holdout_days}")
        return [], None, notes, None
    fold_windows, holdout = split_sessions(usable, train_days, test_days, purge_days, holdout_days)
    work_end = fold_windows[-1][1][-1]
    c = BacktestConfig(**{**cfg.__dict__, "start": usable[0], "end": work_end})
    res = run_backtest(minute_bars, c, research_mode=research_mode, progress=progress, workers=workers)
    t = res.trades
    folds: list[Fold] = []
    for tr, te in fold_windows:
        sub = t[(t["session"] >= te[0]) & (t["session"] <= te[-1])] if not t.empty else t
        folds.append(Fold(tr[0], tr[-1], te[0], te[-1], compute_stats(sub), sub))
    oos = pd.concat([f.trades for f in folds], ignore_index=True) if folds else pd.DataFrame()
    oos_stats = compute_stats(oos) if not oos.empty else None
    if holdout:
        notes.append(f"holdout {holdout[0]}..{holdout[-1]} untouched ({len(holdout)} sessions); run `evaluate_holdout` once, at the end")
    notes.append(f"{len(folds)} folds of {test_days} test sessions; {res.candidates} candidates, {res.accepted} trades over the work range")
    return folds, oos_stats, notes, res


def evaluate_holdout(minute_bars: pd.DataFrame, cfg: BacktestConfig, holdout_days: int = 60, research_mode: bool = False) -> Stats:
    sessions = sorted(minute_bars["ts"].dt.date.unique())
    hold = sessions[len(sessions) - holdout_days:]
    c = BacktestConfig(**{**cfg.__dict__, "start": hold[0], "end": hold[-1]})
    return compute_stats(run_backtest(minute_bars, c, research_mode=research_mode).trades)
