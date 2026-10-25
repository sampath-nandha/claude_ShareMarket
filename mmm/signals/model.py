"""Signal schema (Part F04 of the spec). Every recommendation carries evidence, contradictions and a data-quality status."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Direction = Literal["BULLISH", "BEARISH"]
Status = Literal["NO_TRADE", "WATCH", "VALID_SETUP", "STRONG_SETUP", "HIGH_CONVICTION", "INVALIDATED", "EXPIRED"]


class SetupCandidate(BaseModel):
    """Output of Stage 1 (a strategy's deterministic price-action check)."""

    strategy_id: str
    direction: Direction
    timeframe: str
    trigger_event: str
    entry: float
    entry_zone: tuple[float, float] | None = None
    stop: float
    target1: float
    target2: float
    invalidation: str
    pa_score: float = Field(ge=0, le=100, description="price-action quality 0-100")
    evidence: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    expires_after_bars: int = 6
    bar_ts: datetime

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def reward_risk(self) -> float:
        return abs(self.target1 - self.entry) / self.risk if self.risk > 0 else 0.0


class Signal(BaseModel):
    signal_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: datetime
    underlying: str = "NIFTY 50"
    direction: Direction
    strategy_id: str
    regime: str
    timeframe: str
    trigger_event: str
    entry: float
    entry_zone: tuple[float, float] | None = None
    stop: float
    target1: float
    target2: float
    reward_risk: float
    invalidation: str
    score: float
    status: Status
    win_probability: float | None = None       # calibrated estimate; None until statistically supportable
    win_probability_note: str = "insufficient out-of-sample history for a calibrated estimate"
    evidence: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    futures_confirmation: dict[str, Any] | None = None
    options_confirmation: dict[str, Any] | None = None
    breadth_confirmation: dict[str, Any] | None = None
    liquidity: dict[str, Any] | None = None
    data_quality: str = "MISSING"
    recommended_option: dict[str, Any] | None = None
    option_alternatives: list[dict[str, Any]] = Field(default_factory=list)
    expected_spread_pct: float | None = None
    suggested_max_risk_rupees: float | None = None
    suggested_lots: int | None = None
    expiry_conditions: str = ""
    do_not_execute_reasons: list[str] = Field(default_factory=list)
    strategy_status: str = "EXPERIMENTAL"
    component_scores: dict[str, float] = Field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
