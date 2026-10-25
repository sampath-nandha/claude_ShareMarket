"""SQLite operational store (WAL mode: one writer, many readers across processes).

Holds small, frequently updated state the dashboard reads live: latest quotes, option-chain latest
snapshot, signals, paper/manual trade journal, health heartbeats, audit events. Bulk time-series
live in Parquet (see parquet_store).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from mmm.data.paths import state_db_path
from mmm.timeutil import now_ist

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS latest_quote (
    instrument_token INTEGER PRIMARY KEY,
    tradingsymbol TEXT, exchange TEXT, segment TEXT,
    last_price REAL, last_quantity INTEGER, average_price REAL, volume INTEGER,
    buy_quantity INTEGER, sell_quantity INTEGER,
    open REAL, high REAL, low REAL, close REAL, oi INTEGER, oi_day_high INTEGER, oi_day_low INTEGER,
    bid REAL, bid_qty INTEGER, ask REAL, ask_qty INTEGER, depth_json TEXT,
    exchange_ts TEXT, received_ts TEXT, quality TEXT
);
CREATE TABLE IF NOT EXISTS option_chain_latest (
    tradingsymbol TEXT PRIMARY KEY, instrument_token INTEGER, expiry TEXT, strike REAL, option_type TEXT,
    last_price REAL, bid REAL, ask REAL, volume INTEGER, oi INTEGER, prev_oi INTEGER,
    exchange_ts TEXT, received_ts TEXT, quality TEXT
);
CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, strategy_id TEXT, direction TEXT,
    status TEXT, score REAL, payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_signals_created ON signals(created_at);
CREATE TABLE IF NOT EXISTS journal (
    trade_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, mode TEXT NOT NULL,   -- PAPER | MANUAL
    signal_id TEXT, instrument TEXT, direction TEXT, qty INTEGER,
    entry_price REAL, entry_ts TEXT, exit_price REAL, exit_ts TEXT, exit_reason TEXT,
    stop REAL, target1 REAL, target2 REAL, costs REAL, pnl_gross REAL, pnl_net REAL,
    mfe REAL, mae REAL, notes TEXT, payload_json TEXT
);
CREATE TABLE IF NOT EXISTS heartbeat (
    component TEXT PRIMARY KEY, ts TEXT NOT NULL, status TEXT NOT NULL, detail_json TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, component TEXT, event TEXT, detail_json TEXT
);
CREATE TABLE IF NOT EXISTS dq_incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, instrument_token INTEGER, kind TEXT,
    severity TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS subscription_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, action TEXT, tokens_json TEXT, mode TEXT
);
"""


