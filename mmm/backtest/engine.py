"""Event-driven backtest over 1-minute NIFTY candles using the same strategy code as live detection.

Execution model (conservative, disclosed):
  * Decisions are made at setup-TF bar closes; entry fills at the next 1-minute bar's open +/- slippage.
  * Exits are evaluated on 1-minute bars. If a bar touches both stop and target, the STOP is assumed first.
  * Forced exit at the configured session cut-off (default 15:20) at that bar's close.
  * Index-point P&L is converted to rupees via a configurable point value (default: NIFTY lot 75,
    i.e. a futures-like proxy). Option-execution P&L is a separate dimension and is NOT what this reports.
  * Costs: configurable per-trade rupee cost + slippage; the default mirrors Zerodha futures-like costs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import pandas as pd
import structlog

from mmm.analytics.candles import MINUTES
from mmm.settings import get_config
from mmm.signals.engine import build_context, stage1
from mmm.strategies import Strategy, load_strategies

log = structlog.get_logger(__name__)


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


def run_backtest(minute_bars: pd.DataFrame, cfg: BacktestConfig | None = None) -> BacktestResult:
    cfg = cfg or BacktestConfig()
    strategies: list[Strategy] = load_strategies(cfg.strategies or get_config().strategies.enabled)
    bars = minute_bars.sort_values("ts").reset_index(drop=True)
    if cfg.start:
        bars = bars[bars["ts"].dt.date >= cfg.start]
    if cfg.end:
        bars = bars[bars["ts"].dt.date <= cfg.end]
    bars = bars.reset_index(drop=True)
    sessions = sorted(bars["ts"].dt.date.unique())
    step = MINUTES[cfg.setup_tf]
    trades: list[Trade] = []
    n_cand = 0
    open_trade: Trade | None = None
    # warm-up: need enough history before first decision
    first_decision_day = sessions[min(len(sessions) - 1, 10)] if sessions else None
    for day in sessions:
        if first_decision_day is None or day < first_decision_day:
            continue
        day_bars = bars[bars["ts"].dt.date == day].reset_index(drop=True)
        if day_bars.empty:
            continue
        # decision times: setup-TF closes from 09:15+step up to last_entry
        t0 = day_bars["ts"].iloc[0].replace(hour=9, minute=15)
        decision_times = [t0 + timedelta(minutes=step * k) for k in range(1, 400) if (t0 + timedelta(minutes=step * k)).time() <= cfg.last_entry]
        # iterate minute bars for exits; decisions at boundaries
        for i, row in day_bars.iterrows():
            ts = row["ts"]
            if open_trade is not None:
                _update_exit(open_trade, row, cfg)
                if open_trade.exit is not None:
                    trades.append(open_trade)
                    open_trade = None
            if ts.time() >= cfg.force_exit and open_trade is not None and open_trade.exit is None:
                open_trade.exit, open_trade.exit_ts, open_trade.exit_reason = float(row["close"]), ts.to_pydatetime(), "SESSION_END"
                trades.append(open_trade)
                open_trade = None
            # decision at this minute's close if it closes a setup bar
            boundary = ts + timedelta(minutes=1)
            if boundary in decision_times and (open_trade is None or cfg.allow_overlap):
                hist = bars[bars["ts"] <= ts]
                ctx = build_context(hist, boundary.to_pydatetime(), setup_tf=cfg.setup_tf)
                if ctx is None:
                    continue
                cands = stage1(ctx, strategies)
                n_cand += len(cands)
                if not cands:
                    continue
                strat, cand = max(cands, key=lambda sc: sc[1].pa_score)
                nxt = day_bars.iloc[i + 1] if i + 1 < len(day_bars) else None
                if nxt is None:
                    continue
                d = 1 if cand.direction == "BULLISH" else -1
                fill = float(nxt["open"]) + d * cfg.slippage_points
                open_trade = Trade(strat.id, cand.direction, boundary.to_pydatetime(), nxt["ts"].to_pydatetime(), fill, cand.stop,
                                   cand.target1, cand.target2, regime=ctx.regime.code, trigger=cand.trigger_event, pa_score=cand.pa_score,
                                   coincident=",".join(sc[0].id for sc in cands if sc[0].id != strat.id))
        if open_trade is not None and open_trade.exit is None:  # safety: close at last bar
            last = day_bars.iloc[-1]
            open_trade.exit, open_trade.exit_ts, open_trade.exit_reason = float(last["close"]), last["ts"].to_pydatetime(), "SESSION_END"
            trades.append(open_trade)
            open_trade = None
    df = pd.DataFrame([t.to_row(cfg) for t in trades]) if trades else pd.DataFrame(
        columns=["strategy_id", "direction", "pnl_gross", "costs", "pnl_net", "session"])
    notes = ["stop assumed first when stop and target touched in the same 1m bar", "fills at next 1m open +/- slippage",
             f"P&L in rupees at {cfg.point_value} per index point (futures-like proxy, NOT option P&L)"]
    return BacktestResult(df, n_cand, len(trades), len(sessions), cfg, notes)


def _update_exit(t: Trade, row: pd.Series, cfg: BacktestConfig) -> None:
    if row["ts"] <= pd.Timestamp(t.entry_ts):
        return
    d = 1 if t.direction == "BULLISH" else -1
    t.bars_held += 1
    fav = d * (row["high"] - t.entry) if d > 0 else d * (row["low"] - t.entry)
    adv = d * (row["low"] - t.entry) if d > 0 else d * (row["high"] - t.entry)
    t.mfe, t.mae = max(t.mfe, float(fav)), min(t.mae, float(adv))
    hit_stop = row["low"] <= t.stop if d > 0 else row["high"] >= t.stop
    hit_t1 = row["high"] >= t.target1 if d > 0 else row["low"] <= t.target1
    if hit_stop:  # conservative: stop first
        t.exit, t.exit_reason = float(t.stop - d * cfg.slippage_points), "STOP"
    elif hit_t1:
        t.exit, t.exit_reason = float(t.target1 - d * cfg.slippage_points), "TARGET1"
    elif t.bars_held >= cfg.max_hold_bars:
        t.exit, t.exit_reason = float(row["close"] - d * cfg.slippage_points), "TIME"
    if t.exit is not None:
        t.exit_ts = row["ts"].to_pydatetime()
