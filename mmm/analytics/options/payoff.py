"""Multi-leg option strategy payoff at expiry and today's mark-to-model, with breakevens and aggregate Greeks."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from mmm.analytics.options.pricing import bs_price, greeks


@dataclass
class Leg:
    kind: str            # CE / PE / FUT
    strike: float
    qty: int             # +lots buy, -lots sell
    premium: float       # entry premium (per unit)
    iv: float | None = None
    t: float | None = None
    lot_size: int = 75


@dataclass
class PayoffResult:
    grid: pd.DataFrame
    max_profit: float
    max_loss: float
    breakevens: list[float]
    net_premium: float   # +credit / -debit (total, incl. lot size)
    agg_delta: float
    agg_gamma: float
    agg_theta: float
    agg_vega: float
    notes: list[str] = field(default_factory=list)


def _leg_expiry_pnl(leg: Leg, s: np.ndarray) -> np.ndarray:
    if leg.kind == "CE":
        intrinsic = np.maximum(s - leg.strike, 0)
    elif leg.kind == "PE":
        intrinsic = np.maximum(leg.strike - s, 0)
    else:
        intrinsic = s - leg.strike  # futures: strike field holds the entry price
    return (intrinsic - leg.premium) * leg.qty * leg.lot_size


def _leg_today_pnl(leg: Leg, s: np.ndarray, t_remaining: float) -> np.ndarray | None:
    if leg.kind == "FUT":
        return (s - leg.strike) * leg.qty * leg.lot_size
    if leg.iv is None or not np.isfinite(leg.iv):
        return None
    vals = np.array([bs_price(x, leg.strike, t_remaining, leg.iv, leg.kind) for x in s])
    return (vals - leg.premium) * leg.qty * leg.lot_size


def payoff(legs: list[Leg], spot: float, width_pct: float = 6.0, points: int = 241, days_forward: float = 0.0) -> PayoffResult:
    s = np.linspace(spot * (1 - width_pct / 100), spot * (1 + width_pct / 100), points)
    expiry = np.zeros_like(s)
    today = np.zeros_like(s)
    today_ok = True
    notes: list[str] = []
    d = g = th = v = 0.0
    for leg in legs:
        expiry += _leg_expiry_pnl(leg, s)
        t_rem = max((leg.t or 0) - days_forward / 365, 1e-6)
        tp = _leg_today_pnl(leg, s, t_rem)
        if tp is None:
            today_ok = False
        else:
            today += tp
        if leg.kind in ("CE", "PE") and leg.iv is not None and np.isfinite(leg.iv) and leg.t:
            gk = greeks(spot, leg.strike, leg.t, leg.iv, leg.kind)
            m = leg.qty * leg.lot_size
            d += gk.delta * m; g += gk.gamma * m; th += gk.theta * m; v += gk.vega * m
        elif leg.kind == "FUT":
            d += leg.qty * leg.lot_size
    if not today_ok:
        notes.append("T+0 curve omitted: some legs lack IV")
    grid = pd.DataFrame({"spot": s, "expiry_pnl": expiry, "t0_pnl": today if today_ok else np.nan})
    net_prem = -sum(leg.premium * leg.qty * leg.lot_size for leg in legs if leg.kind != "FUT")
    # breakevens: sign changes of expiry payoff
    bes: list[float] = []
    sign = np.sign(expiry)
    for i in range(1, len(s)):
        if sign[i] != sign[i - 1] and sign[i] != 0:
            x0, x1, y0, y1 = s[i - 1], s[i], expiry[i - 1], expiry[i]
            bes.append(float(x0 - y0 * (x1 - x0) / (y1 - y0)) if y1 != y0 else float(x0))
    unbounded_up = any((leg.kind == "CE" and leg.qty > 0) or (leg.kind == "FUT" and leg.qty > 0) for leg in legs) and expiry[-1] > expiry[-2]
    unbounded_dn = any((leg.kind == "PE" and leg.qty > 0) or (leg.kind == "FUT" and leg.qty < 0) for leg in legs) and expiry[0] > expiry[1]
    max_p = float("inf") if (unbounded_up or unbounded_dn) else float(expiry.max())
    short_up = expiry[-1] < expiry[-2]
    short_dn = expiry[0] < expiry[1]
    max_l = float("-inf") if (short_up or short_dn) and (expiry.min() == expiry[0] or expiry.min() == expiry[-1]) else float(expiry.min())
    return PayoffResult(grid, max_p, max_l, [round(b, 1) for b in bes], net_prem, d, g, th, v, notes)
