
import shutil
from datetime import date, datetime

import pandas as pd
import pytest

from mmm.analytics.candles import resample
from mmm.data import parquet_store
from mmm.data.state_db import StateDB
from mmm.data.ticks import CandleAggregator, normalise_tick
from mmm.data.verify import SessionCalendar, check, repair, summarise
from mmm.demo import demo_minute_bars
from mmm.timeutil import IST


def test_verify_detects_missing_and_partial_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("MMM_DATA_DIR", str(tmp_path))
    from mmm import settings

    settings.get_secrets.cache_clear()
    bars = demo_minute_bars(days=6, seed=2)
    sessions = sorted(bars["ts"].dt.date.unique())
    drop_day, partial_day = sessions[2], sessions[4]
    bars = bars[bars["ts"].dt.date != drop_day]
    bars = bars[~((bars["ts"].dt.date == partial_day) & (bars["ts"].dt.hour >= 13))]
    parquet_store.write_candles(bars, "minute", 1)
    cov = check(1, "X", "minute")
    assert cov.missing == [drop_day]
    assert [p[0] for p in cov.partial] == [partial_day]
    assert not cov.ok
    df = summarise([cov])
    assert df.iloc[0]["status"] == "GAPS"
    full = check(1, "X", "minute", start=sessions[0], end=sessions[1])
    assert full.ok
    settings.get_secrets.cache_clear()


# --- source calendar, source-confirmed ledger, bad timestamps -------------------------------------------------
INDEX, FUT = 256265, 999


@pytest.fixture()
def archive(tmp_path, monkeypatch):
    monkeypatch.setenv("MMM_DATA_DIR", str(tmp_path))
    from mmm import settings

    settings.get_secrets.cache_clear()
    yield tmp_path
    settings.get_secrets.cache_clear()


def _days(df):
    return sorted(df["ts"].dt.date.unique())


def test_zero_epoch_tick_never_becomes_a_1970_candle(archive):
    recv = datetime(2026, 11, 20, 10, 0, 5, tzinfo=IST)
    row = normalise_tick({"instrument_token": FUT, "last_price": 22000.0, "exchange_timestamp": datetime(1970, 1, 1, 5, 30),
                          "last_trade_time": datetime(1970, 1, 1, 5, 30)}, recv)
    assert row["exchange_timestamp"] is None and row["last_trade_time"] is None
    agg = CandleAggregator()
    agg.on_tick(row)
    assert agg.bars[FUT].start == datetime(2026, 11, 20, 10, 0, tzinfo=IST)

    bad = pd.DataFrame([{"ts": pd.Timestamp("1970-01-01 05:30", tz=IST), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0, "oi": 0},
                        {"ts": pd.Timestamp("2026-11-20 10:00", tz=IST), "open": 2, "high": 2, "low": 2, "close": 2, "volume": 0, "oi": 0}])
    assert parquet_store.write_candles(bad, "minute", FUT) == 1
    assert not list(archive.rglob("year=1970.parquet"))
    assert parquet_store.candle_coverage("minute", FUT)[0].year == 2026


def test_calendar_comes_from_index_daily_candles(archive):
    bars = demo_minute_bars(days=8, seed=3, end=date(2026, 11, 13))
    days = _days(bars)
    holiday, short = days[2], days[5]                       # an "unlisted holiday" and a short special session
    idx = bars[bars["ts"].dt.date != holiday]
    idx = idx[~((idx["ts"].dt.date == short) & (idx["ts"] >= pd.Timestamp(short).tz_localize(IST) + pd.Timedelta(hours=10, minutes=15)))]
    parquet_store.write_candles(idx, "minute", INDEX)
    parquet_store.write_candles(resample(idx, "day"), "day", INDEX)
    fut = idx[idx["ts"].dt.date != days[6]]                  # the future really misses one session
    parquet_store.write_candles(fut, "minute", FUT)

    cal = SessionCalendar.from_archive(INDEX)
    assert holiday not in cal.days and cal.session_minutes(short) == 60
    cov = check(FUT, "FUT", "minute", calendar=cal)
    assert cov.missing == [days[6]]                          # the holiday is not reported, the real gap is
    assert cov.partial == []                                 # the short session is not partial for the future
    ref = check(INDEX, "NIFTY 50", "minute", calendar=cal, reference=True)
    assert ref.missing == [] and [p[0] for p in ref.partial] == [short]   # until the ledger confirms it


class _Kite:
    def __init__(self, fail: bool = False):
        self.fail, self.calls = fail, 0

    def historical(self, *_a, **_k):
        self.calls += 1
        if self.fail:
            raise RuntimeError("network down")
        return []                                            # the source has nothing more


def test_repair_confirms_days_the_source_cannot_fill(archive):
    bars = demo_minute_bars(days=6, seed=4, end=date(2026, 11, 13))
    days = _days(bars)
    thin = bars[~((bars["ts"].dt.date == days[3]) & (bars["ts"].dt.minute % 2 == 0))]   # a thinly traded day
    parquet_store.write_candles(thin, "minute", FUT)
    state = StateDB(archive / "s.sqlite")

    cov = check(FUT, "OPT", "minute")
    assert [p[0] for p in cov.partial] == [days[3]]
    repair(_Kite(fail=True), cov, state=state)               # a failed fetch must not be confirmed
    assert state.source_confirmed() == {}

    kite = _Kite()
    repair(kite, cov, state=state)
    assert kite.calls >= 1
    ledger = state.source_confirmed()[(FUT, "minute")]
    assert days[3].isoformat() in ledger
    again = check(FUT, "OPT", "minute", confirmed=ledger)
    assert again.ok and again.confirmed == 1
    assert summarise([again]).iloc[0]["status"] == "OK*"

    # bars lost later from the archive -> the day is flagged again
    lost = thin[~((thin["ts"].dt.date == days[3]) & (thin["ts"].dt.hour >= 12))]
    shutil.rmtree(archive / "candles")
    parquet_store.write_candles(lost, "minute", FUT)
    assert not check(FUT, "OPT", "minute", confirmed=ledger).ok
