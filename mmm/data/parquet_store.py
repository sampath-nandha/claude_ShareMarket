"""Parquet archive + DuckDB query layer for bulk time-series (candles, ticks, depth, option snapshots)."""

from __future__ import annotations

import glob
import uuid
from datetime import date, datetime
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from mmm.data import paths
from mmm.timeutil import IST

CANDLE_COLS = ["ts", "open", "high", "low", "close", "volume", "oi"]


def _connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(database=":memory:")
    con.execute("SET TimeZone='Asia/Kolkata'")
    return con


# --- candles ---------------------------------------------------------------------------------
def write_candles(df: pd.DataFrame, interval: str, token: int) -> int:
    """Merge candles into the per-year Parquet file (idempotent upsert on ts). Returns rows written."""
    if df.empty:
        return 0
    df = _normalise_candles(df)
    written = 0
    for year, part in df.groupby(df["ts"].dt.year):
        path = paths.candles_file(interval, token, int(year))
        if path.exists():
            old = pd.read_parquet(path)
            old["ts"] = pd.to_datetime(old["ts"], utc=True).dt.tz_convert(IST)
            merged = pd.concat([old, part], ignore_index=True)
            merged = merged.drop_duplicates("ts", keep="last").sort_values("ts")
        else:
            merged = part.sort_values("ts")
        tmp = path.with_suffix(".tmp.parquet")
        merged[CANDLE_COLS].to_parquet(tmp, index=False, compression="zstd")
        tmp.replace(path)
        written += len(part)
    return written


def read_candles(
    interval: str,
    token: int,
    start: datetime | date | None = None,
    end: datetime | date | None = None,
) -> pd.DataFrame:
    pattern = paths.candles_glob(interval, token)
    if not glob.glob(pattern):
        return pd.DataFrame(columns=CANDLE_COLS)
    con = _connect()
    q = f"SELECT * FROM read_parquet('{pattern}')"
    conds = []
    if start is not None:
        conds.append(f"ts >= TIMESTAMPTZ '{_ts(start)}'")
    if end is not None:
        conds.append(f"ts <= TIMESTAMPTZ '{_ts(end)}'")
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY ts"
    df = con.execute(q).df()
    con.close()
    return _normalise_candles(df)


def candle_coverage(interval: str, token: int) -> tuple[datetime | None, datetime | None, int]:
    pattern = paths.candles_glob(interval, token)
    if not glob.glob(pattern):
        return None, None, 0
    con = _connect()
    row = con.execute(f"SELECT min(ts), max(ts), count(*) FROM read_parquet('{pattern}')").fetchone()
    con.close()
    if row is None or row[2] == 0:
        return None, None, 0
    return pd.Timestamp(row[0]).tz_convert(IST).to_pydatetime(), pd.Timestamp(row[1]).tz_convert(IST).to_pydatetime(), int(row[2])


def _normalise_candles(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if "date" in df.columns and "ts" not in df.columns:
        df = df.rename(columns={"date": "ts"})
    df["ts"] = pd.to_datetime(df["ts"])
    if df["ts"].dt.tz is None:
        df["ts"] = df["ts"].dt.tz_localize(IST)
    else:
        df["ts"] = df["ts"].dt.tz_convert(IST)
    for c in ["open", "high", "low", "close"]:
        df[c] = df[c].astype("float64")
    df["volume"] = df.get("volume", 0)
    df["volume"] = df["volume"].fillna(0).astype("int64")
    df["oi"] = df.get("oi", 0)
    df["oi"] = df["oi"].fillna(0).astype("int64")
    return df[CANDLE_COLS].sort_values("ts").reset_index(drop=True)


# --- append-only partitions (ticks, depth, option_chain) ---------------------------------------
def append_partition(kind: str, d: date, df: pd.DataFrame) -> Path | None:
    if df.empty:
        return None
    out = paths.partition_dir(kind, d) / f"part-{datetime.now().strftime('%H%M%S')}-{uuid.uuid4().hex[:6]}.parquet"
    table = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(table, out, compression="zstd")
    return out


def read_partition(kind: str, d: date | None = None, where: str | None = None) -> pd.DataFrame:
    pattern = paths.partition_glob(kind, d)
    if not glob.glob(pattern):
        return pd.DataFrame()
    con = _connect()
    q = f"SELECT * FROM read_parquet('{pattern}', union_by_name=true)"
    if where:
        q += f" WHERE {where}"
    df = con.execute(q).df()
    con.close()
    return df


def query(sql: str) -> pd.DataFrame:
    """Ad-hoc DuckDB over the archive. Use paths.partition_glob()/candles_glob() to build sources."""
    con = _connect()
    try:
        return con.execute(sql).df()
    finally:
        con.close()


def available_dates(kind: str) -> list[date]:
    base = paths.data_dir() / kind
    if not base.exists():
        return []
    out = []
    for p in sorted(base.glob("date=*")):
        try:
            out.append(date.fromisoformat(p.name.split("=", 1)[1]))
        except ValueError:
            continue
    return out


def _ts(x: datetime | date) -> str:
    if isinstance(x, datetime):
        if x.tzinfo is None:
            x = x.replace(tzinfo=IST)
        return x.isoformat()
    return datetime(x.year, x.month, x.day, tzinfo=IST).isoformat()
