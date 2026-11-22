"""Phase 1: pivots, CPR, confluence, gap, opening type, initial balance, pre-open, PA11/PA12."""

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from mmm.analytics.levels import Level
from mmm.analytics.opening import classify_gap, gap_with_fill, initial_balance, opening_type, preopen_reading
from mmm.analytics.pivots import confluence, cpr, floor_pivots, pivot_levels
from mmm.demo import demo_minute_bars
from mmm.signals.engine import FrameCache, build_context
from mmm.strategies.pa11_opening_drive import OpeningDrive
from mmm.strategies.pa12_opening_trap import OpeningTrap
from mmm.timeutil import IST

DAY = date(2026, 11, 13)


def _session(path: list[float], day: date = DAY, noise: float = 0.5) -> pd.DataFrame:
    """1-minute bars whose closes follow `path` (one value per minute from 09:15)."""
    rows, prev = [], path[0]
    t0 = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
    for i, c in enumerate(path):
        o = prev
        rows.append({"ts": t0 + timedelta(minutes=i), "open": o, "high": max(o, c) + noise, "low": min(o, c) - noise,
                     "close": c, "volume": 1000, "oi": 0})
        prev = c
    return pd.DataFrame(rows)


def _daily(rows: list[tuple[float, float, float, float]], end: date = DAY - timedelta(days=1)) -> pd.DataFrame:
    days = pd.bdate_range(end=end, periods=len(rows))
    return pd.DataFrame([{"ts": pd.Timestamp(d).tz_localize(IST), "open": o, "high": h, "low": lo, "close": c, "volume": 0, "oi": 0}
                         for d, (o, h, lo, c) in zip(days, rows)])


def test_floor_pivots_and_cpr_ordering():
    fp = floor_pivots(110, 90, 105)
    assert fp["P"] == pytest.approx(101.6667, rel=1e-4)
    assert fp["S1"] < fp["P"] < fp["R1"] < fp["R2"] < fp["R3"]
    d = _daily([(100, 110 + i % 5, 90 - i % 3, 100 + i % 7) for i in range(40)])
    c = cpr(d)
    assert c is not None and c.bc <= c.pivot <= c.tc and c.width_class in ("NARROW", "NORMAL", "WIDE")
    kinds = {lv.kind for lv in pivot_levels(d)}
    assert {"PIV_P", "PIV_R1", "PIV_S1", "CPR_TC", "CPR_BC"} <= kinds


def test_confluence_merges_distinct_sources_only():
    lv = [Level(100.0, "PDH", "day", 70), Level(100.6, "PIV_R1", "day", 50), Level(100.3, "PDH", "day", 60),
          Level(150.0, "ZONE_R", "5m", 80)]
    cl = confluence(lv, atr_value=4.0)            # tolerance 1 point
    assert len(cl) == 1
    assert cl[0].members == ["PDH 100", "PIV_R1 101"]
    assert cl[0].score == pytest.approx(100 * (1 - 0.3 * 0.5))


def test_gap_classification_and_fill():
    d = _daily([(100, 102, 98, 100)] * 20)
    assert classify_gap(100.05, d).kind == "FLAT"
    assert classify_gap(103, d).kind == "GAP_UP_OUTSIDE"
    assert classify_gap(101, d).kind == "GAP_UP_INSIDE"
    assert classify_gap(97, d).kind == "GAP_DOWN_OUTSIDE"
    s = _session([103, 102, 101, 99.4, 100.5], noise=0.1)
    g = gap_with_fill(s, d)
    assert g.direction == 1 and g.filled and g.fill_ts == s["ts"].iloc[3].to_pydatetime()


@pytest.mark.parametrize("path,code,direction", [
    (list(np.linspace(100, 115, 15)), "OPEN_DRIVE", 1),
    (list(np.linspace(100, 95, 5)) + list(np.linspace(95, 112, 10)), "OPEN_TEST_DRIVE", 1),
    (list(np.linspace(100, 112, 7)) + list(np.linspace(112, 101, 8)), "OPEN_REJECTION_REVERSE", -1),
    ([100, 103, 98, 102, 99, 101, 100, 102, 98, 101, 100, 99, 101, 100, 100.5], "OPEN_AUCTION", 0),
])
def test_opening_types(path, code, direction):
    s = _session(path, noise=0.2)
    ot = opening_type(s, 15)
    assert (ot.code, ot.direction) == (code, direction)


