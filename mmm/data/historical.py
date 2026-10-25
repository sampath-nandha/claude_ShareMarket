"""Historical candle backfill via Kite's historical API, with window chunking, rate limiting and raw archiving."""

from __future__ import annotations

import gzip
import json
import time
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
import structlog

from mmm.data import parquet_store, paths
from mmm.data.quality import candle_quality
from mmm.settings import get_config
from mmm.timeutil import IST, now_ist

log = structlog.get_logger(__name__)

INTERVAL_MINUTES = {
    "minute": 1, "3minute": 3, "5minute": 5, "10minute": 10, "15minute": 15, "30minute": 30, "60minute": 60,
    "day": None,
}


def _archive_raw(token: int, interval: str, start: date, end: date, rows: list[dict[str, Any]]) -> None:
    out = paths.raw_api_dir("historical") / f"{token}_{interval}_{start}_{end}.json.gz"
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        json.dump(rows, fh, default=str)


def backfill(
    client,
    token: int,
    interval: str,
    start: date,
    end: date | None = None,
    oi: bool = True,
    archive_raw: bool = True,
) -> int:
    """Download [start, end] in documented-size windows, archive raw JSON, merge into Parquet. Returns rows."""
    cfg = get_config().historical
    end = end or now_ist().date()
    max_days = cfg.max_days_per_request.get(interval, 60)
    pause = 1.0 / max(cfg.requests_per_second, 0.5)
    total = 0
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=max_days - 1), end)
        try:
            rows = client.historical(token, cur, chunk_end, interval, oi=oi)
        except Exception as exc:  # network / 4xx; log and continue with next window
            log.warning("historical_chunk_failed", token=token, interval=interval, start=str(cur), end=str(chunk_end), error=str(exc))
            rows = []
        if rows:
            if archive_raw:
                _archive_raw(token, interval, cur, chunk_end, rows)
            df = pd.DataFrame(rows).rename(columns={"date": "ts"})
            status, problems = candle_quality(df.assign(ts=pd.to_datetime(df["ts"])), INTERVAL_MINUTES.get(interval))
            if problems:
                log.info("historical_quality", token=token, interval=interval, status=status.name, problems=problems)
            total += parquet_store.write_candles(df, interval, token)
        cur = chunk_end + timedelta(days=1)
        time.sleep(pause)
    log.info("backfill_done", token=token, interval=interval, rows=total)
    return total


def incremental_update(client, token: int, interval: str, lookback_days_if_empty: int = 365) -> int:
    """Fetch from the last stored candle (minus one day for safety) to now."""
    _first, last, _n = parquet_store.candle_coverage(interval, token)
    start = (last.date() - timedelta(days=1)) if last else now_ist().date() - timedelta(days=lookback_days_if_empty)
    return backfill(client, token, interval, start)


def backfill_options(client, option_rows, interval: str = "minute", days: int = 60, progress=None) -> dict[str, int]:
    """Backfill candles (with OI) for currently active option contracts.

    Kite serves history only for contracts that still exist in the instrument master, so this must run while
    the contracts are alive; expired strikes are gone for good. `option_rows` is the Universe.options frame.
    """
    end = now_ist().date()
    start = end - timedelta(days=days)
    out: dict[str, int] = {}
    for r in option_rows.itertuples():
        n = backfill(client, int(r.instrument_token), interval, start, end, oi=True, archive_raw=False)
        out[r.tradingsymbol] = n
        if progress:
            progress(r.tradingsymbol, n)
    return out


def missing_sessions(interval: str, token: int, sessions: list[date]) -> list[date]:
    """Trading days with no candles stored for the given interval."""
    if not sessions:
        return []
    df = parquet_store.read_candles(interval, token, start=sessions[0], end=datetime.combine(sessions[-1], datetime.max.time(), tzinfo=IST))
    have = set(df["ts"].dt.date.unique()) if not df.empty else set()
    return [d for d in sessions if d not in have]
