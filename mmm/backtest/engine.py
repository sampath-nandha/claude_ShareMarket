"""Event-driven backtest over 1-minute NIFTY candles using the same strategy code as live detection.

Execution model (conservative, disclosed):
  * Decisions are made at setup-TF bar closes; entry fills at the next 1-minute bar's open +/- slippage.
  * Exits are evaluated on 1-minute bars. If a bar touches both stop and target, the STOP is assumed first.
  * Forced exit at the configured session cut-off (default 15:20) at that bar's close.
  * Index-point P&L is converted to rupees via a configurable point value (default: NIFTY lot 75,
    i.e. a futures-like proxy). Option-execution P&L is a separate dimension (backtest.option_leg).
  * Costs: configurable per-trade rupee cost + slippage; the default mirrors Zerodha futures-like costs.

Every position is flat by the session close, so sessions are independent and can be simulated in parallel
processes (`workers > 1`); results are identical to the single-process run.
"""

from __future__ import annotations

import os
import time as _clock
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd
import structlog

from mmm.analytics.candles import MINUTES
from mmm.settings import get_config
from mmm.signals.engine import FrameCache, build_context, stage1
from mmm.strategies import Strategy, load_strategies

log = structlog.get_logger(__name__)

WARMUP_SESSIONS = 10


@dataclass
class BacktestConfig:
    setup_tf: str = "5minute"
    start: date | None = None
    end: date | None = None
    point_value: float = 75.0            # rupees per index point (1 lot proxy)
    cost_per_trade: float = 60.0         # rupees round trip (brokerage+taxes proxy for a futures-like unit)
    slippage_points: float = 1.0         # per side, index points
    last_entry: time = time(14, 30)
    force_exit: time = time(15, 20)
    max_hold_bars: int = 120             # minutes
    allow_overlap: bool = False
    strategies: list[str] | None = None


@dataclass
class Trade:
    strategy_id: str
    direction: str
    signal_ts: datetime
    entry_ts: datetime
    entry: float
    stop: float
    target1: float
    target2: float
    exit_ts: datetime | None = None
    exit: float | None = None
    exit_reason: str | None = None
    mfe: float = 0.0
    mae: float = 0.0
    regime: str = ""
    trigger: str = ""
    bars_held: int = 0
    pa_score: float = 0.0
    coincident: str = ""

    def to_row(self, cfg: BacktestConfig) -> dict:
        d = 1 if self.direction == "BULLISH" else -1
        pts = d * ((self.exit or self.entry) - self.entry)
        gross = pts * cfg.point_value
        costs = cfg.cost_per_trade + 2 * cfg.slippage_points * cfg.point_value
        return {
            "strategy_id": self.strategy_id, "direction": self.direction, "signal_ts": self.signal_ts, "entry_ts": self.entry_ts,
            "exit_ts": self.exit_ts, "entry": self.entry, "exit": self.exit, "stop": self.stop, "target1": self.target1,
            "exit_reason": self.exit_reason, "points": pts, "pnl_gross": gross, "costs": costs, "pnl_net": gross - costs,
            "mfe": self.mfe, "mae": self.mae, "bars_held": self.bars_held, "regime": self.regime, "trigger": self.trigger,
            "session": self.entry_ts.date(), "hour": self.entry_ts.hour, "weekday": self.entry_ts.strftime("%a"),
            "directional_hit": bool(self.exit_reason == "TARGET1"), "pa_score": self.pa_score, "coincident": self.coincident,
        }


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    candidates: int
    accepted: int
    sessions: int
    config: BacktestConfig
    notes: list[str] = field(default_factory=list)


EMPTY_COLS = ["strategy_id", "direction", "pnl_gross", "costs", "pnl_net", "session"]


def _decision_sessions(bars: pd.DataFrame, cfg: BacktestConfig) -> list[date]:
    all_sessions = sorted(bars["ts"].dt.date.unique())
    if not all_sessions:
        return []
    warm_from = all_sessions[min(len(all_sessions) - 1, WARMUP_SESSIONS)]
    return [d for d in all_sessions if d >= warm_from and (cfg.start is None or d >= cfg.start) and (cfg.end is None or d <= cfg.end)]