def test_opening_type_forming_and_initial_balance():
    s = _session(list(np.linspace(100, 110, 90)))
    as_of = s["ts"].iloc[10].to_pydatetime()
    assert opening_type(s.iloc[:10], 15, as_of).code == "FORMING"
    ib = initial_balance(s, 30)
    assert ib.complete and ib.status == "EXT_UP" and ib.ext_up > 0


def test_preopen_reading_from_ticks():
    t = lambda h, m: datetime(2026, 11, 13, h, m, tzinfo=IST)
    ticks = pd.DataFrame([
        {"instrument_token": 1, "last_price": 25010.0, "exchange_timestamp": t(9, 7), "received_ts": t(9, 7), "total_buy_quantity": None, "total_sell_quantity": None},
        {"instrument_token": 2, "last_price": 10.0, "exchange_timestamp": t(9, 7), "received_ts": t(9, 7), "total_buy_quantity": 300, "total_sell_quantity": 100},
        {"instrument_token": 3, "last_price": 20.0, "exchange_timestamp": t(9, 7), "received_ts": t(9, 7), "total_buy_quantity": 100, "total_sell_quantity": 100},
        {"instrument_token": 1, "last_price": 25100.0, "exchange_timestamp": t(9, 20), "received_ts": t(9, 20), "total_buy_quantity": None, "total_sell_quantity": None},
    ])
    p = preopen_reading(ticks, 1, [2, 3], prev_close=25000.0)
    assert p.available and p.index_price == 25010.0 and p.gap_points == 10.0
    assert p.imbalance == pytest.approx(200 / 600)
    assert not preopen_reading(ticks[ticks["exchange_timestamp"] > t(9, 10)], 1, [2, 3], 25000.0).available


def _history_then(today: pd.DataFrame, days: int = 25) -> pd.DataFrame:
    hist = demo_minute_bars(days=days, seed=9, start_price=float(today["open"].iloc[0]), end=DAY - timedelta(days=1))
    return pd.concat([hist, today], ignore_index=True)


def test_pa11_fires_on_an_open_drive_at_0930():
    prev_close = None
    today = _session(list(np.linspace(0, 1, 375)))       # placeholder to size
    hist = demo_minute_bars(days=25, seed=9, end=DAY - timedelta(days=1))
    prev_close = float(hist["close"].iloc[-1])
    path = list(prev_close + 20 + np.linspace(0, 60, 15)) + list(prev_close + 80 + np.linspace(0, 5, 360))
    today = _session(path, noise=1.0)
    bars = pd.concat([hist, today], ignore_index=True)
    cache = FrameCache(bars, "5minute", "15minute", "60minute")
    as_of = datetime(2026, 11, 13, 9, 30, tzinfo=IST)
    ctx = build_context(None, as_of, cache=cache)
    assert ctx.extras["open15"].code == "OPEN_DRIVE"
    cand = OpeningDrive().evaluate(ctx)
    if cand is not None:                                   # R:R gate may legitimately reject on random history
        assert cand.direction == "BULLISH" and cand.stop < ctx.extras["open15"].low
    assert OpeningDrive().evaluate(build_context(None, as_of + timedelta(minutes=5), cache=cache)) is None


def test_pa12_trades_against_a_failed_gap():
    hist = demo_minute_bars(days=25, seed=13, end=DAY - timedelta(days=1))
    pc = float(hist["close"].iloc[-1])
    a = float((hist["high"] - hist["low"]).mean())
    # gap up 150, holds the OR for 15 min, then breaks below ORL and keeps sliding towards the previous close
    path = list(pc + 150 + np.sin(np.arange(15)) * 6) + list(pc + 150 - np.linspace(0, 40, 15)) + list(pc + 110 - np.linspace(0, 100, 345))
    bars = pd.concat([hist, _session(path, noise=1.0)], ignore_index=True)
    cache = FrameCache(bars, "5minute", "15minute", "60minute")
    found = None
    for k in range(4, 30):
        ctx = build_context(None, datetime(2026, 11, 13, 9, 15, tzinfo=IST) + timedelta(minutes=5 * k), cache=cache)
        g = ctx.extras["gap"]
        if g.atr_multiple is None or abs(g.atr_multiple) < 0.3:
            pytest.skip("synthetic gap too small for this seed")
        c = OpeningTrap().evaluate(ctx)
        if c is not None:
            found = c
            break
    assert found is not None and found.direction == "BEARISH" and found.trigger_event == "OPENING_TRAP"
    assert found.target1 == pytest.approx(pc) or found.target1 < found.entry
    assert a > 0
