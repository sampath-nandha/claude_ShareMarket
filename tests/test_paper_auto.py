"""Automatic paper trader: budget-limited contract choice, entries, exits, limits, logging, backtest of the rules."""

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from mmm import demo
from mmm.analytics.options.chain import enrich_chain
from mmm.data.state_db import StateDB
from mmm.paper import auto as A
from mmm.signals.model import SetupCandidate
from mmm.timeutil import IST

DAY = date(2026, 11, 13)
EXP = date(2026, 11, 17)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MMM_DATA_DIR", str(tmp_path))
    from mmm import settings

    settings.get_secrets.cache_clear()
    yield tmp_path
    settings.get_secrets.cache_clear()


class Fake:
    def __init__(self, bundle, quotes, now):
        self.b, self.quotes, self.t = bundle, quotes, now

    def now(self):
        return self.t

    def market_open(self):
        return self.t.time() < datetime(2000, 1, 1, 15, 30).time()

    def bundle(self):
        return self.b

    def aux(self, b):
        return None

    def quote(self, token):
        q = self.quotes.get(int(token))
        return None if q is None else {**q, "received_ts": self.t.isoformat()}

    def lot_size(self, token):
        return 75


def _bundle(minutes: int = 120, demo_flag: bool = False):
    from mmm.web.data import Bundle

    hist = demo.demo_minute_bars(days=30, seed=3, end=DAY)
    hist = hist[hist["ts"] < datetime(2026, 11, 13, 9, 15, tzinfo=IST) + timedelta(minutes=minutes)]
    spot = float(hist["close"].iloc[-1])
    as_of = hist["ts"].iloc[-1].to_pydatetime() + timedelta(minutes=1)
    chain = enrich_chain(demo.demo_option_chain(spot, EXP, as_of, seed=7), spot, as_of)
    return Bundle(demo_flag, as_of, spot, hist, chain, [EXP], {}, pd.DataFrame(), 50)


def _cand(direction="BULLISH", spot=25000.0, stop_off=40.0, t1_off=60.0, ts=None):
    d = 1 if direction == "BULLISH" else -1
    return SetupCandidate(strategy_id="PA01", direction=direction, timeframe="5minute", trigger_event="TEST", entry=spot,
                          stop=spot - d * stop_off, target1=spot + d * t1_off, target2=spot + d * 2 * t1_off, invalidation="x",
                          pa_score=70, evidence=["test"], bar_ts=ts or datetime(2026, 11, 13, 11, 10, tzinfo=IST))


def _patch_stage1(monkeypatch, cand):
    from mmm.signals import engine
    from mmm.strategies import load_strategies

    strat = load_strategies(["PA01"], apply_registry=False)[0]
    monkeypatch.setattr(engine, "stage1", lambda ctx, strategies=None, **k: [(strat, cand)] if cand else [])


def _quotes(b, bump=0.0):
    return {int(r.instrument_token): {"last_price": r.last_price + bump, "bid": r.bid + bump, "ask": r.ask + bump} for r in b.chain.itertuples()}


def test_pick_contract_fits_the_budget():
    b = _bundle()
    pick, why = A.pick_contract(b.chain, 1, b.spot, EXP, 10_000, lambda t: 75, (0.0, 1.0), 10)
    assert pick is not None and pick["option_type"] == "CE"
    cost = pick["price"] * 75 * pick["lots"]
    assert cost <= 10_000 and pick["strike"] >= round(b.spot / 50) * 50 - 25          # ATM/OTM, never deep ITM
    expensive = b.chain[(b.chain["option_type"] == "CE")]
    atm = expensive.iloc[(expensive["strike"] - b.spot).abs().argsort()[:1]]
    if float(atm["ask"].iloc[0]) * 75 > 10_000:
        assert pick["strike"] > float(atm["strike"].iloc[0]) and any("budget" in w for w in why)
    none, why2 = A.pick_contract(b.chain, 1, b.spot, EXP, 50, lambda t: 75, (0.0, 1.0), 10)
    assert none is None and why2


