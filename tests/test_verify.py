
from mmm.data import parquet_store
from mmm.data.verify import check, summarise
from mmm.demo import demo_minute_bars


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
