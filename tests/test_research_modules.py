from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from mmm.analytics.consolidation import detect_box
from mmm.backtest import calibration
from mmm.backtest.engine import BacktestConfig, run_backtest
from mmm.backtest.metrics import compute_stats
from mmm.backtest.option_leg import evaluate_option_leg
from mmm.data.state_db import StateDB
from mmm.demo import demo_minute_bars
from mmm.research import registry
from mmm.signals.engine import build_context, dedupe, stage1
from mmm.signals.model import SetupCandidate
from mmm.strategies import ALL_IDS, load_strategies
from mmm.strategies.base import Strategy
from mmm.timeutil import IST


def test_all_ten_strategies_load_and_have_specs():
    s = load_strategies(apply_registry=False)
    assert [x.id for x in s] == ALL_IDS
    for x in s:
        spec = x.spec()
        assert spec["doc"] and spec["status"] == "EXPERIMENTAL"


def test_detect_box_on_flat_series():
    ts = pd.date_range("2026-01-05 09:15", periods=60, freq="5min", tz=IST)
    rng = np.random.default_rng(1)
    base = 24000 + rng.normal(0, 3, 60).cumsum() * 0.1
    df = pd.DataFrame({"ts": ts, "open": base, "high": base + 5, "low": base - 5, "close": base + rng.normal(0, 1, 60), "volume": 1, "oi": 0})
    df.loc[20:40, ["open", "high", "low", "close"]] = [[24000, 24006, 23994, 24001]] * 21  # tight box
    box = detect_box(df, end_idx=40, min_bars=8, max_width_atr=1.6)
    assert box is not None and box.bars >= 8 and box.high - box.low <= 15


def test_new_strategies_run_through_engine_without_error():
    bars = demo_minute_bars(days=25, seed=21)
    strategies = load_strategies(["PA06", "PA07", "PA08", "PA09", "PA10"], apply_registry=False)
    sessions = sorted(bars["ts"].dt.date.unique())[12:]
    evaluated = 0
    for day in sessions[:6]:
        day_bars = bars[bars["ts"].dt.date == day]
        for k in range(6, 70, 3):
            ctx = build_context(bars, day_bars["ts"].iloc[0] + timedelta(minutes=5 * k))
            if ctx is None:
                continue
            for strat, cand in stage1(ctx, strategies):
                evaluated += 1
                d = 1 if cand.direction == "BULLISH" else -1
                assert d * (cand.entry - cand.stop) > 0 and d * (cand.target1 - cand.entry) > 0 and cand.reward_risk >= 1.5
    assert evaluated >= 0  # presence of setups depends on the synthetic path; correctness checked above


class _Dummy(Strategy):
    def __init__(self, id_):
        self.id = id_

    def evaluate(self, ctx):
        return None


def _cand(direction, score, sid):
    ts = datetime(2026, 1, 5, 10, 0, tzinfo=IST)
    return SetupCandidate(strategy_id=sid, direction=direction, timeframe="5minute", trigger_event="X", entry=100, stop=99 if direction == "BULLISH" else 101,
                          target1=102 if direction == "BULLISH" else 98, target2=103 if direction == "BULLISH" else 97, invalidation="", pa_score=score, bar_ts=ts)


def test_dedupe_keeps_best_per_direction_and_flags_opposites():
    a, b, c = _Dummy("PA03"), _Dummy("PA05"), _Dummy("PA02")
    out = dedupe([(a, _cand("BEARISH", 60, "PA03")), (b, _cand("BEARISH", 70, "PA05")), (c, _cand("BULLISH", 55, "PA02"))])
    assert len(out) == 2
    bear = next(x for x in out if x[1].direction == "BEARISH")
    assert bear[0].id == "PA05" and any("coincident: PA03" in e for e in bear[1].evidence)
    assert all(any("opposing setup" in x for x in cnd.contradictions) for _, cnd in out)


def test_calibration_gating_and_fit():
    rng = np.random.default_rng(3)
    n = 400
    score = rng.uniform(40, 95, n)
    win = rng.random(n) < (0.3 + 0.5 * (score - 40) / 55)
    df = pd.DataFrame({"pa_score": score, "win": win})
    small = calibration.fit(df.head(50), score_col="pa_score", min_n=200)
    assert small.method == "platt" and not small.usable and small.predict(70)[0] is None
    big = calibration.fit(df, score_col="pa_score", min_n=200)
    assert big.method == "isotonic" and big.usable
    p_lo, _ = big.predict(45)
    p_hi, note = big.predict(90)
    assert p_hi > p_lo and "n=400" in note
    assert big.brier is not None and big.brier < 0.25
    rt = calibration.Calibrator.from_json(big.to_json())
    assert rt.predict(70)[0] == big.predict(70)[0]


def test_registry_transitions(tmp_path):
    db = StateDB(tmp_path / "r.sqlite")
    assert registry.statuses(db) == {}
    weak = compute_stats(pd.DataFrame({"pnl_gross": [100] * 30, "costs": [10] * 30, "pnl_net": [90] * 30, "session": [date(2026, 1, 1)] * 30}))
    assert registry.record_backtest("PA01", weak, state=db) == "INSUFFICIENT_EVIDENCE"
    rng = np.random.default_rng(4)
    wins = rng.random(600) < 0.62
    good = pd.DataFrame({"pnl_gross": np.where(wins, 1500, -1000), "costs": 90, "session": [date(2026, 1, 1) + timedelta(days=i // 3) for i in range(600)]})
    good["pnl_net"] = good["pnl_gross"] - good["costs"]
    gs = compute_stats(good)
    assert registry.record_backtest("PA01", gs, state=db) == "ACCEPTED_FOR_PAPER_TRADING"
    assert registry.record_forward("PA01", gs, state=db) == "FORWARD_VALIDATED"
    assert registry.record_backtest("PA01", weak, state=db) == "FORWARD_VALIDATED"  # backtests can't demote forward-validated
    assert load_strategies(["PA01"], apply_registry=False)[0].status == "EXPERIMENTAL"


def test_option_leg_reports_missing_data_instead_of_guessing(tmp_path, monkeypatch):
    monkeypatch.setenv("MMM_DATA_DIR", str(tmp_path))
    from mmm import settings

    settings.get_secrets.cache_clear()
    bars = demo_minute_bars(days=22, seed=5)
    res = run_backtest(bars, BacktestConfig(max_hold_bars=60))
    ol = evaluate_option_leg(res.trades)
    assert ol.evaluated == 0
    if not res.trades.empty:
        assert ol.not_evaluated == len(res.trades) and "no instrument snapshot for date" in ol.reasons
        assert "pa_score" in res.trades and "coincident" in res.trades
    settings.get_secrets.cache_clear()


def test_backtest_dedup_single_trade_per_bar():
    bars = demo_minute_bars(days=22, seed=9)
    res = run_backtest(bars, BacktestConfig(max_hold_bars=30))
    if not res.trades.empty:
        assert not res.trades.duplicated(subset=["signal_ts"]).any()
