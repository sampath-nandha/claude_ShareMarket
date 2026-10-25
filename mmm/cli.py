"""Command-line entry point: `mmm --help`."""

from __future__ import annotations

import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import typer

app = typer.Typer(help="MoneyMakingMachine - Zerodha-only NIFTY intelligence (read-only, no order execution)", no_args_is_help=True)
auth_app = typer.Typer(help="Kite login & secrets")
data_app = typer.Typer(help="Instrument snapshots and historical backfill")
app.add_typer(auth_app, name="auth")
app.add_typer(data_app, name="data")


@auth_app.command("set-keys")
def set_keys(api_key: str = typer.Option(..., prompt=True), api_secret: str = typer.Option(..., prompt=True, hide_input=True)) -> None:
    """Store the Kite API key/secret in the OS credential store (Windows Credential Manager)."""
    from mmm.broker.auth import TokenStore

    TokenStore().set_keys(api_key, api_secret)
    typer.echo("Stored.")


@auth_app.command("login")
def login(no_browser: bool = typer.Option(False, help="Print the URL instead of opening a browser")) -> None:
    """Daily Kite login: opens the login page, captures the redirect, stores today's access token."""
    from mmm.broker.auth import login_interactive

    login_interactive(open_browser=not no_browser)
    typer.echo("Access token stored for today.")


@auth_app.command("token")
def token(request_token: str) -> None:
    """Exchange a request_token pasted from the redirect URL (headless fallback)."""
    from mmm.broker.auth import exchange_request_token

    exchange_request_token(request_token)
    typer.echo("Access token stored for today.")


@auth_app.command("status")
def status() -> None:
    from mmm.broker.auth import auth_status

    for k, v in auth_status().items():
        typer.echo(f"{k}: {v}")


@data_app.command("instruments")
def instruments() -> None:
    """Download and archive today's instrument master; print the NIFTY universe summary."""
    from mmm.broker.auth import get_client
    from mmm.data.instruments import build_universe, snapshot_instruments
    from mmm.settings import get_config

    client = get_client()
    df = snapshot_instruments(client)
    cfg = get_config().universe
    spot = None
    try:
        q = client.ltp([f"{cfg.index_exchange}:{cfg.index_symbol}"])
        spot = list(q.values())[0]["last_price"]
    except Exception as exc:  # non-fatal: universe falls back to last_price in the master
        typer.echo(f"LTP unavailable: {exc}")
    uni = build_universe(df, spot)
    typer.echo(f"rows={len(df)} spot={spot} futures={len(uni.futures)} options={len(uni.options)} constituents={len(uni.constituents)} expiries={uni.expiries}")


@data_app.command("backfill")
def backfill(years: float = typer.Option(3.0, help="Years of history for the index"), intervals: str = "minute,5minute,15minute,60minute,day",
             include_constituents: bool = False) -> None:
    """Backfill NIFTY index (and optionally constituent) candles into the Parquet archive."""
    from mmm.broker.auth import get_client
    from mmm.data.historical import backfill as _bf
    from mmm.data.instruments import build_universe, load_instruments
    from mmm.settings import get_config

    client = get_client()
    inst = load_instruments()
    if inst.empty:
        raise typer.BadParameter("No instrument snapshot; run `mmm data instruments` first")
    uni = build_universe(inst, None)
    cfg = get_config()
    start = date.today() - timedelta(days=int(years * 365))
    targets = []
    if uni.index is not None:
        targets.append((int(uni.index["instrument_token"]), cfg.universe.index_symbol))
    if uni.vix is not None:
        targets.append((int(uni.vix["instrument_token"]), cfg.universe.vix_symbol))
    for r in uni.futures.itertuples():
        targets.append((int(r.instrument_token), r.tradingsymbol))
    if include_constituents:
        targets += [(int(r.instrument_token), r.tradingsymbol) for r in uni.constituents.itertuples()]
    for tok, name in targets:
        for iv in intervals.split(","):
            n = _bf(client, tok, iv.strip(), start)
            typer.echo(f"{name} {iv}: {n} rows")


