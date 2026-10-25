from datetime import datetime, timedelta


from mmm.analytics.candles import completed_only, resample
from mmm.analytics.levels import reference_levels, round_levels, swing_zones
from mmm.analytics.structure import candle_anatomy, classify_structure, pivot_swings, zigzag_swings
from mmm.analytics.volatility import atr
from mmm.data.ticks import CandleAggregator, normalise_tick
from mmm.demo import demo_minute_bars
from mmm.timeutil import IST


def test_candle_aggregator_emits_completed_bars_only():
    agg = CandleAggregator()
    t0 = datetime(2026, 1, 5, 9, 15, 10, tzinfo=IST)
    ticks = [
        {"instrument_token": 1, "last_price": 100.0, "volume_traded": 1000, "exchange_timestamp": t0},
        {"instrument_token": 1, "last_price": 101.0, "volume_traded": 1500, "exchange_timestamp": t0 + timedelta(seconds=20)},
        {"instrument_token": 1, "last_price": 99.5, "volume_traded": 1800, "exchange_timestamp": t0 + timedelta(seconds=40)},
    ]
    out = [agg.on_tick(normalise_tick(t, t0)) for t in ticks]
    assert all(o is None for o in out)
    nxt = {"instrument_token": 1, "last_price": 100.5, "volume_traded": 2000, "exchange_timestamp": t0 + timedelta(minutes=1)}
    bar = agg.on_tick(normalise_tick(nxt, t0))
    assert bar is not None
    assert (bar["open"], bar["high"], bar["low"], bar["close"]) == (100.0, 101.0, 99.5, 99.5)
    assert bar["volume"] == 800  # 1800 - 1000 cumulative delta
    assert bar["ts"] == t0.replace(second=0, microsecond=0)


def test_index_tick_has_no_fabricated_volume():
    t = {"instrument_token": 256265, "last_price": 24000.0, "ohlc": {"open": 1, "high": 2, "low": 0.5, "close": 1.5},
         "exchange_timestamp": datetime(2026, 1, 5, 10, 0, tzinfo=IST)}
    row = normalise_tick(t)
    assert row["volume_traded"] is None and row["oi"] is None and row["bid1"] is None


def test_resample_anchors_at_0915_and_drops_incomplete():
    bars = demo_minute_bars(days=3)
    five = resample(bars, "5minute")
    assert five["ts"].iloc[0].time().minute % 5 == 0
    assert five["ts"].iloc[0].time().hour == 9 and five["ts"].iloc[0].time().minute == 15
    as_of = five["ts"].iloc[-1] + timedelta(minutes=2)
    cut = completed_only(five, "5minute", as_of)
    assert len(cut) == len(five) - 1
    sixty = resample(bars, "60minute")
    assert sixty["ts"].iloc[1] - sixty["ts"].iloc[0] == timedelta(minutes=60)


def test_pivot_swings_confirmation_delay_and_labels():
    bars = demo_minute_bars(days=5)
    five = resample(bars, "5minute")
    sw = pivot_swings(five, 3, 3)
    assert sw, "expected swings"
    for s in sw:
        assert s.confirmed_at == s.idx + 3
    kinds = [s.kind for s in sw]
    assert all(a != b for a, b in zip(kinds, kinds[1:]))  # alternation enforced
    labels = {s.label for s in sw if s.label}
    assert labels <= {"HH", "HL", "LH", "LL", "EQH", "EQL"}
    zz = zigzag_swings(five, 2.0)
    assert all(a.kind != b.kind for a, b in zip(zz, zz[1:]))


def test_structure_and_levels_run():
    bars = demo_minute_bars(days=6)
    five = resample(bars, "5minute").reset_index(drop=True)
    sw = pivot_swings(five)
    st = classify_structure(five, sw)
    assert st.trend in {"STRONG_BULL", "WEAK_BULL", "STRONG_BEAR", "WEAK_BEAR", "RANGE", "TRANSITION", "UNCERTAIN"}
    zones = swing_zones(five, sw, len(five) - 1, tf="5minute")
    assert all(0 <= z.strength <= 100 for z in zones)
    daily = resample(bars, "day")
    today = bars["ts"].iloc[-1].date()
    intraday = bars[bars["ts"].dt.date == today]
    lv = reference_levels(daily, intraday, today)
    kinds = {x.kind for x in lv}
    assert {"PDH", "PDL", "PDC", "OPEN", "SH", "SL", "ORH", "ORL"} <= kinds
    assert len(round_levels(24_037.0)) == 7
    an = candle_anatomy(five)
    assert ((an["body_ratio"].dropna() >= 0) & (an["body_ratio"].dropna() <= 1.0001)).all()
    assert atr(five).dropna().gt(0).all()
