from datetime import datetime

from mmm.data.state_db import StateDB
from mmm.paper.journal import Journal
from mmm.timeutil import IST


def test_state_db_roundtrip(tmp_path):
    db = StateDB(tmp_path / "s.sqlite")
    db.set_kv("spot", 24000.5)
    assert db.get_kv("spot") == 24000.5
    db.upsert_quotes([{"instrument_token": 1, "tradingsymbol": "X", "last_price": 10.0, "quality": "VALID"}])
    db.upsert_quotes([{"instrument_token": 1, "tradingsymbol": "X", "last_price": 11.0, "quality": "VALID"}])
    q = db.quotes([1])
    assert len(q) == 1 and q[0]["last_price"] == 11.0
    db.upsert_signal({"signal_id": "abc", "created_at": datetime(2026, 1, 5, tzinfo=IST), "strategy_id": "PA01",
                      "direction": "BULLISH", "status": "WATCH", "score": 50})
    assert db.signals()[0]["signal_id"] == "abc"
    db.heartbeat("collector", "UP", ticks=5)
    hb = db.heartbeats()
    assert hb[0]["status"] == "UP" and hb[0]["detail"]["ticks"] == 5
    db.audit("t", "e", x=1)
    assert db.recent_audit(1)[0]["event"] == "e"
    db.close()


def test_journal_pnl_with_costs(tmp_path):
    j = Journal(StateDB(tmp_path / "j.sqlite"))
    tid = j.open_trade("PAPER", "NIFTY24000CE", "BUY", 75, 150.0, stop=120.0, target1=200.0)
    j.mark(tid, 180.0)
    j.mark(tid, 140.0)
    t = j.close_trade(tid, 190.0, "TARGET1")
    assert t["pnl_gross"] == 40.0 * 75
    assert t["costs"] > 0 and t["pnl_net"] < t["pnl_gross"]
    assert t["mfe"] == 30.0 and t["mae"] == -10.0
    s = j.summary("PAPER")
    assert s["closed"] == 1 and s["win_rate"] == 1.0
