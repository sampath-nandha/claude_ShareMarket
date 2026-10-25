"""Configurable risk limits, position sizing and fail-safe checks. All limits come from settings.yaml -> risk."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from mmm.data.quality import DQ
from mmm.settings import get_config


@dataclass
class RiskState:
    """Running state for the current session (persisted by the caller)."""

    daily_pnl: float = 0.0
    consecutive_losses: int = 0
    recommendations_today: int = 0
    open_positions: int = 0
    net_direction: int = 0   # +1 long bias exposure count, -1 short


@dataclass
class RiskCheck:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    max_lots: int = 0
    rupee_risk: float = 0.0
    utilisation: dict[str, float] = field(default_factory=dict)


def position_size(premium_risk_per_unit: float, lot_size: int | None = None) -> tuple[int, float]:
    """Lots so that max premium loss <= max_risk_per_trade. Returns (lots, rupee_risk)."""
    cfg = get_config().risk
    lot = lot_size or cfg.nifty_lot_size
    budget = cfg.account_capital * cfg.max_risk_per_trade_pct / 100
    if premium_risk_per_unit <= 0:
        return 0, 0.0
    lots = int(budget // (premium_risk_per_unit * lot))
    return lots, lots * lot * premium_risk_per_unit


def check(
    state: RiskState,
    direction: int,
    reward_risk: float | None,
    premium_risk_per_unit: float | None,
    spread_pct: float | None,
    signal_ts: datetime,
    now: datetime,
    dq: DQ,
) -> RiskCheck:
    cfg = get_config().risk
    reasons: list[str] = []
    cap = cfg.account_capital
    daily_loss_limit = cap * cfg.max_daily_loss_pct / 100
    if state.daily_pnl <= -daily_loss_limit:
        reasons.append(f"daily loss limit hit ({state.daily_pnl:,.0f} <= -{daily_loss_limit:,.0f})")
    if state.consecutive_losses >= cfg.max_consecutive_losses:
        reasons.append(f"{state.consecutive_losses} consecutive losses >= {cfg.max_consecutive_losses}")
    if state.recommendations_today >= cfg.max_daily_recommendations:
        reasons.append("max daily recommendations reached")
    if state.open_positions >= cfg.max_concurrent_positions:
        reasons.append("max concurrent positions reached")
    if reward_risk is not None and reward_risk < cfg.min_reward_risk:
        reasons.append(f"reward/risk {reward_risk:.2f} < {cfg.min_reward_risk}")
    if spread_pct is not None and spread_pct > cfg.max_option_spread_pct:
        reasons.append(f"option spread {spread_pct:.2f}% > {cfg.max_option_spread_pct}%")
    age_min = (now - signal_ts).total_seconds() / 60
    if age_min > cfg.max_signal_age_minutes:
        reasons.append(f"signal age {age_min:.0f}m > {cfg.max_signal_age_minutes}m")
    if not dq.at_least(cfg.min_data_quality):
        reasons.append(f"data quality {dq.name} below {cfg.min_data_quality}")
    lots, rupee = (0, 0.0)
    if premium_risk_per_unit:
        lots, rupee = position_size(premium_risk_per_unit)
        if lots == 0:
            reasons.append("premium risk per lot exceeds per-trade budget")
    util = {
        "daily_loss_pct": min(max(-state.daily_pnl, 0) / daily_loss_limit * 100, 100) if daily_loss_limit else 0,
        "recommendations_pct": state.recommendations_today / cfg.max_daily_recommendations * 100,
        "positions_pct": state.open_positions / cfg.max_concurrent_positions * 100,
        "consecutive_losses_pct": state.consecutive_losses / cfg.max_consecutive_losses * 100,
    }
    return RiskCheck(not reasons, reasons, lots, rupee, util)