@data_app.command("update")
def update(intervals: str = "minute,5minute,15minute,60minute,day") -> None:
    """Incremental update from the last stored candle to now for the index, VIX and futures."""
    from mmm.broker.auth import get_client
    from mmm.data.historical import incremental_update
    from mmm.data.instruments import build_universe, load_instruments

    client = get_client()
    uni = build_universe(load_instruments(), None)
    toks = [int(uni.index["instrument_token"])] if uni.index is not None else []
    if uni.vix is not None:
        toks.append(int(uni.vix["instrument_token"]))
    toks += uni.futures["instrument_token"].astype(int).tolist()
    for tok in toks:
        for iv in intervals.split(","):
            typer.echo(f"{tok} {iv}: {incremental_update(client, tok, iv.strip())} rows")


@data_app.command("options-snapshot")
def options_snapshot() -> None:
    """One REST snapshot of the configured option chain into the state DB + archive (useful off-market or as a fallback)."""
    from mmm.scheduler import snapshot_option_chain

    typer.echo(f"{snapshot_option_chain()} contracts")


@data_app.command("backfill-options")
def backfill_options_cmd(days: int = typer.Option(60, help="Days of history per active contract (minute max is 60)"),
                         interval: str = "minute") -> None:
    """Backfill candles+OI for the currently active NIFTY option contracts (only possible while they are alive)."""
    from mmm.broker.auth import get_client
    from mmm.data.historical import backfill_options
    from mmm.data.instruments import build_universe, load_instruments

    client = get_client()
    inst = load_instruments()
    if inst.empty:
        raise typer.BadParameter("No instrument snapshot; run `mmm data instruments` first")
    cfg = get_config_universe()
    spot = None
    try:
        spot = list(client.ltp([f"{cfg.index_exchange}:{cfg.index_symbol}"]).values())[0]["last_price"]
    except Exception as exc:
        typer.echo(f"LTP unavailable ({exc}); using master last_price")
    uni = build_universe(inst, spot)
    typer.echo(f"{len(uni.options)} contracts across {uni.expiries}")
    res = backfill_options(client, uni.options, interval, days, progress=lambda s, n: typer.echo(f"  {s}: {n}"))
    typer.echo(f"total rows: {sum(res.values()):,}")


def get_config_universe():
    from mmm.settings import get_config

    return get_config().universe


