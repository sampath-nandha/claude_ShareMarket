"""Multi-factor signal scoring. Weights are research hypotheses from settings.yaml, not validated truths.
A score is NOT a probability; calibrated probabilities come from out-of-sample results only."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from mmm.settings import get_config


@dataclass
class Components:
    price_action: float          # 0..100 from the strategy
    mtf_alignment: float         # -1..+1
    futures: float               # -1..+1
    options: float               # -1..+1
    breadth: float               # -1..+1
    liquidity: float             # 0..1


def _unit(x: float) -> float:
    """Map -1..+1 to 0..100."""
    return float(np.clip((x + 1) / 2 * 100, 0, 100))


def score(c: Components) -> tuple[float, dict[str, float]]:
    w = get_config().scoring
    parts = {
        "price_action": c.price_action * w.price_action / 100,
        "mtf_alignment": _unit(c.mtf_alignment) * w.mtf_alignment / 100,
        "futures": _unit(c.futures) * w.futures / 100,
        "options": _unit(c.options) * w.options / 100,
        "breadth": _unit(c.breadth) * w.breadth / 100,
        "liquidity": float(np.clip(c.liquidity, 0, 1)) * 100 * w.liquidity / 100,
    }
    total = sum(parts.values())
    return round(float(np.clip(total, 0, 100)), 1), {k: round(v, 1) for k, v in parts.items()}


def classify(total: float, stage1_ok: bool, risk_ok: bool) -> str:
    th = get_config().scoring.thresholds
    if not stage1_ok or not risk_ok:
        return "NO_TRADE"
    if total >= th.high_conviction:
        return "HIGH_CONVICTION"
    if total >= th.strong:
        return "STRONG_SETUP"
    if total >= th.valid:
        return "VALID_SETUP"
    if total >= th.watch:
        return "WATCH"
    return "NO_TRADE"
