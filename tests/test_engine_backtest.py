from datetime import timedelta


from mmm.backtest.engine import BacktestConfig, run_backtest
from mmm.backtest.metrics import compute_stats
from mmm.demo import demo_minute_bars
from mmm.signals.engine import Confirmations, build_context, stage1, stage2
from mmm.strategies import load_strategies


def test_build_context_has_no_lookahead():
    bars = demo_minute_bars(days=12)
    as_of = bars["ts"].iloc[-200]
    ctx = build_context(bars, as_of)
    assert ctx is not None
    assert (ctx.setup["ts"] + timedelta(minutes=5) <= as_of).all()
    assert (ctx.context["ts"] + timedelta(minutes=15) <= as_of).all()
    assert (ctx.intraday_today["ts"] < as_of).all()
    assert ctx.atr > 0 and ctx.levels


def test_strategies_return_valid_candidates_and_stage2_labels_research_only():
    bars = demo_minute_bars(days=25, seed=11)
    strategies = load_strategies()
    assert [s.id for s in strategies] == ["PA01", "PA02", "PA03", "PA04", "PA05"]
    found = 0
    sessions = sorted(bars["ts"].dt.date.unique())[12:]
    for day in sessions:
        day_bars = bars[bars["ts"].dt.date == day]
        for k in range(6, 70, 2):
            as_of = day_bars["ts"].iloc[0] + timedelta(minutes=5 * k)
            ctx = build_context(bars, as_of)
            if ctx is None:
                continue
            for strat, cand in stage1(ctx, strategies):
                found += 1
                d = 1 if cand.direction == "BULLISH" else -1
                assert d * (cand.entry - cand.stop) > 0, "stop must be on the losing side"
                assert d * (cand.target1 - cand.entry) > 0 and d * (cand.target2 - cand.target1) > 0
                assert cand.reward_risk >= 1.5
                sig = stage2(ctx, strat, cand, Confirmations())
                assert any("research-only" in r for r in sig.do_not_execute_reasons)
                assert sig.win_probability is None
                assert 0 <= sig.score <= 100
        if found >= 5:
            break
    assert found > 0, "synthetic data should produce at least one candidate across many sessions"


def test_backtest_runs_and_is_conservative():
    bars = demo_minute_bars(days=22, seed=5)
    res = run_backtest(bars, BacktestConfig(max_hold_bars=60))
    assert res.sessions == 22
    assert "stop assumed first" in " ".join(res.notes)
    if not res.trades.empty:
        t = res.trades
        assert (t["entry_ts"] > t["signal_ts"]).all() or (t["entry_ts"] >= t["signal_ts"]).all()
        assert (t["exit_ts"] >= t["entry_ts"]).all()
        assert (t["costs"] > 0).all()
        assert set(t["exit_reason"]) <= {"STOP", "TARGET1", "TIME", "SESSION_END"}
        stats = compute_stats(t)
        assert stats.verdict in {"INSUFFICIENT_EVIDENCE", "REJECTED", "ACCEPTED"}
