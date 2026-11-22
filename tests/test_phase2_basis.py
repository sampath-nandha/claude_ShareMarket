"""Phase 2: fair-value basis, stitched futures, divergence, causal access, PA13."""

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from mmm.analytics.basis import basis_frame, basis_state, divergence, fair_basis, premium_confirmation
from mmm.data import parquet_store, paths
from mmm.data.futures_series import continuous_futures, monthly_expiry
from mmm.demo import demo_minute_bars
from mmm.signals.aux import AuxData
from mmm.signals.engine import FrameCache, build_context
from mmm.strategies.pa13_basis_divergence import BasisDivergence
from mmm.timeutil import IST


@pytest.fixture()
def archive(tmp_path, monkeypatch):
    monkeypatch.setenv("MMM_DATA_DIR", str(tmp_path))
    from mmm import settings

    settings.get_secrets.cache_clear()
    yield tmp_path
    settings.get_secrets.cache_clear()


def test_fair_basis_decays_to_zero_at_expiry():
    e = date(2026, 11, 24)
    far = fair_basis(25000, e, datetime(2026, 10, 27, 10, 0, tzinfo=IST), r=0.065, q=0.0)
    near = fair_basis(25000, e, datetime(2026, 11, 23, 10, 0, tzinfo=IST), r=0.065, q=0.0)
    assert far == pytest.approx(25000 * (np.exp(0.065 * (28 * 86400 + 5.5 * 3600) / (365 * 86400)) - 1), rel=1e-3)
    assert 0 < near < far
    assert fair_basis(25000, e, datetime(2026, 11, 24, 15, 30, tzinfo=IST)) == 0


def test_monthly_expiry_follows_the_weekday_rules():
    assert monthly_expiry(date(2025, 8, 5)).weekday() == 3       # Thursday era
    assert monthly_expiry(date(2026, 11, 2)) == date(2026, 11, 24)   # last Tuesday
    assert monthly_expiry(date(2026, 11, 25)) == date(2026, 12, 29)


def _instruments(archive, rows):
    inst = pd.DataFrame(rows)
    for c in ("strike", "last_price", "tick_size", "lot_size", "exchange_token"):
        inst[c] = 0
    inst["segment"] = "NFO-FUT"
    inst["snapshot_date"] = pd.Timestamp("2026-11-13")
    inst.to_parquet(paths.instruments_dir(date(2026, 11, 13)) / "instruments.parquet", index=False)


def test_continuous_futures_rolls_before_expiry(archive):
    spot = demo_minute_bars(days=12, seed=3, end=date(2026, 11, 13))
    days = sorted(spot["ts"].dt.date.unique())
    e1, e2 = days[5], days[-1] + timedelta(days=30)
    _instruments(archive, [
        {"instrument_token": 11, "tradingsymbol": "NIFTYAFUT", "name": "NIFTY", "expiry": pd.Timestamp(e1), "instrument_type": "FUT", "exchange": "NFO"},
        {"instrument_token": 12, "tradingsymbol": "NIFTYBFUT", "name": "NIFTY", "expiry": pd.Timestamp(e2), "instrument_type": "FUT", "exchange": "NFO"},
    ])
    parquet_store.write_candles(spot[spot["ts"].dt.date <= e1].assign(close=lambda x: x["close"] + 40), "minute", 11)
    parquet_store.write_candles(spot.assign(close=lambda x: x["close"] + 120), "minute", 12)
    fut = continuous_futures(roll_days=2)
    by_day = fut.groupby(fut["ts"].dt.date)["token"].first()
    assert by_day[days[0]] == 11 and by_day[days[5]] == 12      # rolled 2 days before e1
    assert set(fut["source"]) == {"contract"}
    bf = basis_frame(spot, fut)
    assert len(bf) == len(spot)
    assert (bf.loc[bf["ts"].dt.date == days[0], "expiry"] == e1).all()


