"""Application settings: secrets from env / Credential Manager, everything else from config/settings.yaml."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"


class Secrets(BaseSettings):
    """Secrets come from environment variables / .env (prefix MMM_) or Credential Manager (see broker.auth)."""

    model_config = SettingsConfigDict(env_prefix="MMM_", env_file=PROJECT_ROOT / ".env", extra="ignore")

    kite_api_key: str = ""
    kite_api_secret: str = ""
    kite_redirect_url: str = "http://127.0.0.1:5010/callback"
    data_dir: Path = PROJECT_ROOT / "data"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


class UniverseCfg(BaseModel):
    index_symbol: str = "NIFTY 50"
    index_exchange: str = "NSE"
    vix_symbol: str = "INDIA VIX"
    fno_exchange: str = "NFO"
    fno_name: str = "NIFTY"
    futures_contracts: int = 2
    option_expiries: int = 2
    option_strikes_each_side: int = 12
    strike_step: int = 50
    constituents: list[str] = Field(default_factory=list)


class SessionCfg(BaseModel):
    timezone: str = "Asia/Kolkata"
    open: str = "09:15"
    close: str = "15:30"
    collector_start: str = "09:00"
    collector_stop: str = "15:40"
    opening_range_minutes: list[int] = Field(default_factory=lambda: [5, 15, 30])


class HistoricalCfg(BaseModel):
    intervals: list[str] = Field(default_factory=lambda: ["minute", "5minute", "15minute", "60minute", "day"])
    max_days_per_request: dict[str, int] = Field(default_factory=dict)
    requests_per_second: float = 3
    backfill_years_index: int = 5
    backfill_years_stocks: int = 2


class PricingCfg(BaseModel):
    risk_free_rate: float = 0.065
    dividend_yield: float = 0.0
    expiry_time: str = "15:30"
    iv_min: float = 0.01
    iv_max: float = 3.0


class RiskCfg(BaseModel):
    account_capital: float = 500_000
    max_risk_per_trade_pct: float = 1.0
    max_daily_loss_pct: float = 3.0
    max_consecutive_losses: int = 3
    max_daily_recommendations: int = 6
    max_concurrent_positions: int = 2
    min_reward_risk: float = 1.5
    max_option_spread_pct: float = 1.5
    min_option_oi: int = 50_000
    min_option_volume: int = 10_000
    max_signal_age_minutes: int = 10
    min_data_quality: str = "PARTIAL"
    nifty_lot_size: int = 75


class CostCfg(BaseModel):
    brokerage_per_order: float = 20.0
    stt_sell_pct: float = 0.1
    exchange_txn_pct: float = 0.03503
    sebi_pct: float = 0.0001
    stamp_buy_pct: float = 0.003
    gst_pct: float = 18.0
    slippage_ticks: int = 2
    tick_size: float = 0.05


class ScoringThresholds(BaseModel):
    watch: float = 40
    valid: float = 55
    strong: float = 70
    high_conviction: float = 82


class ScoringCfg(BaseModel):
    price_action: float = 35
    mtf_alignment: float = 15
    futures: float = 15
    options: float = 15
    breadth: float = 10
    liquidity: float = 10
    thresholds: ScoringThresholds = Field(default_factory=ScoringThresholds)


class ValidationCfg(BaseModel):
    min_trades_per_strategy: int = 200
    confidence_level: float = 0.95
    target_win_rate: float = 0.51
    bootstrap_iterations: int = 2000


class StrategiesCfg(BaseModel):
    enabled: list[str] = Field(default_factory=lambda: ["PA01", "PA02", "PA03", "PA04", "PA05"])
    setup_timeframe: str = "5minute"
    context_timeframe: str = "15minute"
    trend_timeframe: str = "60minute"
    entry_timeframe: str = "minute"


class AppConfig(BaseModel):
    universe: UniverseCfg = Field(default_factory=UniverseCfg)
    session: SessionCfg = Field(default_factory=SessionCfg)
    historical: HistoricalCfg = Field(default_factory=HistoricalCfg)
    pricing: PricingCfg = Field(default_factory=PricingCfg)
    risk: RiskCfg = Field(default_factory=RiskCfg)
    costs: CostCfg = Field(default_factory=CostCfg)
    scoring: ScoringCfg = Field(default_factory=ScoringCfg)
    validation: ValidationCfg = Field(default_factory=ValidationCfg)
    strategies: StrategiesCfg = Field(default_factory=StrategiesCfg)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@lru_cache(maxsize=1)
def get_config(path: Path | None = None) -> AppConfig:
    return AppConfig.model_validate(_load_yaml(path or CONFIG_DIR / "settings.yaml"))


@lru_cache(maxsize=1)
def get_secrets() -> Secrets:
    return Secrets()


@lru_cache(maxsize=1)
def get_holidays() -> set[str]:
    raw = _load_yaml(CONFIG_DIR / "market_holidays.yaml").get("holidays", {})
    out: set[str] = set()
    for _year, days in raw.items():
        for d in days or []:
            out.add(str(d))
    return out
