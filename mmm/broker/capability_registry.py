"""Kite Connect v3 API capability registry (read-only subset used by this system).

Source: https://kite.trade/docs/connect/v3/ . Verify against the live docs before relying on limits;
Zerodha changes rate limits and field availability without notice. Nothing here is invented: each
entry lists only documented fields. Derived fields (Greeks, IV, basis, ...) are computed internally
and are labelled DERIVED wherever shown.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Endpoint:
    name: str
    method: str
    path: str
    auth: bool
    rate_limit: str
    refresh: str
    fields: tuple[str, ...]
    availability: str
    consumers: tuple[str, ...]
    failure_modes: tuple[str, ...] = field(default_factory=tuple)
    notes: str = ""


REGISTRY: dict[str, Endpoint] = {
    "instruments": Endpoint(
        name="instruments",
        method="GET",
        path="/instruments[/:exchange]",
        auth=True,
        rate_limit="Documented as 'once a day' intended usage; large CSV (~gzip)",
        refresh="Daily, before market open",
        fields=(
            "instrument_token", "exchange_token", "tradingsymbol", "name", "last_price", "expiry",
            "strike", "tick_size", "lot_size", "instrument_type", "segment", "exchange",
        ),
        availability="All exchanges/segments",
        consumers=("data.instruments", "universe selection", "options chain reconstruction"),
        failure_modes=("token expired", "network", "schema drift"),
    ),
    "quote": Endpoint(
        name="quote",
        method="GET",
        path="/quote?i=EXCH:SYMBOL",
        auth=True,
        rate_limit="1 req/s (documented); max 500 instruments per call",
        refresh="Snapshot on request",
        fields=(
            "instrument_token", "timestamp", "last_trade_time", "last_price", "last_quantity",
            "buy_quantity", "sell_quantity", "volume", "average_price", "oi", "oi_day_high",
            "oi_day_low", "net_change", "lower_circuit_limit", "upper_circuit_limit",
            "ohlc.open", "ohlc.high", "ohlc.low", "ohlc.close",
            "depth.buy[5].price/quantity/orders", "depth.sell[5].price/quantity/orders",
        ),
        availability="Tradable instruments: full; indices: price/ohlc only",
        consumers=("options chain snapshot", "futures snapshot", "constituent snapshot"),
        failure_modes=("rate limit 429", "token expired", "partial response for bad keys"),
    ),
    "ohlc": Endpoint(
        name="ohlc", method="GET", path="/quote/ohlc", auth=True, rate_limit="1 req/s",
        refresh="Snapshot", fields=("instrument_token", "last_price", "ohlc.*"),
        availability="All", consumers=("breadth snapshot",),
    ),
    "ltp": Endpoint(
        name="ltp", method="GET", path="/quote/ltp", auth=True, rate_limit="1 req/s",
        refresh="Snapshot", fields=("instrument_token", "last_price"),
        availability="All", consumers=("health check",),
    ),
    "historical_data": Endpoint(
        name="historical_data",
        method="GET",
        path="/instruments/historical/:instrument_token/:interval",
        auth=True,
        rate_limit="3 req/s (documented)",
        refresh="Candles finalised after interval close",
        fields=("date", "open", "high", "low", "close", "volume", "oi (if oi=1)"),
        availability=(
            "minute/3/5/10/15/30/60minute/day. Max window per request: minute 60d, 3-10min 100d, "
            "15/30min 200d, 60min 400d, day 2000d. Expired contracts not retrievable indefinitely."
        ),
        consumers=("data.historical backfill", "backtesting", "gap repair"),
        failure_modes=("window too large (400)", "no data for expired token", "rate limit"),
        notes="Requires historical API subscription on the Kite Connect app.",
    ),
    "websocket": Endpoint(
        name="websocket",
        method="WS",
        path="wss://ws.kite.trade",
        auth=True,
        rate_limit="Max 3 connections per API key; up to 3000 instruments per connection",
        refresh="Streaming, ~1 tick/s per instrument when active",
        fields=(
            "instrument_token", "last_price", "last_traded_quantity", "average_traded_price",
            "volume_traded", "total_buy_quantity", "total_sell_quantity", "ohlc.*", "change",
            "last_trade_time", "oi", "oi_day_high", "oi_day_low", "exchange_timestamp",
            "depth.buy[5]", "depth.sell[5]",
        ),
        availability="FULL mode for tradables (184-byte packet). Index packets: ltp/ohlc/change/exchange_timestamp only (no volume/depth/OI)",
        consumers=("data.collector", "candle aggregation", "depth analytics", "live signals"),
        failure_modes=("disconnect", "token expiry at ~6AM next day", "subscription cap"),
    ),
    "profile": Endpoint(
        name="profile", method="GET", path="/user/profile", auth=True, rate_limit="n/a",
        refresh="Static", fields=("user_id", "user_name", "email", "broker", "exchanges", "products"),
        availability="Account", consumers=("health page",),
    ),
    "margins": Endpoint(
        name="margins", method="GET", path="/user/margins[/:segment]", auth=True, rate_limit="n/a",
        refresh="Live", fields=("equity.net", "equity.available.cash", "equity.available.live_balance",
                                "equity.utilised.*", "commodity.*"),
        availability="Account", consumers=("risk page",),
    ),
    "orders": Endpoint(
        name="orders", method="GET", path="/orders", auth=True, rate_limit="n/a",
        refresh="Live", fields=("order_id", "status", "tradingsymbol", "transaction_type", "quantity",
                                "filled_quantity", "average_price", "order_timestamp", "exchange_timestamp"),
        availability="Account (today)", consumers=("manual trade reconciliation",),
        notes="READ ONLY. Order placement endpoints are deliberately absent from this system.",
    ),
    "trades": Endpoint(
        name="trades", method="GET", path="/trades", auth=True, rate_limit="n/a",
        refresh="Live", fields=("trade_id", "order_id", "tradingsymbol", "transaction_type", "quantity",
                                "average_price", "fill_timestamp"),
        availability="Account (today)", consumers=("trade journal", "fill vs signal comparison"),
    ),
    "positions": Endpoint(
        name="positions", method="GET", path="/portfolio/positions", auth=True, rate_limit="n/a",
        refresh="Live", fields=("net[].tradingsymbol", "quantity", "average_price", "last_price",
                                "pnl", "m2m", "unrealised", "realised", "day[]"),
        availability="Account", consumers=("risk page", "position tracking"),
    ),
    "holdings": Endpoint(
        name="holdings", method="GET", path="/portfolio/holdings", auth=True, rate_limit="n/a",
        refresh="Live", fields=("tradingsymbol", "quantity", "average_price", "last_price", "pnl"),
        availability="Account", consumers=("risk page",),
    ),
}

DERIVED_FIELDS: dict[str, str] = {
    "iv": "Black-Scholes implied volatility solved from mid/LTP (internal model, not Zerodha data)",
    "delta/gamma/theta/vega": "Black-Scholes Greeks from derived IV (internal model)",
    "basis": "futures_last - spot_last (computed)",
    "pcr_oi": "sum(put OI)/sum(call OI) over the configured chain window",
    "max_pain": "strike minimising total option-writer payout at expiry (computed)",
    "oi_change": "oi(t) - oi(previous snapshot/close) (computed from captured history)",
    "vwap": "cumulative sum(price*qty)/sum(qty) from captured ticks; approximation from candles labelled",
    "breadth": "equal-weight advancing/declining over configured basket (not official index breadth)",
}