def _parity_day(prem_path: np.ndarray, day: date = date(2026, 11, 13)):
    """One session where spot rallies to a high while the futures premium follows `prem_path`."""
    n = len(prem_path)
    t0 = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
    sp = 25000 + np.linspace(0, 60, n)
    ts = [t0 + timedelta(minutes=i) for i in range(n)]
    spot = pd.DataFrame({"ts": ts, "open": sp, "high": sp + 1, "low": sp - 1, "close": sp, "volume": 0, "oi": 0})
    fair = np.array([fair_basis(s, date(2026, 11, 24), t) for s, t in zip(sp, ts)])
    fut = pd.DataFrame({"ts": ts, "close": sp + fair + prem_path, "oi": np.linspace(1e7, 1.02e7, n), "expiry": date(2026, 11, 24)})
    return spot, fut


def test_divergence_detects_a_lagging_premium():
    prem = np.r_[np.full(40, 10.0), np.linspace(10, 0, 31)]
    spot, fut = _parity_day(prem)
    bf = basis_frame(spot, fut, z_min=10)
    dv = divergence(bf, lookback=30)
    assert dv is not None and dv.kind == "BEARISH" and dv.premium_change == pytest.approx(-10, abs=0.5)
    assert any("OI" in e for e in dv.evidence)
    st = basis_state(bf)
    assert st.divergence is not None and premium_confirmation(st, -1) > 0 > premium_confirmation(st, 1)
    steady = basis_frame(*_parity_day(np.full(71, 10.0)), z_min=10)
    assert divergence(steady, 30) is None


def test_aux_basis_is_causal():
    prem = np.r_[np.full(40, 10.0), np.linspace(10, 0, 31)]
    bf = basis_frame(*_parity_day(prem), z_min=10)
    aux = AuxData(bf)
    as_of = bf["ts"].iloc[50].to_pydatetime()
    today = aux.basis_today(as_of)
    assert today["ts"].max() < as_of and len(today) == 50
    assert aux.basis_today(as_of + timedelta(days=3)) is None


def test_pa13_triggers_on_divergence_at_the_session_high():
    hist = demo_minute_bars(days=25, seed=21, end=date(2026, 11, 12))
    base = float(hist["close"].iloc[-1])
    n = 180
    t0 = datetime(2026, 11, 13, 9, 15, tzinfo=IST)
    up = base + np.r_[np.linspace(0, 80, n - 5), np.full(5, 80.0)]
    ts = [t0 + timedelta(minutes=i) for i in range(n)]
    today = pd.DataFrame({"ts": ts, "open": up, "high": up + 1.5, "low": up - 1.5, "close": up, "volume": 0, "oi": 0})
    # last 5-minute bar: pokes the high and closes weak
    today.loc[n - 5:, "high"] = up.max() + 2
    today.loc[n - 1, "close"] = up[n - 1] - 0.5
    bars = pd.concat([hist, today], ignore_index=True)
    prem = np.r_[np.full(n - 31, 12.0), np.linspace(12, 0, 31)]
    fair = np.array([fair_basis(s, date(2026, 11, 24), t) for s, t in zip(today["close"], ts)])
    fut = pd.DataFrame({"ts": ts, "close": today["close"].to_numpy() + fair + prem, "oi": np.linspace(1e7, 1.03e7, n),
                        "expiry": date(2026, 11, 24)})
    aux = AuxData(basis_frame(today, fut, z_min=10))
    cache = FrameCache(bars, "5minute", "15minute", "60minute")
    ctx = build_context(None, t0 + timedelta(minutes=n), cache=cache, aux=aux)
    st = ctx.extras["basis"]
    assert st is not None and st.divergence is not None and st.divergence.kind == "BEARISH"
    strat = BasisDivergence()
    strat.min_reward_risk = 0.5     # the R:R gate depends on levels in the random history; test the trigger itself
    cand = strat.evaluate(ctx)
    assert cand is not None and cand.direction == "BEARISH" and cand.stop > float(today["high"].max())
    assert cand.trigger_event == "BASIS_DIVERGENCE_BEARISH"
