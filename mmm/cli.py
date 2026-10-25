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
