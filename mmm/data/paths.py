"""On-disk layout under MMM_DATA_DIR.

data/
  state.sqlite                     operational state (quotes cache, signals, journal, health, audit)
  instruments/date=YYYY-MM-DD/instruments.parquet
  candles/interval=X/token=N/year=YYYY.parquet
  ticks/date=YYYY-MM-DD/part-*.parquet
  depth/date=YYYY-MM-DD/part-*.parquet
  option_chain/date=YYYY-MM-DD/part-*.parquet
  raw_api/historical/...json.gz     raw API responses (append-only)
  spool/                           emergency tick spool when the writer is down
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from mmm.settings import get_secrets


def data_dir() -> Path:
    p = Path(get_secrets().data_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_db_path() -> Path:
    return data_dir() / "state.sqlite"


def instruments_dir(d: date) -> Path:
    p = data_dir() / "instruments" / f"date={d.isoformat()}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def candles_file(interval: str, token: int, year: int) -> Path:
    p = data_dir() / "candles" / f"interval={interval}" / f"token={token}"
    p.mkdir(parents=True, exist_ok=True)
    return p / f"year={year}.parquet"


def candles_glob(interval: str, token: int | None = None) -> str:
    base = data_dir() / "candles" / f"interval={interval}"
    if token is None:
        return str(base / "token=*" / "*.parquet")
    return str(base / f"token={token}" / "*.parquet")


def partition_dir(kind: str, d: date) -> Path:
    p = data_dir() / kind / f"date={d.isoformat()}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def partition_glob(kind: str, d: date | None = None) -> str:
    base = data_dir() / kind
    if d is None:
        return str(base / "date=*" / "*.parquet")
    return str(base / f"date={d.isoformat()}" / "*.parquet")


def raw_api_dir(kind: str) -> Path:
    p = data_dir() / "raw_api" / kind
    p.mkdir(parents=True, exist_ok=True)
    return p


def spool_dir() -> Path:
    p = data_dir() / "spool"
    p.mkdir(parents=True, exist_ok=True)
    return p


def seed_dir() -> Path:
    """Committed one-time data seed inside the repo (see `mmm data export-seed` / `import-seed`)."""
    from mmm.settings import PROJECT_ROOT

    p = PROJECT_ROOT / "seed"
    p.mkdir(parents=True, exist_ok=True)
    return p


def logs_dir() -> Path:
    p = data_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p
