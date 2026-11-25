"""Web UI: every REST route on demo data, POST workflows, WebSocket push, tick bus, static assets, read-only surface."""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

STATIC = Path(__file__).resolve().parents[1] / "mmm" / "web" / "static"
GETS = ["/api/status", "/api/version", "/api/overview", "/api/overview?tf=minute&instrument=overlay&sessions=2", "/api/overview?instrument=fut&tf=15minute",
        "/api/premarket", "/api/priceaction", "/api/futures", "/api/futures?direction=BEARISH", "/api/breadth", "/api/options", "/api/options/theta",
        "/api/signals", "/api/risk", "/api/account", "/api/backtest/options", "/api/research", "/api/replay/sessions", "/api/health", "/api/coverage/detail",
        "/api/patterns/watch", "/api/condition", "/api/condition?horizon=60minute", "/api/condition?horizon=day&with_path=false", "/api/paper"]


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    import os

    from mmm import settings
    from mmm.web import data

    old = os.environ.get("MMM_DATA_DIR")
    os.environ["MMM_DATA_DIR"] = str(tmp_path_factory.mktemp("web"))
    settings.get_secrets.cache_clear()
    data.clear_cache()
    data._state = None
    from mmm.web.app import create_app

    with TestClient(create_app(live=True)) as c:
        yield c
    if old is None:
        os.environ.pop("MMM_DATA_DIR", None)
    else:
        os.environ["MMM_DATA_DIR"] = old
    settings.get_secrets.cache_clear()
    data.clear_cache()
    data._state = None


@pytest.mark.parametrize("path", GETS)
def test_get_routes(client, path):
    r = client.get(path)
    assert r.status_code == 200, r.text[:500]
    json.dumps(r.json())          # strictly JSON-serialisable (no NaN/Infinity)


def test_status_is_demo_and_labelled(client):
    s = client.get("/api/status").json()
    assert s["demo"] is True and "SYNTHETIC" in s["demo_note"] and s["tokens"]["index"]


def test_overview_bar_times_are_ist_epoch_seconds(client):
    bars = client.get("/api/overview?tf=5minute").json()["bars"]
    import datetime as _dt

    t = _dt.datetime.fromtimestamp(bars[-1]["time"], _dt.UTC)          # shifted by +05:30, so this reads as IST wall time
    assert t.year >= 2020 and (9, 15) <= (t.hour, t.minute) <= (15, 30)


def test_option_workflows(client):
    rank = client.post("/api/options/rank", json={"direction": "BEARISH"}).json()
    assert "candidates" in rank
    pay = client.post("/api/options/payoff", json={"preset": "Iron condor"}).json()
    assert len(pay["legs"]) == 4 and len(pay["grid"]["spot"]) == len(pay["grid"]["expiry"]) and pay["breakevens"]
    legs = client.post("/api/options/payoff", json={"legs": [{"kind": "CE", "strike": pay["atm"], "qty": -1}]}).json()
    assert legs["max_loss"] == "unlimited"
    assert client.post("/api/options/payoff", json={"preset": "nope"}).status_code == 400


def test_journal_roundtrip(client):
    tid = client.post("/api/journal/open", json={"mode": "PAPER", "instrument": "NIFTY TEST CE", "qty": 75, "price": 100.0}).json()["trade_id"]
    assert tid in client.get("/api/risk?mode=PAPER").json()["open"]
    closed = client.post("/api/journal/close", json={"trade_id": tid, "price": 110.0, "reason": "TARGET1"}).json()
    assert closed["exit_price"] == 110.0
    assert client.post("/api/journal/open", json={"mode": "LIVE", "instrument": "X", "price": 1}).status_code == 400


def test_backtest_walkforward_calibration_optionleg(client):
    assert client.post("/api/backtest/calibration", json={"source": "backtest"}).status_code in (409, 200)
    bt = client.post("/api/backtest", json={"strategies": ["PA01", "PA02", "PA03"]}).json()
    assert bt["sessions"] > 0 and "notes" in bt
    wf = client.post("/api/backtest/walkforward", json={"strategies": ["PA02"], "train_days": 10, "test_days": 5, "holdout_days": 5}).json()
    assert "folds" in wf
    cal = client.post("/api/backtest/calibration", json={"source": "backtest"})
    assert cal.status_code in (200, 409)
    assert client.post("/api/backtest/calibration", json={"source": "backtest", "save": True}).status_code in (400, 409)   # never saved from demo / in-sample
    ol = client.post("/api/backtest/optionleg", json={"itm_strikes": 0})
    assert ol.status_code in (200, 409)


