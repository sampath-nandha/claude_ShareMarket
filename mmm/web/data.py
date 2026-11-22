"""Data access for the web UI: archive bundle (real or clearly-labelled demo), cached analytics, JSON helpers.

Heavy work (reading the archive, building the signal context, option-OI history) is cached per minute, so a
page refresh costs a dictionary lookup; live prices arrive separately over the WebSocket.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from mmm import demo
from mmm.analytics.options.chain import enrich_chain
from mmm.data import parquet_store
from mmm.data.instruments import build_universe, load_instruments
from mmm.data.state_db import StateDB
from mmm.settings import get_config
from mmm.timeutil import IST, now_ist

DEMO_NOTE = ("SYNTHETIC DEMO DATA - nothing has been captured from Zerodha yet. Run `mmm auth login`, `mmm data instruments`, "
             "`mmm data backfill` and start the collector. Numbers shown are generated, not market data.")
DERIVED_NOTE = "IV, Greeks, PCR, max pain, expected move, basis vs fair value and OI-flow labels are DERIVED internally - they are not Zerodha fields."
IST_OFFSET = 19800          # Lightweight Charts renders UTC; shifting by +5:30 shows IST wall-clock times


@dataclass
class Bundle:
    is_demo: bool
    as_of: datetime
    spot: float
    minute_bars: pd.DataFrame
    chain: pd.DataFrame
    expiries: list[date]
    futures_quote: dict[str, Any]
    constituents: pd.DataFrame
    universe_size: int
    vix: float | None = None
    notes: list[str] = field(default_factory=list)
    futures_bars: pd.DataFrame = field(default_factory=pd.DataFrame)
    futures_symbol: str = ""
    index_token: int | None = None
    vix_token: int | None = None
    tokens: dict[str, Any] = field(default_factory=dict)


# --- tiny TTL cache ---------------------------------------------------------------------------------------
_cache: dict[Any, tuple[float, Any]] = {}
_lock = threading.RLock()


def cached(key: Any, ttl: float, fn):
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _lock:
        _cache[key] = (now, val)
    return val


def clear_cache() -> None:
    with _lock:
        _cache.clear()


_state: StateDB | None = None


def state_db() -> StateDB:
    global _state
    if _state is None:
        _state = StateDB()
    return _state


# --- bundle -------------------------------------------------------------------------------------------------
def _token_map(uni) -> dict[str, Any]:
    opts = {}
    if not uni.options.empty:
        for r in uni.options.itertuples():
            opts[int(r.instrument_token)] = [r.tradingsymbol, float(r.strike), r.instrument_type, pd.Timestamp(r.expiry).date().isoformat()]
    cons = {int(r.instrument_token): r.tradingsymbol for r in uni.constituents.itertuples()} if not uni.constituents.empty else {}
    futs = [{"token": int(r.instrument_token), "symbol": r.tradingsymbol} for r in uni.futures.itertuples()] if not uni.futures.empty else []
    return {"options": opts, "constituents": cons, "futures": futs}


def _load_bundle(days: int) -> Bundle:
    cfg = get_config()
    inst = load_instruments()
    state = state_db()
    notes: list[str] = []
    if not inst.empty:
        idx = inst[(inst["exchange"] == cfg.universe.index_exchange) & (inst["tradingsymbol"] == cfg.universe.index_symbol)]
        if not idx.empty:
            token = int(idx.iloc[0]["instrument_token"])
            bars = parquet_store.read_candles("minute", token, start=now_ist().date() - timedelta(days=days))
            if len(bars) > 500:
                quotes = {q["instrument_token"]: q for q in state.quotes()}
                spot_q = quotes.get(token)
                spot = float(spot_q["last_price"]) if spot_q and spot_q.get("last_price") else float(bars["close"].iloc[-1])
                as_of = (pd.Timestamp(spot_q["received_ts"]).to_pydatetime() if spot_q and spot_q.get("received_ts")
                         else (bars["ts"].iloc[-1] + pd.Timedelta(minutes=1)).to_pydatetime())
                uni = build_universe(inst, spot)
                rows = state.chain()
                chain = pd.DataFrame(rows) if rows else pd.DataFrame()
                if not chain.empty:
                    chain = enrich_chain(chain, spot, as_of)
                else:
                    notes.append("no option-chain snapshot in the state DB yet (collector/scheduler not running)")
                fut_q: dict[str, Any] = {}
                fut_bars, fut_sym = pd.DataFrame(), ""
                if not uni.futures.empty:
                    ft = int(uni.futures.iloc[0]["instrument_token"])
                    fut_q, fut_sym = quotes.get(ft, {}), str(uni.futures.iloc[0]["tradingsymbol"])
                    fut_bars = parquet_store.read_candles("minute", ft, start=now_ist().date() - timedelta(days=days))
                cons = pd.DataFrame([quotes[t] for t in uni.constituents["instrument_token"].astype(int) if t in quotes]) if not uni.constituents.empty else pd.DataFrame()
                vix_tok = int(uni.vix["instrument_token"]) if uni.vix is not None else None
                vq = quotes.get(vix_tok) if vix_tok else None
                return Bundle(False, as_of, spot, bars, chain, uni.expiries, fut_q, cons, len(cfg.universe.constituents),
                              float(vq["last_price"]) if vq and vq.get("last_price") else None, notes, fut_bars, fut_sym,
                              token, vix_tok, _token_map(uni))
    bars = demo.demo_minute_bars(days=days)
    as_of = bars["ts"].iloc[-1].to_pydatetime() + timedelta(minutes=1)
    spot = float(bars["close"].iloc[-1])
    e1 = demo.next_weekly_expiry(as_of.date())
    e2 = e1 + timedelta(days=7)
    chain = pd.concat([demo.demo_option_chain(spot, e1, as_of, seed=7), demo.demo_option_chain(spot, e2, as_of, seed=8, base_iv=0.135)])
    chain = enrich_chain(chain, spot, as_of)
    fut = bars.copy()
    for c in ("open", "high", "low", "close"):
        fut[c] = fut[c] + 45.0
    fut["volume"] = 15000
    opts = {}
    if "instrument_token" in chain.columns:
        for r in chain.itertuples():
            opts[int(r.instrument_token)] = [r.tradingsymbol, float(r.strike), r.option_type, str(pd.Timestamp(r.expiry).date())]
    return Bundle(True, as_of, spot, bars, chain, [e1, e2], demo.demo_futures_quote(spot), demo.demo_constituent_quotes(), 50, 13.8,
                  [], fut, "NIFTY-DEMO-FUT", 256265, 264969, {"options": opts, "constituents": {}, "futures": [{"token": 1, "symbol": "NIFTY-DEMO-FUT"}]})


def bundle(days: int = 40) -> Bundle:
    return cached(("bundle", days), 15, lambda: _load_bundle(days))


def context(b: Bundle | None = None, with_aux: bool = True):
    from mmm.signals.engine import build_context

    b = b or bundle()
    key = ("ctx", b.is_demo, b.as_of.replace(second=0, microsecond=0), with_aux)
    return cached(key, 60, lambda: build_context(b.minute_bars, b.as_of, aux=aux(b) if with_aux else None))


def aux(b: Bundle | None = None):
    """Futures parity + option OI history for today and the previous sessions (real archive only)."""
    b = b or bundle()
    if b.is_demo:
        return None

    def _mk():
        from mmm.signals.auxdata import AuxData

        try:
            return AuxData.from_archive(b.minute_bars, start=b.as_of.date() - timedelta(days=7))
        except Exception:  # noqa: BLE001
            return None

    return cached(("aux", b.as_of.replace(second=0, microsecond=0)), 60, _mk)


# --- JSON helpers -------------------------------------------------------------------------------------------
def chart_time(ts: Any) -> int:
    return int(pd.Timestamp(ts).timestamp()) + IST_OFFSET


_EPOCH = pd.Timestamp("1970-01-01", tz="UTC")


def epoch_seconds(ts: pd.Series) -> np.ndarray:
    """Unix seconds for a datetime Series of any resolution (ns, us, ms, s) and any timezone."""
    s = pd.to_datetime(pd.Series(ts), utc=True)
    return ((s - _EPOCH) // pd.Timedelta(seconds=1)).to_numpy(dtype="int64")


def bars_json(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    t = epoch_seconds(df["ts"]) + IST_OFFSET
    o, h, lo, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    v = df["volume"].to_numpy() if "volume" in df else np.zeros(len(df))
    return [{"time": int(t[i]), "open": round(o[i], 2), "high": round(h[i], 2), "low": round(lo[i], 2), "close": round(c[i], 2),
             "volume": int(v[i])} for i in range(len(df))]


def line_json(ts: pd.Series, values: pd.Series | np.ndarray) -> list[dict[str, Any]]:
    t = epoch_seconds(ts) + IST_OFFSET
    vals = np.asarray(values, dtype=float)
    return [{"time": int(t[i]), "value": round(float(vals[i]), 3)} for i in range(len(t)) if not math.isnan(vals[i])]


def clean(x: Any) -> Any:
    """Recursively convert numpy/pandas/dataclass values into JSON-safe Python."""
    if x is None or isinstance(x, (bool, str)):
        return x
    if isinstance(x, (int, np.integer)):
        return int(x)
    if isinstance(x, (float, np.floating)):
        f = float(x)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(x, (pd.Timestamp, datetime)):
        return pd.Timestamp(x).isoformat()
    if isinstance(x, date):
        return x.isoformat()
    if isinstance(x, dict):
        return {str(k): clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, set)):
        return [clean(v) for v in x]
    if isinstance(x, pd.DataFrame):
        return records(x)
    if isinstance(x, np.ndarray):
        return [clean(v) for v in x.tolist()]
    if hasattr(x, "as_dict"):
        return clean(x.as_dict())
    if hasattr(x, "__dataclass_fields__"):
        return clean(x.__dict__)
    if hasattr(x, "model_dump"):
        return clean(x.model_dump(mode="json"))
    if x is pd.NaT:
        return None
    return str(x)


def records(df: pd.DataFrame | None) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    return [clean(r) for r in df.to_dict("records")]


def ensure_ist(ts: Any) -> datetime:
    t = pd.Timestamp(ts)
    return (t.tz_localize(IST) if t.tzinfo is None else t.tz_convert(IST)).to_pydatetime()
