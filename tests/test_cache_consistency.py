"""The FrameCache fast path must produce exactly the same context as the from-scratch path."""

from datetime import timedelta

import pandas as pd

from mmm.backtest.engine import BacktestConfig, run_backtest
from mmm.backtest.walkforward import walk_forward
from mmm.demo import demo_minute_bars
from mmm.settings import get_config
from mmm.signals.engine import FrameCache, build_context


def test_cached_context_matches_uncached():
    bars = demo_minute_bars(days=14, seed=17)
    cfg = get_config().strategies
    cache = FrameCache(bars, cfg.setup_timeframe, cfg.context_timeframe, cfg.trend_timeframe)
    sessions = sorted(bars["ts"].dt.date.unique())
    for day in sessions[-3:]:
        day_bars = bars[bars["ts"].dt.date == day]
        for k in (7, 23, 41):  # mid-bar and exact-boundary decision points
            as_of = (day_bars["ts"].iloc[0] + timedelta(minutes=5 * k)).to_pydatetime()
            a = build_context(bars, as_of)
            b = build_context(None, as_of, cache=cache)
            assert (a is None) == (b is None)
            if a is None:
                continue
            for name in ("setup", "context", "trend"):
                pd.testing.assert_frame_equal(getattr(a, name).reset_index(drop=True), getattr(b, name).reset_index(drop=True))
            pd.testing.assert_frame_equal(a.daily.reset_index(drop=True), b.daily.reset_index(drop=True))
            pd.testing.assert_frame_equal(a.intraday_today.reset_index(drop=True), b.intraday_today.reset_index(drop=True))
            assert a.regime.code == b.regime.code and abs(a.atr - b.atr) < 1e-9
            assert sorted(round(lv.price, 4) for lv in a.levels) == sorted(round(lv.price, 4) for lv in b.levels)


def test_research_mode_one_trade_per_strategy_and_walkforward_slices():
    bars = demo_minute_bars(days=40, seed=23)
    res = run_backtest(bars, BacktestConfig(max_hold_bars=60), research_mode=True, log_every=0)
    t = res.trades
    if not t.empty:
        # a strategy never has two overlapping trades
        for sid, g in t.groupby("strategy_id"):
            g = g.sort_values("entry_ts")
            assert (g["entry_ts"].iloc[1:].to_numpy() >= g["exit_ts"].iloc[:-1].to_numpy()).all(), sid
    folds, oos, notes, full = walk_forward(bars, BacktestConfig(max_hold_bars=60), train_days=8, test_days=4, purge_days=1, holdout_days=4, research_mode=True)
    assert full is not None and folds
    n_in_folds = sum(len(f.trades) for f in folds)
    assert n_in_folds <= len(full.trades)
    assert any("holdout" in n for n in notes)
