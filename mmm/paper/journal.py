"""Paper and manual trade journal on top of the state DB, with cost-aware P&L and MFE/MAE tracking."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import pandas as pd

from mmm.data.state_db import StateDB
from mmm.risk.costs import round_trip_costs
from mmm.timeutil import now_ist


class Journal:
    def __init__(self, state: StateDB | None = None):
        self.state = state or StateDB()

    def open_trade(
        self,
        mode: str,
        instrument: str,
        direction: str,
        qty: int,
        entry_price: float,
        stop: float | None = None,
        target1: float | None = None,
        target2: float | None = None,
        signal_id: str | None = None,
        entry_ts: datetime | None = None,
        notes: str = "",
        payload: dict[str, Any] | None = None,
    ) -> str:
        assert mode in ("PAPER", "MANUAL")
        tid = uuid.uuid4().hex[:10]
        self.state.upsert_trade(
            {
                "trade_id": tid, "mode": mode, "signal_id": signal_id, "instrument": instrument, "direction": direction,
                "qty": qty, "entry_price": entry_price, "entry_ts": (entry_ts or now_ist()).isoformat(), "stop": stop,
                "target1": target1, "target2": target2, "mfe": 0.0, "mae": 0.0, "notes": notes, "payload": payload or {},
            }
        )
        self.state.audit("journal", "open_trade", trade_id=tid, mode=mode, instrument=instrument)
        return tid

    def mark(self, trade_id: str, last_price: float) -> None:
        """Update MFE/MAE for an open trade (premium-based for options)."""
        t = self._get(trade_id)
        if t is None or t.get("exit_price") is not None:
            return
        sign = 1 if t["direction"] in ("BUY", "LONG", "BULLISH") else -1
        move = sign * (last_price - t["entry_price"])
        t["mfe"] = max(t.get("mfe") or 0.0, move)
        t["mae"] = min(t.get("mae") or 0.0, move)
        self.state.upsert_trade(self._clean(t))

    def close_trade(self, trade_id: str, exit_price: float, exit_reason: str, exit_ts: datetime | None = None) -> dict[str, Any]:
        t = self._get(trade_id)
        if t is None:
            raise KeyError(trade_id)
        sign = 1 if t["direction"] in ("BUY", "LONG", "BULLISH") else -1
        qty = int(t["qty"])
        gross = sign * (exit_price - t["entry_price"]) * qty
        buy_p, sell_p = (t["entry_price"], exit_price) if sign > 0 else (exit_price, t["entry_price"])
        costs = round_trip_costs(buy_p, sell_p, qty).total
        t.update(
            exit_price=exit_price, exit_ts=(exit_ts or now_ist()).isoformat(), exit_reason=exit_reason, costs=costs,
            pnl_gross=gross, pnl_net=gross - costs,
        )
        self.state.upsert_trade(self._clean(t))
        self.state.audit("journal", "close_trade", trade_id=trade_id, pnl_net=gross - costs, reason=exit_reason)
        return t

    def open_trades(self, mode: str | None = None) -> list[dict[str, Any]]:
        return [t for t in self.state.trades(mode) if t.get("exit_price") is None]

    def frame(self, mode: str | None = None) -> pd.DataFrame:
        rows = self.state.trades(mode)
        return pd.DataFrame(rows) if rows else pd.DataFrame()

    def summary(self, mode: str | None = None) -> dict[str, Any]:
        df = self.frame(mode)
        closed = df[df["exit_price"].notna()] if not df.empty else df
        if closed.empty:
            return {"closed": 0, "open": len(df), "win_rate": None, "net_pnl": 0.0}
        wins = (closed["pnl_net"] > 0).sum()
        return {
            "closed": int(len(closed)), "open": int(len(df) - len(closed)), "wins": int(wins),
            "losses": int((closed["pnl_net"] < 0).sum()), "breakeven": int((closed["pnl_net"] == 0).sum()),
            "win_rate": float(wins / len(closed)), "net_pnl": float(closed["pnl_net"].sum()),
            "gross_pnl": float(closed["pnl_gross"].sum()), "costs": float(closed["costs"].sum()),
            "avg_win": float(closed.loc[closed["pnl_net"] > 0, "pnl_net"].mean()) if wins else 0.0,
            "avg_loss": float(closed.loc[closed["pnl_net"] < 0, "pnl_net"].mean()) if (closed["pnl_net"] < 0).any() else 0.0,
        }

    def _get(self, trade_id: str) -> dict[str, Any] | None:
        for t in self.state.trades():
            if t["trade_id"] == trade_id:
                return t
        return None

    @staticmethod
    def _clean(t: dict[str, Any]) -> dict[str, Any]:
        t = dict(t)
        pj = t.pop("payload_json", None)
        if "payload" not in t and pj:
            import json

            t["payload"] = json.loads(pj)
        return t