def _simulate(bars: pd.DataFrame, cfg: BacktestConfig, sessions: list[date], research_mode: bool, progress=None,
              log_every: int = 25) -> tuple[list[dict], int]:
    """Simulate the given sessions over the full bar history. Returns (trade rows, candidate count)."""
    strategies: list[Strategy] = load_strategies(cfg.strategies or get_config().strategies.enabled)
    scfg = get_config().strategies
    cache = FrameCache(bars, cfg.setup_tf, scfg.context_timeframe, scfg.trend_timeframe)
    step = MINUTES[cfg.setup_tf]
    dates = bars["ts"].dt.date.to_numpy()
    ts_arr = bars["ts"]
    opens, highs, lows, closes = bars["open"].to_numpy(), bars["high"].to_numpy(), bars["low"].to_numpy(), bars["close"].to_numpy()
    trades: list[Trade] = []
    n_cand = 0
    open_trades: dict[str, Trade] = {}
    t_start = _clock.monotonic()

    def close_all(idx: int, reason: str) -> None:
        for key in list(open_trades):
            t = open_trades.pop(key)
            t.exit, t.exit_ts, t.exit_reason = float(closes[idx]), ts_arr.iloc[idx].to_pydatetime(), reason
            trades.append(t)

    for s_i, day in enumerate(sessions):
        idxs = np.flatnonzero(dates == day)
        if idxs.size == 0:
            continue
        t0 = ts_arr.iloc[idxs[0]].replace(hour=9, minute=15, second=0, microsecond=0)
        decision_set = {t0 + timedelta(minutes=step * k) for k in range(1, 400) if (t0 + timedelta(minutes=step * k)).time() <= cfg.last_entry}
        for j, i in enumerate(idxs):
            ts = ts_arr.iloc[i]
            if open_trades:
                for key in list(open_trades):
                    t = open_trades[key]
                    _update_exit(t, ts, highs[i], lows[i], closes[i], cfg)
                    if t.exit is not None:
                        trades.append(open_trades.pop(key))
                if open_trades and ts.time() >= cfg.force_exit:
                    close_all(i, "SESSION_END")
            boundary = ts + timedelta(minutes=1)
            if boundary not in decision_set or j + 1 >= idxs.size:
                continue
            if not research_mode and open_trades and not cfg.allow_overlap:
                continue
            ctx = build_context(None, boundary.to_pydatetime(), setup_tf=cfg.setup_tf, cache=cache)
            if ctx is None:
                continue
            cands = stage1(ctx, strategies, dedupe_candidates=not research_mode)
            n_cand += len(cands)
            if not cands:
                continue
            nxt = idxs[j + 1]
            chosen = [(s, c) for s, c in cands if s.id not in open_trades] if research_mode else [max(cands, key=lambda sc: sc[1].pa_score)]
            for strat, cand in chosen:
                d = 1 if cand.direction == "BULLISH" else -1
                fill = float(opens[nxt]) + d * cfg.slippage_points
                key = strat.id if research_mode else "_"
                open_trades[key] = Trade(strat.id, cand.direction, boundary.to_pydatetime(), ts_arr.iloc[nxt].to_pydatetime(), fill, cand.stop,
                                         cand.target1, cand.target2, regime=ctx.regime.code, trigger=cand.trigger_event, pa_score=cand.pa_score,
                                         coincident=",".join(sc[0].id for sc in cands if sc[0].id != strat.id))
        if open_trades:
            close_all(int(idxs[-1]), "SESSION_END")
        if log_every and (s_i + 1) % log_every == 0:
            log.info("backtest_progress", sessions_done=s_i + 1, sessions_total=len(sessions), trades=len(trades), candidates=n_cand,
                     elapsed_s=round(_clock.monotonic() - t_start))
        if progress:
            progress(s_i + 1, len(sessions), len(trades))
    return [t.to_row(cfg) for t in trades], n_cand


def _worker(args: tuple) -> tuple[list[dict], int]:
    bars, cfg, sessions, research_mode = args
    return _simulate(bars, cfg, sessions, research_mode, progress=None, log_every=0)


def default_workers() -> int:
    return max(1, min(8, (os.cpu_count() or 2) - 1))


