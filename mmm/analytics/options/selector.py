"""Option contract selection for a directional NIFTY view. Direction comes from price action first;
this module only answers "is there a suitable contract, and which?" with a transparent score."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from mmm.settings import get_config


@dataclass
class OptionCandidate:
    tradingsymbol: str
    expiry: date
    strike: float
    option_type: str
    premium: float
    bid: float | None
    ask: float | None
    spread_pct: float | None
    iv: float | None
    delta: float | None
    theta: float | None
    gamma: float | None
    oi: int
    volume: int
    liquidity_score: float
    score: float
    expected_premium_at_target: float | None
    expected_premium_at_stop: float | None
    reward_risk_premium: float | None
    reasons: list[str] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["expiry"] = str(self.expiry)
        return d


def _premium_at(spot_target: float, row: pd.Series, hold_days: float) -> float | None:
    """Re-price the contract at a hypothetical underlying level with the same IV, after `hold_days` theta."""
    from mmm.analytics.options.pricing import bs_price

    if not np.isfinite(row.get("iv", np.nan)):
        return None
    t = max(row["t"] - hold_days / 365.0, 1e-6)
    return float(bs_price(spot_target, row["strike"], t, row["iv"], row["option_type"]))


def rank_options(
    enriched: pd.DataFrame,
    direction: int,
    spot: float,
    target: float | None = None,
    stop: float | None = None,
    hold_days: float = 0.25,
    delta_band: tuple[float, float] = (0.35, 0.65),
    top_n: int = 5,
) -> list[OptionCandidate]:
    """Rank CE (direction>0) or PE (direction<0) contracts.

    Score (0-100): liquidity 35, delta fit 25, theta burden 15, spread 15, premium efficiency 10.
    Hard filters: min OI/volume, max spread, delta inside band, positive mid.
    """
    cfg = get_config().risk
    kind = "CE" if direction > 0 else "PE"
    df = enriched[(enriched["option_type"] == kind) & enriched["mid"].notna()].copy()
    out: list[OptionCandidate] = []
    for _, r in df.iterrows():
        rejections: list[str] = []
        reasons: list[str] = []
        d = abs(r["delta"]) if np.isfinite(r.get("delta", np.nan)) else None
        if r["oi"] is None or r["oi"] < cfg.min_option_oi:
            rejections.append(f"OI {int(r['oi'] or 0):,} < {cfg.min_option_oi:,}")
        if (r["volume"] or 0) < cfg.min_option_volume:
            rejections.append(f"volume {int(r['volume'] or 0):,} < {cfg.min_option_volume:,}")
        if np.isfinite(r["spread_pct"]) and r["spread_pct"] > cfg.max_option_spread_pct:
            rejections.append(f"spread {r['spread_pct']:.2f}% > {cfg.max_option_spread_pct}%")
        if d is None:
            rejections.append("no IV/delta solution (illiquid or stale quote)")
        elif not (delta_band[0] <= d <= delta_band[1]):
            rejections.append(f"delta {d:.2f} outside {delta_band}")

        liq = float(r["liquidity_score"]) if np.isfinite(r["liquidity_score"]) else 0.0
        delta_fit = 1 - min(abs((d or 0.5) - 0.5) / 0.25, 1)
        theta_burden = abs(r["theta"]) / r["mid"] if (np.isfinite(r.get("theta", np.nan)) and r["mid"]) else 0.1
        theta_score = float(np.clip(1 - theta_burden / 0.15, 0, 1))  # 15%/day theta -> 0
        spread_score = float(np.clip(1 - (r["spread_pct"] if np.isfinite(r["spread_pct"]) else 3) / 3, 0, 1))
        prem_eff = float(np.clip(1 - r["mid"] / (spot * 0.02), 0, 1))  # cheaper than 2% of spot preferred
        score = 35 * liq / 100 + 25 * delta_fit + 15 * theta_score + 15 * spread_score + 10 * prem_eff
        if rejections:
            score *= 0.3
        else:
            reasons.append(f"delta {d:.2f} in band, liquidity {liq:.0f}/100")
        if theta_burden > 0.1:
            reasons.append(f"theta burden high ({theta_burden*100:.1f}%/day)")
        p_t = _premium_at(target, r, hold_days) if target else None
        p_s = _premium_at(stop, r, hold_days) if stop else None
        rr = None
        if p_t is not None and p_s is not None and r["mid"] - p_s > 0:
            rr = (p_t - r["mid"]) / (r["mid"] - p_s)
        out.append(
            OptionCandidate(
                r["tradingsymbol"], r["expiry"], float(r["strike"]), kind, float(r["mid"]), r.get("bid"), r.get("ask"),
                float(r["spread_pct"]) if np.isfinite(r["spread_pct"]) else None,
                float(r["iv"]) if np.isfinite(r["iv"]) else None, r.get("delta"), r.get("theta"), r.get("gamma"),
                int(r["oi"] or 0), int(r["volume"] or 0), liq, round(float(score), 1), p_t, p_s, rr, reasons, rejections,
            )
        )
    out.sort(key=lambda c: -c.score)
    return out[:top_n]


def best_option(cands: list[OptionCandidate]) -> OptionCandidate | None:
    """First candidate without hard rejections, else None (NO SUITABLE OPTION)."""
    for c in cands:
        if not c.rejections:
            return c
    return None