def test_entry_then_target_exit_with_costs_and_log(env, monkeypatch):
    b = _bundle(minutes=115)
    st = StateDB(env / "s.sqlite")
    now = datetime(2026, 11, 13, 11, 10, 15, tzinfo=IST)
    fake = Fake(b, _quotes(b), now)
    _patch_stage1(monkeypatch, _cand(spot=b.spot, stop_off=10, t1_off=20))
    tr = A.PaperTrader(fake, st)
    tr.step()
    pos = tr.position()
    assert pos is not None and pos.lots * pos.lot_size * pos.entry_price <= 10_000
    assert st.paper_logs()[0]["event"] == "ENTRY"
    # price rallies through target 1 in the next minutes; option bid rises
    nb = b.minute_bars.copy()
    extra = pd.DataFrame([{"ts": now.replace(second=0) + timedelta(minutes=k), "open": b.spot, "high": b.spot + 30, "low": b.spot - 2,
                           "close": b.spot + 25, "volume": 0, "oi": 0} for k in range(2)])
    b.minute_bars = pd.concat([nb, extra], ignore_index=True)
    fake.quotes = _quotes(b, bump=15)
    fake.t = now + timedelta(minutes=3)
    tr.step()
    assert tr.position() is None
    ex = st.paper_logs()[0]
    assert ex["event"] == "EXIT" and ex["detail"]["reason"] == "TARGET1" and ex["detail"]["costs"] > 0
    t = tr.trades()
    assert len(t) == 1 and t.iloc[0]["pnl_net"] == pytest.approx(t.iloc[0]["pnl_gross"] - t.iloc[0]["costs"])
    assert tr.equity() == pytest.approx(10_000 + t.iloc[0]["pnl_net"])


@pytest.mark.parametrize("scenario,reason", [("premium", "PREMIUM_STOP"), ("time", "TIME"), ("stop", "STOP")])
def test_other_exits(env, monkeypatch, scenario, reason):
    b = _bundle(minutes=115)
    st = StateDB(env / "s.sqlite")
    now = datetime(2026, 11, 13, 11, 10, 15, tzinfo=IST)
    fake = Fake(b, _quotes(b), now)
    _patch_stage1(monkeypatch, _cand(spot=b.spot, stop_off=40, t1_off=500))
    tr = A.PaperTrader(fake, st)
    tr.step()
    pos = tr.position()
    if scenario == "premium":
        fake.quotes = {pos.token: {"last_price": pos.entry_price * 0.4, "bid": pos.entry_price * 0.4, "ask": pos.entry_price * 0.42}}
        fake.t = now + timedelta(minutes=1)
    elif scenario == "time":
        fake.t = now + timedelta(minutes=121)
    else:
        b.minute_bars = pd.concat([b.minute_bars, pd.DataFrame([{"ts": now.replace(second=0), "open": b.spot, "high": b.spot, "low": b.spot - 60,
                                                                   "close": b.spot - 50, "volume": 0, "oi": 0}])], ignore_index=True)
        fake.t = now + timedelta(minutes=2)
    tr.step()
    assert tr.position() is None and st.paper_logs()[0]["detail"]["reason"] == reason


def test_daily_trade_limit_and_demo_guard(env, monkeypatch):
    b = _bundle(minutes=115)
    st = StateDB(env / "s.sqlite")
    now = datetime(2026, 11, 13, 11, 10, 15, tzinfo=IST)
    fake = Fake(b, _quotes(b), now)
    _patch_stage1(monkeypatch, _cand(spot=b.spot, t1_off=500))
    tr = A.PaperTrader(fake, st)
    monkeypatch.setattr(tr.cfg, "max_trades_per_day", 1)
    tr.step()
    fake.t = now + timedelta(minutes=121)
    tr.step()                                     # time exit
    fake.t = datetime(2026, 11, 13, 13, 15, 15, tzinfo=IST)
    tr.step()
    assert st.paper_logs()[0]["event"] == "SKIP" and "limit" in st.paper_logs()[0]["message"]
    demo_b = _bundle(demo_flag=True)
    tr2 = A.PaperTrader(Fake(demo_b, {}, now), StateDB(env / "d.sqlite"))
    tr2.step()
    assert tr2.position() is None and "demo" in tr2.state.paper_logs()[0]["message"]


