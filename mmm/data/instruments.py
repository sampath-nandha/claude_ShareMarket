"""Instrument master snapshots and NIFTY universe selection.

Instrument tokens can be recycled by the exchange, so the permanent internal id is
`exchange:tradingsymbol:expiry:strike:type` (see `permanent_id`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd
import structlog

from mmm.data import paths
from mmm.settings import get_config
from mmm.timeutil import now_ist

log = structlog.get_logger(__name__)

INSTRUMENT_COLS = [
    "instrument_token", "exchange_token", "tradingsymbol", "name", "last_price", "expiry", "strike",
    "tick_size", "lot_size", "instrument_type", "segment", "exchange",
]


def permanent_id(row: pd.Series | dict[str, Any]) -> str:
    exp = row.get("expiry")
    exp_s = "" if exp in (None, "", pd.NaT) or pd.isna(exp) else pd.Timestamp(exp).date().isoformat()
    strike = row.get("strike") or 0
    return f"{row['exchange']}:{row['tradingsymbol']}:{exp_s}:{float(strike):g}:{row.get('instrument_type', '')}"


def snapshot_instruments(client, d: date | None = None) -> pd.DataFrame:
    """Download the full instrument master and archive it as today's snapshot."""
    d = d or now_ist().date()
    rows = client.instruments()
    df = pd.DataFrame(rows)
    for c in INSTRUMENT_COLS:
        if c not in df.columns:
            df[c] = None
    df = df[INSTRUMENT_COLS].copy()
    df["expiry"] = pd.to_datetime(df["expiry"], errors="coerce")
    df["permanent_id"] = df.apply(permanent_id, axis=1)
    df["snapshot_date"] = pd.Timestamp(d)
    out = paths.instruments_dir(d) / "instruments.parquet"
    df.to_parquet(out, index=False, compression="zstd")
    log.info("instruments_snapshot", rows=len(df), path=str(out))
    return df


def load_instruments(d: date | None = None) -> pd.DataFrame:
    """Load the latest snapshot on/before d. Empty frame if none exists."""
    base = paths.data_dir() / "instruments"
    if not base.exists():
        return pd.DataFrame(columns=INSTRUMENT_COLS)
    cands = sorted(base.glob("date=*/instruments.parquet"))
    if d is not None:
        cands = [p for p in cands if p.parent.name <= f"date={d.isoformat()}"]
    if not cands:
        return pd.DataFrame(columns=INSTRUMENT_COLS)
    df = pd.read_parquet(cands[-1])
    df["expiry"] = pd.to_datetime(df["expiry"], errors="coerce")
    return df


@dataclass
class Universe:
    index: pd.Series | None
    vix: pd.Series | None
    futures: pd.DataFrame
    options: pd.DataFrame
    constituents: pd.DataFrame
    expiries: list[date]
    atm_strike: float | None

    def tokens_by_priority(self) -> list[tuple[int, list[int]]]:
        """[(priority, tokens)] - P1 index/fut, P2 near-ATM options, P3 constituents, P4 wide chain."""
        step = get_config().universe.strike_step
        out: list[tuple[int, list[int]]] = []
        p1 = [int(self.index["instrument_token"])] if self.index is not None else []
        if self.vix is not None:
            p1.append(int(self.vix["instrument_token"]))
        p1 += self.futures["instrument_token"].astype(int).tolist()
        out.append((1, p1))
        if not self.options.empty and self.atm_strike is not None:
            near = self.options[(self.options["strike"] - self.atm_strike).abs() <= 4 * step]
            far = self.options[(self.options["strike"] - self.atm_strike).abs() > 4 * step]
            out.append((2, near["instrument_token"].astype(int).tolist()))
            out.append((3, self.constituents["instrument_token"].astype(int).tolist()))
            out.append((4, far["instrument_token"].astype(int).tolist()))
        else:
            out.append((3, self.constituents["instrument_token"].astype(int).tolist()))
        return out

    def all_tokens(self) -> list[int]:
        seen: list[int] = []
        for _p, toks in self.tokens_by_priority():
            seen.extend(t for t in toks if t not in seen)
        return seen

    def token_map(self) -> dict[int, dict[str, Any]]:
        frames = [self.futures, self.options, self.constituents]
        rows: list[dict[str, Any]] = []
        for f in frames:
            if not f.empty:
                rows += f.to_dict("records")
        for s in (self.index, self.vix):
            if s is not None:
                rows.append(dict(s))
        return {int(r["instrument_token"]): r for r in rows}


def round_to_step(x: float, step: int) -> float:
    return round(x / step) * step


def build_universe(inst: pd.DataFrame, spot: float | None, today: date | None = None) -> Universe:
    """Select NIFTY index, VIX, near futures, option strikes around ATM and the constituent basket."""
    cfg = get_config().universe
    today = today or now_ist().date()
    nse = inst[inst["exchange"] == cfg.index_exchange]
    idx = nse[nse["tradingsymbol"] == cfg.index_symbol]
    vix = nse[nse["tradingsymbol"] == cfg.vix_symbol]
    index_row = idx.iloc[0] if not idx.empty else None
    vix_row = vix.iloc[0] if not vix.empty else None

    nfo = inst[(inst["exchange"] == cfg.fno_exchange) & (inst["name"] == cfg.fno_name)].copy()
    nfo = nfo[nfo["expiry"].notna()]
    nfo = nfo[nfo["expiry"].dt.date >= today]

    futs = nfo[nfo["instrument_type"] == "FUT"].sort_values("expiry").head(cfg.futures_contracts)

    opts = nfo[nfo["instrument_type"].isin(["CE", "PE"])].copy()
    expiries = sorted(opts["expiry"].dt.date.unique().tolist())[: cfg.option_expiries]
    opts = opts[opts["expiry"].dt.date.isin(expiries)]

    ref = spot
    if ref is None and index_row is not None and index_row.get("last_price"):
        ref = float(index_row["last_price"])
    atm = round_to_step(ref, cfg.strike_step) if ref else None
    if atm is not None:
        width = cfg.option_strikes_each_side * cfg.strike_step
        opts = opts[(opts["strike"] >= atm - width) & (opts["strike"] <= atm + width)]
    opts = opts.sort_values(["expiry", "strike", "instrument_type"])

    cons = nse[(nse["tradingsymbol"].isin(cfg.constituents)) & (nse["instrument_type"] == "EQ")]
    missing = sorted(set(cfg.constituents) - set(cons["tradingsymbol"]))
    if missing:
        log.warning("constituents_missing_from_master", symbols=missing)

    return Universe(
        index=index_row, vix=vix_row, futures=futs, options=opts, constituents=cons, expiries=expiries, atm_strike=atm
    )


def quote_key(row: pd.Series | dict[str, Any]) -> str:
    return f"{row['exchange']}:{row['tradingsymbol']}"