def run_backtest(minute_bars: pd.DataFrame, cfg: BacktestConfig | None = None, research_mode: bool = False,
                 progress=None, log_every: int = 25, workers: int = 1) -> BacktestResult:
    """Event-driven backtest.

    * All provided bars feed the FrameCache (history); decisions happen only for sessions in [start, end]
      and after WARMUP_SESSIONS from the first provided session.
    * research_mode=False: production behaviour - de-duplicated candidates, one open trade at a time.
    * research_mode=True:  every strategy is evaluated independently with its own open trade, so one pass
      yields per-strategy statistics for all strategies (used by `mmm research run`).
    * workers>1: sessions are split into contiguous chunks simulated in separate processes.
    """
    cfg = cfg or BacktestConfig()
    bars = minute_bars.sort_values("ts").reset_index(drop=True)
    if bars.empty:
        return BacktestResult(pd.DataFrame(columns=EMPTY_COLS), 0, 0, 0, cfg, ["no bars"])
    sessions = _decision_sessions(bars, cfg)
    notes = ["stop assumed first when stop and target touched in the same 1m bar", "fills at next 1m open +/- slippage",
             f"P&L in rupees at {cfg.point_value} per index point (futures-like proxy, NOT option P&L)"]
    if research_mode:
        notes.append("research mode: strategies simulated independently (one open trade per strategy)")
    if not sessions:
        return BacktestResult(pd.DataFrame(columns=EMPTY_COLS), 0, 0, 0, cfg, notes + ["no decision sessions after warm-up"])
    workers = max(1, min(workers, len(sessions) // 5 or 1))
    if workers == 1:
        rows, n_cand = _simulate(bars, cfg, sessions, research_mode, progress, log_every)
    else:
        chunks = [list(c) for c in np.array_split(np.array(sessions, dtype=object), workers)]
        # each worker needs history before its first session: pass bars from 30 calendar days earlier to its last session
        jobs = []
        for ch in chunks:
            lo = ch[0] - timedelta(days=45)
            sub = bars[(bars["ts"].dt.date >= lo) & (bars["ts"].dt.date <= ch[-1])]
            jobs.append((sub, cfg, ch, research_mode))
        log.info("backtest_parallel", workers=workers, sessions=len(sessions))
        t0 = _clock.monotonic()
        rows, n_cand, done = [], 0, 0
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for r, n in ex.map(_worker, jobs):
                rows += r
                n_cand += n
                done += 1
                if progress:
                    progress(min(done * len(sessions) // workers, len(sessions)), len(sessions), len(rows))
        log.info("backtest_parallel_done", elapsed_s=round(_clock.monotonic() - t0), trades=len(rows))
        notes.append(f"simulated in {workers} parallel chunks")
    df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=EMPTY_COLS)
    if not df.empty:
        df = df.sort_values("entry_ts").reset_index(drop=True)
    return BacktestResult(df, n_cand, len(df), len(sessions), cfg, notes)


def _update_exit(t: Trade, ts: pd.Timestamp, high: float, low: float, close: float, cfg: BacktestConfig) -> None:
    if ts <= pd.Timestamp(t.entry_ts):
        return
    d = 1 if t.direction == "BULLISH" else -1
    t.bars_held += 1
    fav = (high - t.entry) if d > 0 else (t.entry - low)
    adv = (low - t.entry) if d > 0 else (t.entry - high)
    t.mfe, t.mae = max(t.mfe, float(fav)), min(t.mae, float(adv))
    hit_stop = low <= t.stop if d > 0 else high >= t.stop
    hit_t1 = high >= t.target1 if d > 0 else low <= t.target1
    if hit_stop:  # conservative: stop first
        t.exit, t.exit_reason = float(t.stop - d * cfg.slippage_points), "STOP"
    elif hit_t1:
        t.exit, t.exit_reason = float(t.target1 - d * cfg.slippage_points), "TARGET1"
    elif t.bars_held >= cfg.max_hold_bars:
        t.exit, t.exit_reason = float(close - d * cfg.slippage_points), "TIME"
    if t.exit is not None:
        t.exit_ts = ts.to_pydatetime()