@data_app.command("inventory")
def inventory(detail: bool = typer.Option(False, help="List every instrument per interval instead of a summary")) -> None:
    """Print what is actually in the archive: snapshots, candle coverage per interval/instrument, tick/depth/chain partitions."""
    import glob

    import duckdb
    import pandas as pd

    from mmm.data import paths
    from mmm.data.instruments import load_instruments

    root = paths.data_dir()
    inst = load_instruments()
    names = dict(zip(inst["instrument_token"].astype(int), inst["tradingsymbol"])) if not inst.empty else {}
    con = duckdb.connect()
    con.execute("SET TimeZone='Asia/Kolkata'")

    typer.echo(f"Archive: {root}\n")
    snaps = sorted((root / "instruments").glob("date=*")) if (root / "instruments").exists() else []
    typer.echo(f"== Instrument snapshots: {len(snaps)}  " + (f"({snaps[0].name[5:]} .. {snaps[-1].name[5:]})" if snaps else "(none)"))

    typer.echo("\n== Candles")
    cdir = root / "candles"
    for ivdir in sorted(cdir.glob("interval=*")) if cdir.exists() else []:
        iv = ivdir.name.split("=", 1)[1]
        toks = sorted(ivdir.glob("token=*"))
        pattern = str(ivdir / "token=*" / "*.parquet")
        if not glob.glob(pattern):
            continue
        tot = con.execute(f"SELECT count(*), min(ts), max(ts) FROM read_parquet('{pattern}')").fetchone()
        typer.echo(f"  {iv:<9} instruments={len(toks):<4} rows={tot[0]:>10,}  {pd.Timestamp(tot[1]).date()} .. {pd.Timestamp(tot[2]).date()}")
        if detail:
            rows = con.execute(f"SELECT filename, count(*) n, min(ts) f, max(ts) l FROM read_parquet('{pattern}', filename=true) GROUP BY 1 ORDER BY 1").fetchall()
            per_tok: dict[int, list] = {}
            for fn, n, f, l in rows:
                tok = int(fn.replace("\\", "/").split("token=")[1].split("/")[0])
                p = per_tok.setdefault(tok, [0, f, l])
                p[0] += n; p[1] = min(p[1], f); p[2] = max(p[2], l)
            for tok, (n, f, l) in sorted(per_tok.items(), key=lambda kv: -kv[1][0]):
                typer.echo(f"      {names.get(tok, tok):<28} {n:>9,}  {pd.Timestamp(f).date()} .. {pd.Timestamp(l).date()}")

    for kind in ("ticks", "depth", "option_chain"):
        base = root / kind
        parts = sorted(base.glob("date=*")) if base.exists() else []
        typer.echo(f"\n== {kind}: {len(parts)} partition(s)")
        for p in parts[-10:]:
            pattern = str(p / "*.parquet")
            if not glob.glob(pattern):
                continue
            col = "tradingsymbol" if kind == "option_chain" else "instrument_token"
            n, k = con.execute(f"SELECT count(*), count(DISTINCT {col}) FROM read_parquet('{pattern}', union_by_name=true)").fetchone()
            size = sum(f.stat().st_size for f in p.glob("*.parquet")) / 1e6
            typer.echo(f"  {p.name[5:]}  rows={n:>10,}  instruments={k:<5} files={len(list(p.glob('*.parquet'))):<4} {size:6.1f} MB")
        if len(parts) > 10:
            typer.echo(f"  ... {len(parts) - 10} older partition(s) not shown")
    spool = list(paths.spool_dir().glob("*.parquet"))
    if spool:
        typer.echo(f"\n!! {len(spool)} spool file(s) waiting for replay")
    total = sum(f.stat().st_size for f in root.rglob("*.parquet")) / 1e9
    typer.echo(f"\nTotal Parquet on disk: {total:.2f} GB")


@data_app.command("export-seed")
def export_seed() -> None:
    """Copy the archive (instruments, candles, option_chain snapshots) into ./seed so it can be committed once."""
    import shutil

    from mmm.data import paths

    src, dst = paths.data_dir(), paths.seed_dir()
    for kind in ("instruments", "candles", "option_chain"):
        s = src / kind
        if s.exists():
            shutil.copytree(s, dst / kind, dirs_exist_ok=True)
            typer.echo(f"{kind}: {sum(1 for _ in (dst / kind).rglob('*.parquet'))} files")
    total = sum(p.stat().st_size for p in dst.rglob("*.parquet")) / 1e6
    typer.echo(f"seed size {total:.1f} MB")


@data_app.command("import-seed")
def import_seed() -> None:
    """Copy ./seed into the data dir (fresh machine bootstrap). Existing files are kept unless the seed is newer."""
    import shutil

    from mmm.data import paths

    src, dst = paths.seed_dir(), paths.data_dir()
    n = 0
    for p in src.rglob("*.parquet"):
        target = dst / p.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or p.stat().st_mtime > target.stat().st_mtime:
            shutil.copy2(p, target)
            n += 1
    typer.echo(f"imported {n} files into {dst}")


research_app = typer.Typer(help="Walk-forward research, calibration and strategy status")
app.add_typer(research_app, name="research")


