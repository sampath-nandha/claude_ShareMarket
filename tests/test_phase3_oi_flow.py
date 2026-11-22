"""Phase 3: option OI flow from archived option minute candles, walls, PCR, build-up, PA14."""

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from mmm.analytics.oi_flow import ChainHistory, buildup_label
from mmm.data import parquet_store, paths
from mmm.demo import demo_minute_bars
from mmm.signals.auxdata import AuxData
from mmm.signals.engine import FrameCache, build_context
from mmm.strategies.pa14_oi_wall import OIWall
from mmm.timeutil import IST

DAY, PREV, EXP = date(2026, 11, 13), date(2026, 11, 12), date(2026, 11, 17)


@pytest.fixture()
def archive(tmp_path, monkeypatch):
    monkeypatch.setenv("MMM_DATA_DIR", str(tmp_path))
    from mmm import settings

    settings.get_secrets.cache_clear()
    yield tmp_path
    settings.get_secrets.cache_clear()


def test_buildup_labels():
    assert buildup_label(5, 10) == "LONG_BUILDUP"
    assert buildup_label(-5, 10) == "SHORT_BUILDUP"
    assert buildup_label(5, -10) == "SHORT_COVERING"
    assert buildup_label(-5, -10) == "LONG_UNWINDING"
    assert buildup_label(5, 1) == "NEUTRAL"


def _write_chain(atm: float, call_wall_oi_today, n_today: int = 200):
    """Strikes atm-250..atm+250. Call wall at atm+100; its OI follows `call_wall_oi_today(minute)` today."""
    strikes = [atm + 50 * k for k in range(-5, 6)]
    tok = 50_000
    meta = []
    for k in strikes:
        for side in ("CE", "PE"):
            tok += 1
            meta.append({"instrument_token": tok, "tradingsymbol": f"NIFTY{int(k)}{side}", "name": "NIFTY", "expiry": pd.Timestamp(EXP),
                         "strike": k, "instrument_type": side, "exchange": "NFO"})
            base = 200_000 + (800_000 if (side == "CE" and k == atm + 100) else 0) + (500_000 if (side == "PE" and k == atm - 100) else 0)
            frames = []
            for d, n in ((PREV, 375), (DAY, n_today)):
                t0 = datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST)
                ts = [t0 + timedelta(minutes=i) for i in range(n)]
                if d == DAY and side == "CE" and k == atm + 100:
                    oi = [call_wall_oi_today(i) for i in range(n)]
                else:
                    oi = [base] * n
                px = np.full(n, 50.0)
                frames.append(pd.DataFrame({"ts": ts, "open": px, "high": px, "low": px, "close": px, "volume": 100, "oi": oi}))
            parquet_store.write_candles(pd.concat(frames, ignore_index=True), "minute", tok)
    inst = pd.DataFrame(meta)
    for c in ("last_price", "tick_size", "lot_size", "exchange_token"):
        inst[c] = 0
    inst["segment"] = "NFO-OPT"
    inst["snapshot_date"] = pd.Timestamp(PREV)
    inst.to_parquet(paths.instruments_dir(PREV) / "instruments.parquet", index=False)


def test_chain_history_walls_pcr_and_causality(archive):
    atm = 25000.0
    _write_chain(atm, lambda i: 1_000_000 - (3_000 * max(0, i - 120)))     # call wall unwinds after 11:15
    ch = ChainHistory.from_archive()
    assert not ch.empty and ch.days == {PREV, DAY}
    early = ch.state(datetime(2026, 11, 13, 10, 0, tzinfo=IST), atm + 20)
    assert early.call_wall.strike == atm + 100 and early.put_wall.strike == atm - 100
    assert early.call_wall.status == "NEUTRAL" and early.pcr_near is not None
    late = ch.state(datetime(2026, 11, 13, 12, 15, tzinfo=IST), atm + 20)
    assert late.call_wall.status == "UNWINDING" and late.call_wall.oi_chg_30_pct <= -5
    # causal: nothing after as_of is visible
    ca = ch.state(datetime(2026, 11, 13, 11, 16, tzinfo=IST), atm)
    assert ca.call_wall.oi == 1_000_000
    assert ch.state(datetime(2026, 11, 13, 9, 15, tzinfo=IST), atm) is None
    lv = {lv.kind: lv for lv in late.wall_levels()}
    assert lv["OI_R"].price == atm + 100 and "UNWINDING" in lv["OI_R"].tags
    hm = ch.heatmap(DAY)
    assert {"ts", "oi_chg", "strike", "side"} <= set(hm.columns)
    assert hm[(hm["strike"] == atm + 100) & (hm["side"] == "CE")]["oi_chg"].min() < 0
    assert ch.slice(DAY, DAY).n_days == 2          # keeps the previous session for change-since-close


def test_pa14_breakout_through_unwinding_call_wall(archive):
    hist = demo_minute_bars(days=25, seed=5, end=PREV)
    base = float(hist["close"].iloc[-1])
    atm = round(base / 50) * 50
    wall = atm + 100
    n = 200
    _write_chain(atm, lambda i: 1_000_000 - (4_000 * max(0, i - 150)), n_today=n)
    t0 = datetime(2026, 11, 13, 9, 15, tzinfo=IST)
    path = np.r_[np.linspace(base, wall - 15, n - 10), np.linspace(wall - 15, wall - 5, 5), np.linspace(wall + 2, wall + 12, 5)]
    today = pd.DataFrame({"ts": [t0 + timedelta(minutes=i) for i in range(n)], "open": path, "high": path + 1, "low": path - 1,
                          "close": path, "volume": 0, "oi": 0})
    bars = pd.concat([hist[hist["ts"].dt.date < DAY], today], ignore_index=True)
    aux = AuxData(None, ChainHistory.from_archive())
    cache = FrameCache(bars, "5minute", "15minute", "60minute")
    ctx = build_context(None, t0 + timedelta(minutes=n), cache=cache, aux=aux)
    st = ctx.extras["oi"]
    assert st.call_wall.strike == wall and st.call_wall.status == "UNWINDING"
    assert any(lv.kind == "OI_R" and lv.price == wall for lv in ctx.levels)
    strat = OIWall()
    strat.min_reward_risk = 0.5          # targets depend on levels in the random history; test the trigger itself
    cand = strat.evaluate(ctx)
    assert cand is not None and cand.direction == "BULLISH" and cand.trigger_event == "OI_WALL_BREAKOUT"
    assert cand.stop < wall
