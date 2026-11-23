"""Market condition gauges: the agreed scenarios, knob geometry, states."""

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from mmm.analytics.condition import compute, label
from mmm.demo import demo_minute_bars
from mmm.signals.engine import FrameCache, build_context
from mmm.timeutil import IST

HIST = demo_minute_bars(days=40, seed=4, end=date(2026, 11, 12))
PC = float(HIST["close"].iloc[-1])
T0 = datetime(2026, 11, 13, 9, 15, tzinfo=IST)
N = 300


def _ctx(path: np.ndarray, noise: float = 2.0, minutes: int = N):
    rng = np.random.default_rng(1)
    rows, prev = [], path[0]
    for i, c in enumerate(path):
        c = c + rng.normal(0, noise)
        rows.append({"ts": T0 + timedelta(minutes=i), "open": prev, "high": max(prev, c) + abs(rng.normal(0, noise)),
                     "low": min(prev, c) - abs(rng.normal(0, noise)), "close": c, "volume": 0, "oi": 0})
        prev = c
    bars = pd.concat([HIST, pd.DataFrame(rows)], ignore_index=True)
    return build_context(None, T0 + timedelta(minutes=minutes), cache=FrameCache(bars, "5minute", "15minute", "60minute"))


def _g(st):
    return {g.name: g for g in st.gauges}


def test_strong_trend_up_puts_bullish_knob_right():
    g = _g(compute(_ctx(PC + np.linspace(0, 300, N))))
    assert g["BULLISH"].score >= 70 and g["BULLISH"].knob >= 70 and g["BULLISH"].label == "STRONG"
    assert g["BEARISH"].score < 5 and g["BEARISH"].knob > 95                 # absent bearish sits at the right
    assert g["SIDEWAYS"].score < 30 and g["SIDEWAYS"].knob > 60              # sideways knob drifts towards the bullish side
    assert g["BULLISH"].reasons


def test_strong_trend_down_puts_bearish_knob_left():
    g = _g(compute(_ctx(PC - np.linspace(0, 300, N))))
    assert g["BEARISH"].score >= 40 and g["BEARISH"].knob <= 60
    assert g["BULLISH"].score < 20 and g["SIDEWAYS"].knob < 40


def test_quiet_range_centres_the_sideways_knob():
    g = _g(compute(_ctx(PC + 15 * np.sin(np.arange(N) / 9))))
    assert g["SIDEWAYS"].score >= 70 and 40 <= g["SIDEWAYS"].knob <= 60 and g["SIDEWAYS"].sublabel == "quiet range"
    assert g["BULLISH"].score < 20 and g["BEARISH"].score < 20


def test_volatile_chop_is_labelled_as_such():
    g = _g(compute(_ctx(PC + 120 * np.sin(np.arange(N) / 6), noise=8.0)))
    assert g["SIDEWAYS"].score >= 60 and g["SIDEWAYS"].sublabel == "volatile chop"


def test_rally_then_breakdown_shows_both_sides():
    g = _g(compute(_ctx(PC + np.r_[np.linspace(0, 200, 150), np.linspace(200, -40, 150)])))
    assert g["BULLISH"].score > 15 and g["BEARISH"].score > 15            # evidence on both sides, not hidden


def test_states_and_horizons():
    ctx = _ctx(PC + np.linspace(0, 300, N))
    assert compute(ctx, demo=True).state == "DEMO"
    assert compute(ctx, stale=True).state == "STALE"
    assert compute(ctx, market_open=False).state == "AS_OF_CLOSE"
    assert compute(ctx).state == "LIVE"
    early = _ctx(PC + np.linspace(0, 30, N), minutes=10)
    assert compute(early).state == "FORMING"
    for h in ("60minute", "day"):
        st = compute(ctx, h)
        assert len(st.gauges) == 3 and all(0 <= g.knob <= 100 for g in st.gauges)
    with pytest.raises(ValueError):
        compute(ctx, "weekly")


def test_labels():
    assert [label(x) for x in (0, 19.9, 20, 39, 40, 69, 70, 100)] == ["NONE", "NONE", "WEAK", "WEAK", "MODERATE", "MODERATE", "STRONG", "STRONG"]
