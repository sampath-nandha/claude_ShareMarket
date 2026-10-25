"""Tick normalisation (KiteTicker packets -> flat rows) and 1-minute candle aggregation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from mmm.timeutil import IST, now_ist, to_ist

TICK_COLS = [
    "instrument_token", "tradable", "mode", "last_price", "last_traded_quantity", "average_traded_price",
    "volume_traded", "total_buy_quantity", "total_sell_quantity", "open", "high", "low", "close", "change",
    "last_trade_time", "oi", "oi_day_high", "oi_day_low", "exchange_timestamp", "received_ts",
    "bid1", "bid1_qty", "bid1_orders", "ask1", "ask1_qty", "ask1_orders",
]

DEPTH_COLS = ["instrument_token", "exchange_timestamp", "received_ts", "side", "level", "price", "quantity", "orders"]


def normalise_tick(t: dict[str, Any], received: datetime | None = None) -> dict[str, Any]:
    """Flatten a KiteTicker tick. Index packets lack volume/depth/OI - those stay None, never fabricated."""
    received = received or now_ist()
    ohlc = t.get("ohlc") or {}
    depth = t.get("depth") or {}
    buy = depth.get("buy") or []
    sell = depth.get("sell") or []
    ex_ts = t.get("exchange_timestamp")
    ltt = t.get("last_trade_time")
    row: dict[str, Any] = {
        "instrument_token": int(t["instrument_token"]),
        "tradable": bool(t.get("tradable", True)),
        "mode": t.get("mode"),
        "last_price": t.get("last_price"),
        "last_traded_quantity": t.get("last_traded_quantity"),
        "average_traded_price": t.get("average_traded_price"),
        "volume_traded": t.get("volume_traded"),
        "total_buy_quantity": t.get("total_buy_quantity"),
        "total_sell_quantity": t.get("total_sell_quantity"),
        "open": ohlc.get("open"),
        "high": ohlc.get("high"),
        "low": ohlc.get("low"),
        "close": ohlc.get("close"),
        "change": t.get("change"),
        "last_trade_time": to_ist(ltt) if isinstance(ltt, datetime) else None,
        "oi": t.get("oi"),
        "oi_day_high": t.get("oi_day_high"),
        "oi_day_low": t.get("oi_day_low"),
        "exchange_timestamp": to_ist(ex_ts) if isinstance(ex_ts, datetime) else None,
        "received_ts": received,
        "bid1": buy[0]["price"] if buy else None,
        "bid1_qty": buy[0]["quantity"] if buy else None,
        "bid1_orders": buy[0]["orders"] if buy else None,
        "ask1": sell[0]["price"] if sell else None,
        "ask1_qty": sell[0]["quantity"] if sell else None,
        "ask1_orders": sell[0]["orders"] if sell else None,
    }
    return row


def depth_rows(t: dict[str, Any], received: datetime | None = None) -> list[dict[str, Any]]:
    received = received or now_ist()
    depth = t.get("depth") or {}
    ex_ts = t.get("exchange_timestamp")
    ex_ts = to_ist(ex_ts) if isinstance(ex_ts, datetime) else None
    out = []
    for side in ("buy", "sell"):
        for lvl, d in enumerate(depth.get(side) or [], start=1):
            out.append(
                {
                    "instrument_token": int(t["instrument_token"]),
                    "exchange_timestamp": ex_ts,
                    "received_ts": received,
                    "side": side,
                    "level": lvl,
                    "price": d.get("price"),
                    "quantity": d.get("quantity"),
                    "orders": d.get("orders"),
                }
            )
    return out


def quote_row_from_tick(row: dict[str, Any], meta: dict[str, Any] | None, quality: str) -> dict[str, Any]:
    meta = meta or {}
    return {
        "instrument_token": row["instrument_token"],
        "tradingsymbol": meta.get("tradingsymbol"),
        "exchange": meta.get("exchange"),
        "segment": meta.get("segment"),
        "last_price": row["last_price"],
        "last_quantity": row["last_traded_quantity"],
        "average_price": row["average_traded_price"],
        "volume": row["volume_traded"],
        "buy_quantity": row["total_buy_quantity"],
        "sell_quantity": row["total_sell_quantity"],
        "open": row["open"],
        "high": row["high"],
        "low": row["low"],
        "close": row["close"],
        "oi": row["oi"],
        "oi_day_high": row["oi_day_high"],
        "oi_day_low": row["oi_day_low"],
        "bid": row["bid1"],
        "bid_qty": row["bid1_qty"],
        "ask": row["ask1"],
        "ask_qty": row["ask1_qty"],
        "depth_json": None,
        "exchange_ts": row["exchange_timestamp"],
        "received_ts": row["received_ts"],
        "quality": quality,
    }


@dataclass
class _Bar:
    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume_start: int | None  # cumulative volume at first tick of the bar
    volume_end: int | None
    oi: int | None
    n_ticks: int = 1


@dataclass
class CandleAggregator:
    """Builds 1-minute OHLCV bars from ticks. Volume is derived from cumulative `volume_traded` deltas.

    A bar is only emitted after a tick arrives in a later minute (or on explicit flush), so an
    incomplete bar never leaks into feature calculations.
    """

    bars: dict[int, _Bar] = field(default_factory=dict)
    last_cum_volume: dict[int, int] = field(default_factory=dict)

    @staticmethod
    def _minute(ts: datetime) -> datetime:
        return ts.replace(second=0, microsecond=0)

    def on_tick(self, row: dict[str, Any]) -> dict[str, Any] | None:
        """Feed a normalised tick; returns a completed bar dict when a minute rolls over."""
        tok = row["instrument_token"]
        price = row.get("last_price")
        ts = row.get("exchange_timestamp") or row.get("received_ts")
        if price is None or ts is None:
            return None
        minute = self._minute(ts)
        cum_vol = row.get("volume_traded")
        bar = self.bars.get(tok)
        completed = None
        if bar is None or minute > bar.start:
            if bar is not None:
                completed = self._finish(tok, bar)
            self.bars[tok] = _Bar(
                start=minute, open=price, high=price, low=price, close=price,
                volume_start=self.last_cum_volume.get(tok, cum_vol), volume_end=cum_vol, oi=row.get("oi"),
            )
        elif minute == bar.start:
            bar.high = max(bar.high, price)
            bar.low = min(bar.low, price)
            bar.close = price
            bar.volume_end = cum_vol if cum_vol is not None else bar.volume_end
            bar.oi = row.get("oi") if row.get("oi") is not None else bar.oi
            bar.n_ticks += 1
        # ticks older than the current bar are ignored (out-of-order) - counted by caller via DQ
        if cum_vol is not None:
            self.last_cum_volume[tok] = cum_vol
        return completed

    def _finish(self, tok: int, bar: _Bar) -> dict[str, Any]:
        vol = None
        if bar.volume_end is not None and bar.volume_start is not None:
            vol = max(bar.volume_end - bar.volume_start, 0)
        return {
            "instrument_token": tok, "ts": bar.start, "open": bar.open, "high": bar.high, "low": bar.low,
            "close": bar.close, "volume": vol if vol is not None else 0, "oi": bar.oi or 0, "n_ticks": bar.n_ticks,
        }

    def flush(self, before: datetime | None = None) -> list[dict[str, Any]]:
        """Emit bars whose minute has ended (before `before`, default now). Used at session close / shutdown."""
        before = before or now_ist()
        out = []
        for tok, bar in list(self.bars.items()):
            if bar.start + timedelta(minutes=1) <= before:
                out.append(self._finish(tok, bar))
                del self.bars[tok]
        return out


def ticks_to_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=TICK_COLS)
    for c in ("exchange_timestamp", "received_ts", "last_trade_time"):
        df[c] = pd.to_datetime(df[c], errors="coerce")
        if df[c].dt.tz is None:
            df[c] = df[c].dt.tz_localize(IST)
    return df


def depth_to_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=DEPTH_COLS)
    for c in ("exchange_timestamp", "received_ts"):
        df[c] = pd.to_datetime(df[c], errors="coerce")
        if df[c].dt.tz is None:
            df[c] = df[c].dt.tz_localize(IST)
    return df


def depth_json(t: dict[str, Any]) -> str | None:
    d = t.get("depth")
    return json.dumps(d) if d else None
