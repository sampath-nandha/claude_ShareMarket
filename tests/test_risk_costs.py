from datetime import timedelta

from mmm.data.quality import DQ, candle_quality, combine
from mmm.demo import demo_minute_bars
from mmm.risk.costs import breakeven_move, round_trip_costs
from mmm.risk.limits import RiskState, check, position_size
from mmm.timeutil import now_ist


def test_costs_positive_and_stt_on_sell_only():
    c = round_trip_costs(100.0, 120.0, 75)
    assert c.total > 0
    assert abs(c.stt - 0.1 / 100 * 120 * 75) < 1e-9
    assert c.brokerage <= 40.0
    assert breakeven_move(100.0, 75) > 0


def test_position_size_respects_budget():
    lots, rupees = position_size(premium_risk_per_unit=30.0)  # 30 pts * 75 = 2250/lot, budget 5000
    assert lots == 2 and rupees == 4500.0
    assert position_size(0)[0] == 0


def test_risk_check_blocks():
    now = now_ist()
    ok = check(RiskState(), 1, 2.0, 30.0, 0.5, now, now, DQ.VALID)
    assert ok.ok, ok.reasons
    bad = check(RiskState(daily_pnl=-20000, consecutive_losses=3), 1, 1.0, 30.0, 3.0, now - timedelta(minutes=30), now, DQ.STALE)
    assert not bad.ok
    joined = " | ".join(bad.reasons)
    for key in ("daily loss", "consecutive", "reward/risk", "spread", "signal age", "data quality"):
        assert key in joined


def test_dq_combine_and_candle_quality():
    assert combine(DQ.VALID, DQ.STALE, DQ.PARTIAL) == DQ.STALE
    assert DQ.PARTIAL.at_least("PARTIAL") and not DQ.STALE.at_least("PARTIAL")
    bars = demo_minute_bars(days=2)
    status, problems = candle_quality(bars, 1)
    assert status == DQ.VALID and problems == []
    bad = bars.copy()
    bad.loc[5, "high"] = bad.loc[5, "low"] - 1
    assert candle_quality(bad, 1)[0] == DQ.INVALID
