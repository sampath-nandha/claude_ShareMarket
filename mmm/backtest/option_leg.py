"""Option-execution dimension of a backtest (Part H05): what a long CE/PE bought on each signal would have made.

Uses archived option minute candles when they exist (backfilled from Kite while contracts are active, or
captured live). Contract choice: nearest expiry >= signal date (same-day expiry allowed only if > 2 hours to
close), strike = ATM (direction-agnostic) or one strike ITM. Entry at the next 1-minute open + slippage; exit at
the close of the underlying's exit minute (stop/target/time mapped through time, not through a fixed premium
stop). Costs via the Zerodha NFO model. Trades without option data are reported as NOT EVALUATED, never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pandas as pd

from mmm.data import parquet_store
from mmm.data.instruments import load_instruments
from mmm.risk.costs import round_trip_costs
from mmm.settings import get_config


@dataclass
class OptionLegResult:
    trades: pd.DataFrame
    evaluated: int
    not_evaluated: int
    reasons: dict[str, int]


def _pick_contract(opts: pd.DataFrame, signal_ts: datetime, direction: str, spot: float, step: int, itm_strikes: int) -> pd.Series | None:
    d = signal_ts.date()
    exp = opts[opts["expiry"].dt.date >= d]
    if exp.empty:
        return None
    nearest = exp["expiry"].dt.date.min()
    if nearest == d and signal_ts.hour >= 13:
        later = exp[exp["expiry"].dt.date > d]
        if later.empty:
            return None
        nearest = later["expiry"].dt.date.min()
    kind = "CE" if direction == "BULLISH" else "PE"
    atm = round(spot / step) * step
    strike = atm - itm_strikes * step if kind == "CE" else atm + itm_strikes * step
    row = exp[(exp["expiry"].dt.date == nearest) & (exp["instrument_type"] == kind) & (exp["strike"] == strike)]
    return row.iloc[0] if not row.empty else None


def evaluate_option_leg(trades: pd.DataFrame, itm_strikes: int = 0, lots: int = 1) -> OptionLegResult:
    cfg = get_config()
    step, lot = cfg.universe.strike_step, cfg.risk.nifty_lot_size
    reasons: dict[str, int] = {}
    rows = []
    if trades.empty:
        return OptionLegResult(pd.DataFrame(), 0, 0, {})
    inst_cache: dict[date, pd.DataFrame] = {}
    candle_cache: dict[int, pd.DataFrame] = {}
    for t in trades.itertuples():
        sig_ts = pd.Timestamp(t.signal_ts).to_pydatetime()
        d = sig_ts.date()
        if d not in inst_cache:
            inst = load_instruments(d)
            inst_cache[d] = inst[(inst["exchange"] == cfg.universe.fno_exchange) & (inst["name"] == cfg.universe.fno_name) & inst["instrument_type"].isin(["CE", "PE"])] if not inst.empty else inst
        opts = inst_cache[d]
        if opts.empty:
            reasons["no instrument snapshot for date"] = reasons.get("no instrument snapshot for date", 0) + 1
            continue
        c = _pick_contract(opts, sig_ts, t.direction, float(t.entry), step, itm_strikes)
        if c is None:
            reasons["contract not in snapshot"] = reasons.get("contract not in snapshot", 0) + 1
            continue
        tok = int(c["instrument_token"])
        if tok not in candle_cache:
            candle_cache[tok] = parquet_store.read_candles("minute", tok)
        oc = candle_cache[tok]
        if oc.empty:
            reasons["no option candles archived"] = reasons.get("no option candles archived", 0) + 1
            continue
        e_ts, x_ts = pd.Timestamp(t.entry_ts), pd.Timestamp(t.exit_ts)
        e_bar = oc[oc["ts"] >= e_ts].head(1)
        x_bar = oc[oc["ts"] <= x_ts].tail(1)
        if e_bar.empty or x_bar.empty or x_bar["ts"].iloc[0] < e_ts - timedelta(minutes=1):
            reasons["option candles missing at entry/exit"] = reasons.get("option candles missing at entry/exit", 0) + 1
            continue
        slip = cfg.costs.slippage_ticks * cfg.costs.tick_size
        buy = float(e_bar["open"].iloc[0]) + slip
        sell = max(float(x_bar["close"].iloc[0]) - slip, 0.05)
        qty = lots * lot
        gross = (sell - buy) * qty
        costs = round_trip_costs(buy, sell, qty, include_slippage=False).total
        rows.append({"signal_ts": t.signal_ts, "strategy_id": t.strategy_id, "direction": t.direction, "tradingsymbol": c["tradingsymbol"], "strike": c["strike"],
                     "expiry": c["expiry"], "buy": buy, "sell": sell, "qty": qty, "pnl_gross": gross, "costs": costs, "pnl_net": gross - costs,
                     "underlying_exit": t.exit_reason, "underlying_pnl_net": t.pnl_net, "session": t.session, "regime": t.regime})
    df = pd.DataFrame(rows)
    return OptionLegResult(df, len(df), len(trades) - len(df), reasons)
