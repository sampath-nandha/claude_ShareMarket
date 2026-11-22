"""Continuous near-month NIFTY futures minute series, stitched from archived contracts.

Every NIFTY future that ever appeared in an archived instrument snapshot is a candidate. For each trading day
the *front* contract is the one with the nearest expiry at least `roll_days` calendar days away (so the series
rolls a couple of days before expiry, when liquidity moves). The stitched series keeps each row's own contract
token and expiry, so basis versus fair value is always computed against the right expiry.

If Kite's `continuous=1` history works for 1-minute futures on your account (`mmm data probe-continuous`), those
candles are stored under CONTINUOUS_FUTURES_TOKEN and used where no archived contract covers a day; their
expiry is the rule-based monthly expiry (approximate around exchange holidays, and labelled so).
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

import pandas as pd
import structlog

from mmm.data import parquet_store, paths
from mmm.settings import get_config

log = structlog.get_logger(__name__)

CONTINUOUS_FUTURES_TOKEN = 9_000_000_001      # outside Kite's token range; never collides with a real instrument


def futures_contracts() -> pd.DataFrame:
    """All NIFTY futures seen in any instrument snapshot: instrument_token, tradingsymbol, expiry (date)."""
    cfg = get_config().universe
    base = paths.data_dir() / "instruments"
    frames = []
    for f in sorted(base.glob("date=*/instruments.parquet")) if base.exists() else []:
        try:
            df = pd.read_parquet(f, columns=["instrument_token", "tradingsymbol", "name", "expiry", "instrument_type", "exchange"])
        except Exception as exc:  # noqa: BLE001 - a corrupt snapshot must not stop the series
            log.warning("instrument_snapshot_unreadable", file=str(f), error=str(exc))
            continue
        df = df[(df["exchange"] == cfg.fno_exchange) & (df["name"] == cfg.fno_name) & (df["instrument_type"] == "FUT")]
        frames.append(df[["instrument_token", "tradingsymbol", "expiry"]])
    if not frames:
        return pd.DataFrame(columns=["instrument_token", "tradingsymbol", "expiry"])
    out = pd.concat(frames, ignore_index=True).drop_duplicates("instrument_token")
    out["expiry"] = pd.to_datetime(out["expiry"]).dt.date
    return out.sort_values("expiry").reset_index(drop=True)


def monthly_expiry(d: date) -> date:
    """Rule-based monthly expiry for the month of `d` (last configured weekday), or next month's if passed."""
    rules = sorted(get_config().data_sources.weekly_expiry_rules, key=lambda r: r.from_date)

    def last_weekday(y: int, m: int) -> date:
        wd = rules[0].weekday
        for r in rules:
            if date.fromisoformat(r.from_date) <= date(y, m, 1):
                wd = r.weekday
        last = date(y, m, calendar.monthrange(y, m)[1])
        while last.weekday() != wd:
            last -= timedelta(days=1)
        return last

    e = last_weekday(d.year, d.month)
    if e < d:
        y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
        e = last_weekday(y, m)
    return e


def front_contract_by_day(contracts: pd.DataFrame, days: list[date], roll_days: int = 2) -> dict[date, tuple[int, date]]:
    out: dict[date, tuple[int, date]] = {}
    if contracts.empty:
        return out
    rows = list(contracts.itertuples())
    for d in days:
        live = [r for r in rows if r.expiry >= d]
        if not live:
            continue
        pref = [r for r in live if (r.expiry - d).days >= roll_days] or live
        r = min(pref, key=lambda x: x.expiry)
        out[d] = (int(r.instrument_token), r.expiry)
    return out


def continuous_futures(start: date | None = None, end: date | None = None, roll_days: int = 2) -> pd.DataFrame:
    """Stitched 1-minute front-month series with columns ts, open, high, low, close, volume, oi, token, expiry, source."""
    contracts = futures_contracts()
    parts: list[pd.DataFrame] = []
    cache: dict[int, pd.DataFrame] = {}
    for tok in contracts["instrument_token"].astype(int):
        df = parquet_store.read_candles("minute", tok, start=start, end=end)
        if not df.empty:
            cache[tok] = df
    days = sorted({d for df in cache.values() for d in df["ts"].dt.date.unique()})
    front = front_contract_by_day(contracts[contracts["instrument_token"].isin(list(cache))], days, roll_days)
    for d, (tok, exp) in front.items():
        df = cache[tok]
        day = df[df["ts"].dt.date == d]
        if not day.empty:
            parts.append(day.assign(token=tok, expiry=exp, source="contract"))
    cont = parquet_store.read_candles("minute", CONTINUOUS_FUTURES_TOKEN, start=start, end=end)
    if not cont.empty:
        have = set(front)
        extra = cont[~cont["ts"].dt.date.isin(have)]
        if not extra.empty:
            exp = extra["ts"].dt.date.map(monthly_expiry)
            parts.append(extra.assign(token=CONTINUOUS_FUTURES_TOKEN, expiry=exp, source="kite_continuous"))
    if not parts:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume", "oi", "token", "expiry", "source"])
    return pd.concat(parts, ignore_index=True).sort_values("ts").reset_index(drop=True)


def probe_continuous(client, token: int) -> dict[str, int | str]:
    """Ask Kite for continuous 1-minute and daily futures history ~1 year back. Reports rows returned per interval."""
    out: dict[str, int | str] = {}
    end = date.today() - timedelta(days=365)
    for interval, span in (("minute", 5), ("day", 30)):
        try:
            rows = client.historical(token, end - timedelta(days=span), end, interval, oi=True, continuous=True)
            out[interval] = len(rows)
        except Exception as exc:  # noqa: BLE001 - the probe reports, never raises
            out[interval] = f"error: {exc}"
    return out


def backfill_continuous(client, token: int, years: float = 3.0, progress=None) -> int:
    """Store Kite continuous 1-minute futures history under CONTINUOUS_FUTURES_TOKEN (60-day windows, OI included)."""
    import time

    end = date.today()
    cur = end - timedelta(days=int(years * 365))
    total = 0
    pause = 1.0 / max(get_config().historical.requests_per_second, 0.5)
    while cur <= end:
        nxt = min(cur + timedelta(days=59), end)
        try:
            rows = client.historical(token, cur, nxt, "minute", oi=True, continuous=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("continuous_chunk_failed", start=str(cur), end=str(nxt), error=str(exc))
            rows = []
        if rows:
            total += parquet_store.write_candles(pd.DataFrame(rows).rename(columns={"date": "ts"}), "minute", CONTINUOUS_FUTURES_TOKEN)
        if progress:
            progress(cur, nxt, len(rows))
        cur = nxt + timedelta(days=1)
        time.sleep(pause)
    return total
