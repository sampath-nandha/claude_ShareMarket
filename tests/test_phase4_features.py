"""Phase 4: feature study (OOS, holdout, gates) and score gating."""

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from mmm.data.state_db import StateDB
from mmm.demo import demo_minute_bars
from mmm.research import features as F
from mmm.signals.engine import Confirmations, FrameCache, build_context, stage2
from mmm.signals.model import SetupCandidate
from mmm.strategies import load_strategies
from mmm.timeutil import IST


@pytest.fixture()
def archive(tmp_path, monkeypatch):
    monkeypatch.setenv("MMM_DATA_DIR", str(tmp_path))
    from mmm import settings

    settings.get_secrets.cache_clear()
    yield tmp_path
    settings.get_secrets.cache_clear()


def _events(feature: str, sessions: list[date], hits: list[bool], move: float = 6.0) -> pd.DataFrame:
    return pd.DataFrame([{"feature": feature, "session": d, "ts": None, "direction": 1, "move": move if h else -move,
                          "net": (move if h else -move) - F.COST_POINTS, "hit": h} for d, h in zip(sessions, hits)])


def test_evaluate_pass_fail_insufficient():
    sessions = [date(2024, 1, 1) + timedelta(days=i) for i in range(400)]
    rng = np.random.default_rng(1)
    strong = _events("open_drive", sessions, list(rng.random(400) < 0.70))
    coin = _events("gap_fade", sessions, list(rng.random(400) < 0.50))
    few = _events("narrow_cpr_trend", sessions[:150], [True] * 150)
    res = F.evaluate(pd.concat([strong, coin, few], ignore_index=True), sessions, train_days=120, min_events=100)
    assert res["open_drive"]["status"] == "PASS" and res["open_drive"]["oos"]["n"] == 280
    assert res["gap_fade"]["status"] == "FAIL"
    assert res["narrow_cpr_trend"]["status"] == "INSUFFICIENT"            # only 30 events after the train window
    assert res["basis_divergence"]["status"] == "INSUFFICIENT" and res["basis_divergence"]["oos"]["n"] == 0


def test_forward_move_uses_next_open_and_horizon_close():
    t0 = datetime(2026, 11, 13, 9, 15, tzinfo=IST)
    m = pd.DataFrame({"ts": [t0 + timedelta(minutes=i) for i in range(120)], "open": np.arange(120.0), "close": np.arange(120.0) + 0.5})
    ts_ns = m["ts"].dt.tz_convert(None).to_numpy()
    as_of = t0 + timedelta(minutes=15)
    assert F._forward_move(m, ts_ns, as_of, 30, 1) == pytest.approx((44 + 0.5) - 15)
    assert F._forward_move(m, ts_ns, as_of, 0, -1) == pytest.approx(-((119 + 0.5) - 15))


def _drive_ctx():
    hist = demo_minute_bars(days=25, seed=9, end=date(2026, 11, 12))
    pc = float(hist["close"].iloc[-1])
    path = list(pc + 20 + np.linspace(0, 60, 15)) + list(pc + 80 + np.linspace(0, 5, 360))
    t0 = datetime(2026, 11, 13, 9, 15, tzinfo=IST)
    prev = path[0]
    rows = []
    for i, c in enumerate(path):
        rows.append({"ts": t0 + timedelta(minutes=i), "open": prev, "high": max(prev, c) + 1, "low": min(prev, c) - 1, "close": c, "volume": 0, "oi": 0})
        prev = c
    bars = pd.concat([hist, pd.DataFrame(rows)], ignore_index=True)
    return build_context(None, t0 + timedelta(minutes=15), cache=FrameCache(bars, "5minute", "15minute", "60minute"))


def test_adjustments_only_count_passing_features():
    ctx = _drive_ctx()
    assert F.r_open_drive(ctx) == 1
    none = F.adjustments(ctx, 1, {})
    assert none["pa_delta"] == 0 and any("open_drive" in s for s in none["ignored"])
    reg = {"open_drive": {"status": "PASS"}}
    up = F.adjustments(ctx, 1, reg)
    down = F.adjustments(ctx, -1, reg)
    assert up["pa_delta"] == 5 and up["evidence"] and down["pa_delta"] == -5 and down["contradictions"]


def test_stage2_applies_the_gate(archive):
    ctx = _drive_ctx()
    strat = load_strategies(["PA11"], apply_registry=False)[0]
    cand = SetupCandidate(strategy_id="PA11", direction="BULLISH", timeframe="5minute", trigger_event="X", entry=ctx.close,
                          stop=ctx.close - 20, target1=ctx.close + 40, target2=ctx.close + 60, invalidation="x", pa_score=60,
                          bar_ts=ctx.last_bar["ts"].to_pydatetime())
    plain = stage2(ctx, strat, cand, Confirmations(), feature_registry={})
    gated = stage2(ctx, strat, cand, Confirmations(), feature_registry={"open_drive": {"status": "PASS"}})
    assert gated.score > plain.score
    assert any("validated feature open_drive" in e for e in gated.evidence)
    assert any("unvalidated features" in e for e in plain.evidence)


def test_run_study_writes_report_and_registry(archive):
    bars = demo_minute_bars(days=40, seed=4, end=date(2026, 11, 13))
    state = StateDB(archive / "s.sqlite")
    r = F.run_study(bars, None, train_days=10, holdout_days=5, state=state, out_dir=archive / "reports", min_events=5)
    assert r["holdout_sessions"] == 5 and r["sessions"] == 40 - 10 - 5
    assert set(r["features"]) == set(F.BY_KEY)
    assert F.statuses(state)["open_drive"]["status"] in ("PASS", "FAIL", "INSUFFICIENT")
    assert all(p.endswith((".json", ".md")) for p in r["paths"])
    assert r["features"]["basis_divergence"]["oos"]["n"] == 0           # no futures history supplied
