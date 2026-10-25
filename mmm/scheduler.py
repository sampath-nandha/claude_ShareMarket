"""In-process scheduler (APScheduler) that replaces Windows Task Scheduler entries.

Jobs (IST, trading days only):
  07:30  login reminder (Windows toast / Telegram)         08:00  instrument snapshot
  09:15-15:30 every minute  option-chain REST snapshot (fallback/complement to WebSocket)
  16:30  incremental candle update                          every 2 min  health check -> alerts
"""

from __future__ import annotations

import time

import pandas as pd
import structlog
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from mmm.data import parquet_store, paths
from mmm.data.instruments import build_universe, load_instruments, quote_key
from mmm.data.quality import quote_quality
from mmm.data.state_db import StateDB
from mmm.settings import get_config, get_secrets
from mmm.timeutil import IST, is_market_open, is_trading_day, now_ist, to_ist

log = structlog.get_logger(__name__)


def notify(text: str) -> None:
    """Telegram (if configured) + Windows toast (if available) + log."""
    log.info("notify", text=text)
    sec = get_secrets()
    if sec.telegram_bot_token and sec.telegram_chat_id:
        try:
            import httpx

            httpx.post(f"https://api.telegram.org/bot{sec.telegram_bot_token}/sendMessage", data={"chat_id": sec.telegram_chat_id, "text": text}, timeout=10)
        except Exception as exc:
            log.warning("telegram_failed", error=str(exc))
    try:  # optional Windows toast
        from win10toast import ToastNotifier  # type: ignore

        ToastNotifier().show_toast("MoneyMakingMachine", text, duration=10, threaded=True)
    except Exception:
        pass


def job_login_reminder() -> None:
    from mmm.broker.auth import auth_status

    if not auth_status()["token_valid_today"]:
        notify("Kite login needed: run `mmm auth login` before 09:00.")


def job_instruments() -> None:
    from mmm.broker.auth import get_client
    from mmm.data.instruments import snapshot_instruments

    try:
        snapshot_instruments(get_client())
        StateDB().heartbeat("scheduler", "UP", last_job="instruments")
    except Exception as exc:
        notify(f"Instrument snapshot failed: {exc}")


def snapshot_option_chain() -> int:
    """REST quote() snapshot of the configured chain -> state DB (latest) + Parquet partition (history)."""
    from mmm.broker.auth import get_client

    client = get_client()
    inst = load_instruments()
    if inst.empty:
        raise RuntimeError("no instrument snapshot")
    cfg = get_config().universe
    spot_q = client.ltp([f"{cfg.index_exchange}:{cfg.index_symbol}"])
    spot = list(spot_q.values())[0]["last_price"]
    uni = build_universe(inst, spot)
    if uni.options.empty:
        return 0
    keys = [quote_key(r) for _, r in uni.options.iterrows()]
    quotes = client.quotes(keys)
    state = StateDB()
    prev = {r["tradingsymbol"]: r for r in state.chain()}
    now = now_ist()
    rows = []
    for _, r in uni.options.iterrows():
        q = quotes.get(quote_key(r))
        if not q:
            continue
        depth = q.get("depth") or {}
        bid = (depth.get("buy") or [{}])[0].get("price")
        ask = (depth.get("sell") or [{}])[0].get("price")
        ts = q.get("timestamp")
        ex_ts = to_ist(ts) if ts else None
        p = prev.get(r["tradingsymbol"])
        rows.append({
            "tradingsymbol": r["tradingsymbol"], "instrument_token": int(r["instrument_token"]), "expiry": pd.Timestamp(r["expiry"]).date().isoformat(),
            "strike": float(r["strike"]), "option_type": r["instrument_type"], "last_price": q.get("last_price"), "bid": bid, "ask": ask,
            "volume": q.get("volume"), "oi": q.get("oi"), "prev_oi": (p or {}).get("oi") if p else q.get("oi"), "prev_close": (q.get("ohlc") or {}).get("close"),
            "exchange_ts": ex_ts, "received_ts": now, "quality": quote_quality(ex_ts, now).name,
        })
    state.upsert_chain(rows)
    df = pd.DataFrame(rows)
    df["spot"] = spot
    parquet_store.append_partition("option_chain", now.date(), df)
    state.set_kv("spot", spot)
    state.heartbeat("chain_snapshot", "UP", contracts=len(rows), spot=spot)
    return len(rows)