def test_replay(client):
    day = client.get("/api/replay/sessions").json()["sessions"][0]["date"]
    r = client.get(f"/api/replay?day={day}&i=120").json()
    assert r["i"] == 120 and len(r["bars"]) == 120 and "candidates" in r


def test_websocket_hello_and_demo_ticks(client):
    with client.websocket_connect("/ws") as ws:
        hello = json.loads(ws.receive_text())
        assert hello["type"] == "hello"
        deadline = time.time() + 10
        got = None
        while time.time() < deadline:
            m = json.loads(ws.receive_text())
            if m["type"] == "ticks":
                got = m
                break
        assert got is not None and got["src"] == "demo" and got["d"][0]["p"] > 0 and "srv" in got
        ws.send_text("ping")


def test_tickbus_roundtrip():
    import zmq

    from mmm.data.tickbus import TickPublisher, compact

    addr = "tcp://127.0.0.1:5599"
    pub = TickPublisher(addr)
    sub = zmq.Context.instance().socket(zmq.SUB)
    sub.setsockopt(zmq.SUBSCRIBE, b"")
    sub.setsockopt(zmq.RCVTIMEO, 3000)
    sub.connect(addr)
    from datetime import datetime

    from mmm.timeutil import IST

    row = {"instrument_token": 256265, "last_price": 25000.5, "exchange_timestamp": datetime(2026, 11, 20, 10, 0, tzinfo=IST), "received_ts": datetime.now(IST)}
    msg = None
    for _ in range(20):                # PUB/SUB slow joiner: retry until the subscriber is connected
        pub.publish([row])
        try:
            msg = json.loads(sub.recv())
            break
        except zmq.Again:
            continue
    pub.close()
    sub.close(0)
    assert msg is not None and msg["ticks"][0]["t"] == 256265 and msg["ticks"][0]["p"] == 25000.5
    assert compact(row)["x"] == int(row["exchange_timestamp"].timestamp() * 1000)


def test_static_assets_and_pages_exist(client):
    html = client.get("/").text
    for asset in ("lightweight-charts.standalone.production.js", "ag-grid-community.min.noStyle.js", "ag-grid.min.css", "js/app.js"):
        assert asset in html
        assert client.get(f"/static/{'vendor/' if 'js/app' not in asset else ''}{asset}").status_code == 200
    app_js = (STATIC / "js" / "app.js").read_text()
    for page in ("overview", "condition", "premarket", "priceaction", "futures", "options", "breadth", "signals", "paper", "risk", "backtest", "research", "replay", "health"):
        assert f'"{page}"' in app_js and (STATIC / "js" / "pages" / f"{page}.js").exists()
    assert "http" not in (STATIC / "vendor" / "ag-theme-quartz-no-font.min.css").read_text()   # no external font fetch


def test_no_order_routes(client):
    paths = set(client.app.openapi()["paths"]) | {getattr(r, "path", "") for r in client.app.routes}
    assert not any(k in p for p in paths for k in ("order", "place", "modify", "cancel", "gtt"))


def test_paper_pause_and_backtest(client):
    assert client.post("/api/paper/pause", json={"paused": True}).json()["paused"] is True
    d = client.get("/api/paper").json()
    assert d["paused"] is True and d["config"]["capital"] == 10000
    client.post("/api/paper/pause", json={"paused": False})
    assert client.post("/api/paper/backtest", json={"days": 30}).json()["started"] in (True, False)
    deadline = time.time() + 600
    while time.time() < deadline:
        st = client.get("/api/paper?log_limit=1").json()["backtest_status"]
        if st and st["state"] != "RUNNING":
            break
        time.sleep(2)
    d = client.get("/api/paper").json()
    assert d["backtest_status"]["state"] == "DONE" and d["backtest"]["demo"] is True and "direction" in d["backtest"]