@research_app.command("run")
def research_run(train_days: int = 120, test_days: int = 30, holdout_days: int = 60,
                 days: int = typer.Option(0, help="Only use the last N calendar days of history (0 = all)"),
                 workers: int = typer.Option(0, help="Parallel processes (0 = CPU count - 1, max 8)")) -> None:
    """One research-mode pass over archived NIFTY minutes: per-strategy OOS stats, calibration, statuses, report.

    Progress prints as chunks finish. Rough speed: ~0.5 s/session per core, so 3 years on 4 cores ~ 5 minutes.
    """
    from mmm.backtest.engine import default_workers
    from mmm.research.report import run

    w = workers or default_workers()
    typer.echo(f"workers: {w}")

    def _progress(done: int, total: int, trades: int) -> None:
        typer.echo(f"  sessions ~{done}/{total} · trades so far {trades}")

    r = run(train_days=train_days, test_days=test_days, holdout_days=holdout_days, days=days or None, progress=_progress, workers=w)
    for sid, e in r["strategies"].items():
        o = e.get("oos") or {}
        typer.echo(f"{sid}: n={o.get('n', 0)} win_rate={o.get('win_rate')} LB={o.get('win_rate_ci_low')} verdict={o.get('verdict')} status={e['status']}")
    if "calibration" in r:
        typer.echo(f"calibration: {r['calibration']['method']} n={r['calibration']['n']} usable={r['calibration']['usable']}")
    typer.echo("report: " + ", ".join(r["paths"]))


@research_app.command("status")
def research_status() -> None:
    from mmm.research.registry import statuses

    for sid, s in sorted(statuses().items()):
        typer.echo(f"{sid}: {s['status']} - {s['reason']} ({s['updated_at']})")


@app.command()
def reconcile() -> None:
    """Match MANUAL journal entries with today's broker fills (read-only)."""
    from mmm.broker.auth import get_client
    from mmm.paper.reconcile import reconcile as _rec

    res = _rec(get_client())
    for k, df in res.items():
        typer.echo(f"\n== {k} ({len(df)})")
        if not df.empty:
            typer.echo(df.to_string())


@app.command()
def collect(spot: float | None = typer.Option(None, help="Spot hint for ATM selection before the first tick")) -> None:
    """Run the live WebSocket collector (FULL mode) until stopped."""
    from mmm.data.collector import run_collector

    run_collector(spot)


@app.command()
def schedule() -> None:
    """Run the in-process scheduler (instrument snapshot, chain snapshots, EOD update, health checks)."""
    from mmm.scheduler import run_scheduler

    run_scheduler()


@app.command()
def dashboard(port: int = 8501) -> None:
    """Launch the Streamlit dashboard."""
    app_path = Path(__file__).parent / "dashboard" / "app.py"
    subprocess.run([sys.executable, "-m", "streamlit", "run", str(app_path), "--server.port", str(port), "--server.headless", "true"], check=False)


@app.command()
def backtest(days: int = 60, strategies: str = "PA01,PA02,PA03,PA04,PA05") -> None:
    """Run a backtest on archived NIFTY minute bars and print the statistics."""
    from mmm.backtest.engine import BacktestConfig, run_backtest
    from mmm.backtest.metrics import compute_stats
    from mmm.data import parquet_store
    from mmm.data.instruments import load_instruments
    from mmm.settings import get_config

    cfg = get_config().universe
    inst = load_instruments()
    idx = inst[(inst["exchange"] == cfg.index_exchange) & (inst["tradingsymbol"] == cfg.index_symbol)]
    if idx.empty:
        raise typer.BadParameter("No NIFTY index in instrument snapshot")
    bars = parquet_store.read_candles("minute", int(idx.iloc[0]["instrument_token"]), start=date.today() - timedelta(days=days))
    res = run_backtest(bars, BacktestConfig(strategies=strategies.split(",")))
    s = compute_stats(res.trades)
    typer.echo(f"sessions={res.sessions} candidates={res.candidates} trades={s.n} verdict={s.verdict}")
    for k, v in s.as_dict().items():
        typer.echo(f"  {k}: {v}")


if __name__ == "__main__":
    app()