def test_no_setup_is_logged_and_pause_stops_trading(env, monkeypatch):
    b = _bundle(minutes=115)
    st = StateDB(env / "s.sqlite")
    fake = Fake(b, _quotes(b), datetime(2026, 11, 13, 11, 10, 15, tzinfo=IST))
    _patch_stage1(monkeypatch, None)
    tr = A.PaperTrader(fake, st)
    tr.step()
    assert st.paper_logs()[0]["event"] == "SCAN" and "no setup" in st.paper_logs()[0]["message"]
    _patch_stage1(monkeypatch, _cand(spot=b.spot))
    tr.set_paused(True)
    fake.t = datetime(2026, 11, 13, 11, 15, 15, tzinfo=IST)
    tr.step()
    assert tr.position() is None


def test_nearest_expiry_rolls_after_1pm_on_expiry_day():
    exps = [date(2026, 11, 17), date(2026, 11, 24)]
    assert A.nearest_expiry(exps, datetime(2026, 11, 17, 10, 0, tzinfo=IST)) == date(2026, 11, 17)
    assert A.nearest_expiry(exps, datetime(2026, 11, 17, 13, 5, tzinfo=IST)) == date(2026, 11, 24)
    assert A.nearest_expiry(exps, datetime(2026, 11, 18, 10, 0, tzinfo=IST)) == date(2026, 11, 24)


def test_backtest_paper_reports_direction_and_option_confidence(env):
    from mmm.data import parquet_store, paths

    bars = demo.demo_minute_bars(days=30, seed=5, end=DAY)
    spot = float(bars["close"].iloc[-1])
    atm = round(spot / 50) * 50
    rows, tok = [], 7000
    days = sorted(bars["ts"].dt.date.unique())
    for k in range(-20, 21):
        for t in ("CE", "PE"):
            tok += 1
            rows.append({"instrument_token": tok, "tradingsymbol": f"NIFTYX{int(atm + 50 * k)}{t}", "name": "NIFTY", "expiry": pd.Timestamp(EXP),
                         "strike": atm + 50 * k, "instrument_type": t, "exchange": "NFO", "lot_size": 75})
            intrinsic = np.maximum(bars["close"] - (atm + 50 * k), 0) if t == "CE" else np.maximum((atm + 50 * k) - bars["close"], 0)
            px = intrinsic + 40
            parquet_store.write_candles(pd.DataFrame({"ts": bars["ts"], "open": px, "high": px + 2, "low": px - 2, "close": px, "volume": 1, "oi": 1}), "minute", tok)
    inst = pd.DataFrame(rows)
    for c in ("last_price", "tick_size", "exchange_token"):
        inst[c] = 0
    inst["segment"] = "NFO-OPT"
    inst["snapshot_date"] = pd.Timestamp(days[0])
    inst.to_parquet(paths.instruments_dir(days[0]) / "instruments.parquet", index=False)
    r = A.backtest_paper(bars)
    assert r["sessions"] > 0 and "direction" in r and "option" in r
    if r["option"]["evaluated"]:
        ot = pd.DataFrame(r["option"]["trades"])
        assert (ot["invested"] <= 10_000).all() and r["option"]["stats"]["n"] == len(ot)


def test_risk_gate_no_trade_is_skipped(env, monkeypatch):
    b = _bundle(minutes=115)
    st = StateDB(env / "s.sqlite")
    fake = Fake(b, _quotes(b), datetime(2026, 11, 13, 11, 10, 15, tzinfo=IST))
    _patch_stage1(monkeypatch, _cand(spot=b.spot, stop_off=40, t1_off=20))        # reward:risk 0.5 < 1.5
    tr = A.PaperTrader(fake, st)
    tr.step()
    assert tr.position() is None
    e = st.paper_logs()[0]
    assert e["event"] == "SKIP" and "NO TRADE" in e["message"] and e["detail"]["reasons"]