class StateDB:
    def __init__(self, path: Path | None = None):
        self.path = path or state_db_path()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        self._conn.close()

    # --- kv ----------------------------------------------------------------------------------
    def set_kv(self, key: str, value: Any) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, json.dumps(value, default=str), now_ist().isoformat()),
            )

    def get_kv(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    # --- quotes ------------------------------------------------------------------------------
    def upsert_quotes(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        cols = [
            "instrument_token", "tradingsymbol", "exchange", "segment", "last_price", "last_quantity",
            "average_price", "volume", "buy_quantity", "sell_quantity", "open", "high", "low", "close",
            "oi", "oi_day_high", "oi_day_low", "bid", "bid_qty", "ask", "ask_qty", "depth_json",
            "exchange_ts", "received_ts", "quality",
        ]
        sql = (
            f"INSERT INTO latest_quote({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
            "ON CONFLICT(instrument_token) DO UPDATE SET "
            + ",".join(f"{c}=excluded.{c}" for c in cols if c != "instrument_token")
        )
        with self.tx() as c:
            c.executemany(sql, [tuple(_ser(r.get(k)) for k in cols) for r in rows])

    def quotes(self, tokens: list[int] | None = None) -> list[dict[str, Any]]:
        if tokens:
            q = f"SELECT * FROM latest_quote WHERE instrument_token IN ({','.join('?' * len(tokens))})"
            rows = self._conn.execute(q, tokens).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM latest_quote").fetchall()
        return [dict(r) for r in rows]

    # --- option chain latest -----------------------------------------------------------------
    def upsert_chain(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        cols = [
            "tradingsymbol", "instrument_token", "expiry", "strike", "option_type", "last_price", "bid",
            "ask", "volume", "oi", "prev_oi", "exchange_ts", "received_ts", "quality",
        ]
        sql = (
            f"INSERT INTO option_chain_latest({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
            "ON CONFLICT(tradingsymbol) DO UPDATE SET "
            + ",".join(f"{c}=excluded.{c}" for c in cols if c != "tradingsymbol")
        )
        with self.tx() as c:
            c.executemany(sql, [tuple(_ser(r.get(k)) for k in cols) for r in rows])

    def chain(self, expiry: str | None = None) -> list[dict[str, Any]]:
        if expiry:
            rows = self._conn.execute("SELECT * FROM option_chain_latest WHERE expiry=?", (expiry,)).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM option_chain_latest").fetchall()
        return [dict(r) for r in rows]

    # --- signals -----------------------------------------------------------------------------
    def upsert_signal(self, signal: dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO signals(signal_id,created_at,strategy_id,direction,status,score,payload_json) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(signal_id) DO UPDATE SET status=excluded.status, "
                "score=excluded.score, payload_json=excluded.payload_json",
                (
                    signal["signal_id"], str(signal["created_at"]), signal.get("strategy_id"),
                    signal.get("direction"), signal.get("status"), signal.get("score"),
                    json.dumps(signal, default=str),
                ),
            )

    def signals(self, limit: int = 200, since: datetime | None = None) -> list[dict[str, Any]]:
        if since:
            rows = self._conn.execute(
                "SELECT payload_json FROM signals WHERE created_at>=? ORDER BY created_at DESC LIMIT ?",
                (since.isoformat(), limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT payload_json FROM signals ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(r["payload_json"]) for r in rows]

    # --- journal -----------------------------------------------------------------------------
    def upsert_trade(self, t: dict[str, Any]) -> None:
        cols = [
            "trade_id", "created_at", "mode", "signal_id", "instrument", "direction", "qty", "entry_price",
            "entry_ts", "exit_price", "exit_ts", "exit_reason", "stop", "target1", "target2", "costs",
            "pnl_gross", "pnl_net", "mfe", "mae", "notes", "payload_json",
        ]
        t = dict(t)
        t.setdefault("created_at", now_ist().isoformat())
        t["payload_json"] = json.dumps(t.get("payload", {}), default=str)
        sql = (
            f"INSERT INTO journal({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
            "ON CONFLICT(trade_id) DO UPDATE SET " + ",".join(f"{c}=excluded.{c}" for c in cols if c != "trade_id")
        )
        with self.tx() as c:
            c.execute(sql, tuple(_ser(t.get(k)) for k in cols))

    def trades(self, mode: str | None = None) -> list[dict[str, Any]]:
        if mode:
            rows = self._conn.execute("SELECT * FROM journal WHERE mode=? ORDER BY created_at", (mode,)).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM journal ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    # --- health / audit ----------------------------------------------------------------------
    def heartbeat(self, component: str, status: str, **detail: Any) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO heartbeat(component,ts,status,detail_json) VALUES(?,?,?,?) "
                "ON CONFLICT(component) DO UPDATE SET ts=excluded.ts,status=excluded.status,detail_json=excluded.detail_json",
                (component, now_ist().isoformat(), status, json.dumps(detail, default=str)),
            )

    def heartbeats(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM heartbeat").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json") or "{}")
            out.append(d)
        return out

    def audit(self, component: str, event: str, **detail: Any) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO audit(ts,component,event,detail_json) VALUES(?,?,?,?)",
                (now_ist().isoformat(), component, event, json.dumps(detail, default=str)),
            )

    def recent_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def dq_incident(self, token: int | None, kind: str, severity: str, detail: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO dq_incidents(ts,instrument_token,kind,severity,detail) VALUES(?,?,?,?,?)",
                (now_ist().isoformat(), token, kind, severity, detail),
            )

    def dq_incidents(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM dq_incidents ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def log_subscription(self, action: str, tokens: list[int], mode: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO subscription_log(ts,action,tokens_json,mode) VALUES(?,?,?,?)",
                (now_ist().isoformat(), action, json.dumps(tokens), mode),
            )


def _ser(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    return v
