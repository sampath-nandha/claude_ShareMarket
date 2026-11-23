"""Candlestick pattern trial: detection, pre-declared contexts, Bonferroni gate, rollback switch, parallel equality."""

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from mmm.analytics import patterns as P
from mmm.demo import demo_minute_bars
from mmm.research import features as F
from mmm.timeutil import IST


def _bars(ohlc: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    t0 = datetime(2026, 11, 13, 9, 15, tzinfo=IST)
    return pd.DataFrame([{"ts": t0 + timedelta(minutes=5 * i), "open": o, "high": h, "low": lo, "close": c, "volume": 0, "oi": 0}
                         for i, (o, h, lo, c) in enumerate(ohlc)])


BASE = [(100, 102, 98, 101), (101, 103, 99, 100)] * 10          # 20 ordinary bars, range 4


def _last(extra) -> pd.Series:
    return P.pattern_frame(_bars(BASE + extra)).iloc[-1]


def test_momentum_patterns():
    assert _last([(100, 108.1, 99.9, 108)])["marubozu"] == 1                        # range 8.2 >= 1.5 x avg, body 98%
    assert _last([(100, 102, 99.8, 101.8), (101.8, 104, 101.6, 103.8), (103.8, 106, 103.6, 105.8)])["three_soldiers_crows"] == 1
    assert _last([(100, 104, 96, 101), (101, 102, 99, 100), (100, 105, 99.5, 104.5)])["inside_bar_breakout"] == 1
    r = _last([(100, 104, 96, 97), (97, 97.5, 96.5, 97.2), (97.2, 97.4, 95, 95.2)])
    assert r["inside_bar_breakout"] == -1


def test_reversal_patterns_and_strict_engulfing():
    r = _last([(101, 101.5, 97, 97.5), (97.5, 102, 97.2, 101.8)])
    assert r["engulfing"] == 1
    weak = _last([(101, 101.5, 97, 100.8), (100.8, 102, 97.2, 101.2)])               # tiny bodies: not a real engulfing
    assert weak["engulfing"] == 0
    assert _last([(99, 99.6, 90, 99.4)])["hammer_shooting_star"] == 1                # long lower wick at a 10-bar low


def test_indecision_patterns():
    r = _last([(100, 102, 98, 100.1)])
    assert r["doji"] == 1 and r["spinning_top"] == 0
    assert _last([(100, 102.5, 99.5, 101)])["inside_bar"] == 1                       # inside the previous 99-103 bar


def test_contextual_direction_rules():
    class Struct:
        trend = "BULLISH"

    class Ctx:
        pass

    ctx = Ctx()
    ctx.context_structure = Struct()
    ctx.atr = 2.0
    ctx.setup = _bars(BASE + [(100, 108.1, 99.9, 108)])
    ctx.levels = []
    assert P.contextual_direction("marubozu", 1, ctx) == 1
    assert P.contextual_direction("marubozu", -1, ctx) == 0                         # against the context trend
    assert P.contextual_direction("engulfing", 1, ctx) == 0                         # no level nearby
    from mmm.analytics.levels import Level

    ctx.levels = [Level(99.8, "PDL", "day", 70)]
    assert P.contextual_direction("engulfing", 1, ctx) == 1
    run_up = [(100 + i, 101.5 + i, 99.5 + i, 101 + i) for i in range(8)] + [(108, 109.5, 107.5, 108.05)]
    ctx.setup = _bars(BASE + run_up)
    assert P.contextual_direction("doji", 1, ctx) == -1                              # indecision after a rally -> reversal


def test_pattern_family_gate_is_bonferroni_corrected():
    sessions = [date(2024, 1, 1) + timedelta(days=i) for i in range(400)]
    rng = np.random.default_rng(3)
    hits = list(rng.random(400) < 0.60)
    ev = pd.DataFrame([{"feature": f, "session": d, "ts": None, "direction": 1, "move": 5.0 if h else -5.0, "net": 3.0 if h else -7.0, "hit": h}
                       for f in ("open_drive", "pat_engulfing") for d, h in zip(sessions, hits)])
    res = F.evaluate(ev, sessions, 120, 100)
    od, pe = res["open_drive"], res["pat_engulfing"]
    assert od["oos"]["hit_rate"] == pe["oos"]["hit_rate"]
    assert pe["confidence"] > od["confidence"] and pe["oos"]["lb"] < od["oos"]["lb"]   # stricter bound for the 15-pattern family
    assert pe["oos"]["lb_95"] == pytest.approx(od["oos"]["lb"])


def test_rollback_switch(monkeypatch):
    from mmm.settings import get_config

    assert any(f.group == "pattern" for f in F.active_features())
    monkeypatch.setattr(get_config().patterns, "enabled", False)
    assert not any(f.group == "pattern" for f in F.active_features())
    assert {f.key for f in F.active_features()} == {f.key for f in F.FEATURES}


def test_parallel_collection_matches_serial(tmp_path, monkeypatch):
    monkeypatch.setenv("MMM_DATA_DIR", str(tmp_path))
    from mmm import settings

    settings.get_secrets.cache_clear()
    bars = demo_minute_bars(days=140, seed=8, end=date(2026, 11, 13))
    sess = sorted(bars["ts"].dt.date.unique())[100:]
    key = ["session", "ts", "feature"]
    a = F.collect_events(bars, None, sess).sort_values(key).reset_index(drop=True)
    b = F.collect_events_parallel(bars, None, sess, workers=2).sort_values(key).reset_index(drop=True)
    assert len(a) > 0 and a.equals(b)
    assert (a[a["feature"].str.startswith("pat_")].groupby(["feature", "session"]).size() <= 4).all()
    settings.get_secrets.cache_clear()