def job_chain_snapshot() -> None:
    if not is_market_open():
        return
    try:
        snapshot_option_chain()
    except Exception as exc:
        log.warning("chain_snapshot_failed", error=str(exc))
        StateDB().heartbeat("chain_snapshot", "ERROR", error=str(exc))


def job_eod_update() -> None:
    from mmm.broker.auth import get_client
    from mmm.data.historical import incremental_update

    try:
        client = get_client()
        uni = build_universe(load_instruments(), None)
        toks = ([int(uni.index["instrument_token"])] if uni.index is not None else []) + uni.futures["instrument_token"].astype(int).tolist()
        if uni.vix is not None:
            toks.append(int(uni.vix["instrument_token"]))
        for tok in toks:
            for iv in get_config().historical.intervals:
                incremental_update(client, tok, iv)
        replay_spool()
        StateDB().heartbeat("scheduler", "UP", last_job="eod_update")
    except Exception as exc:
        notify(f"EOD update failed: {exc}")


def replay_spool() -> None:
    for f in sorted(paths.spool_dir().glob("*.parquet")):
        kind = "ticks" if f.name.startswith("ticks") else "depth"
        df = pd.read_parquet(f)
        d = pd.Timestamp(df["received_ts"].iloc[0]).date() if not df.empty else now_ist().date()
        parquet_store.append_partition(kind, d, df)
        f.unlink()
        log.info("spool_replayed", file=f.name)


def job_health() -> None:
    state = StateDB()
    if not is_market_open():
        return
    hb = {h["component"]: h for h in state.heartbeats()}
    col = hb.get("collector")
    if col is None:
        notify("Collector has never reported during market hours.")
        return
    age = (now_ist() - pd.Timestamp(col["ts"]).tz_convert(IST)).total_seconds()
    last_tick = col["detail"].get("last_tick_at")
    tick_age = (now_ist() - pd.Timestamp(last_tick).tz_convert(IST)).total_seconds() if last_tick else None
    if col["status"] != "UP" or age > 60 or (tick_age is not None and tick_age > 60):
        notify(f"Collector unhealthy: status={col['status']} heartbeat_age={age:.0f}s tick_age={tick_age}")
    import shutil

    free = shutil.disk_usage(paths.data_dir()).free / 1e9
    if free < 20:
        notify(f"Low disk: {free:.0f} GB free on data drive")


def run_scheduler() -> None:
    sched = BackgroundScheduler(timezone=IST)
    trading = {"day_of_week": "mon-fri"}
    sched.add_job(job_login_reminder, CronTrigger(hour=7, minute=30, **trading))
    sched.add_job(job_login_reminder, CronTrigger(hour=8, minute=45, **trading))
    sched.add_job(job_instruments, CronTrigger(hour=8, minute=0, **trading))
    sched.add_job(job_chain_snapshot, CronTrigger(hour="9-15", minute="*", **trading))
    sched.add_job(job_eod_update, CronTrigger(hour=16, minute=30, **trading))
    sched.add_job(job_health, CronTrigger(minute="*/2"))
    sched.start()
    StateDB().heartbeat("scheduler", "UP", started=now_ist())
    log.info("scheduler_started", jobs=[j.name for j in sched.get_jobs()])
    try:
        while True:
            time.sleep(5)
            if not is_trading_day():
                time.sleep(55)
    except (KeyboardInterrupt, SystemExit):
        sched.shutdown()
        StateDB().heartbeat("scheduler", "STOPPED")
