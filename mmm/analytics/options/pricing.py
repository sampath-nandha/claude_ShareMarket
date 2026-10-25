"""Black-Scholes(-Merton) pricing, implied volatility and Greeks for European index options.

Modelling assumptions (documented, configurable in settings.yaml -> pricing):
  * Underlying reference: NIFTY spot (index). Futures-based (Black-76) is available via `use_forward`.
  * Risk-free rate r and dividend yield q are configured constants, not market data.
  * Expiry at 15:30 IST on the expiry date; ACT/365 calendar time.
  * IV solved by Brent's method on [iv_min, iv_max]; prices outside no-arbitrage bounds return NaN.
Everything here is DERIVED and must be labelled so in the UI.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.optimize import brentq
from scipy.stats import norm

from mmm.settings import get_config


def _d1d2(s: float, k: float, t: float, r: float, q: float, sigma: float) -> tuple[float, float]:
    vs = sigma * math.sqrt(t)
    d1 = (math.log(s / k) + (r - q + 0.5 * sigma**2) * t) / vs
    return d1, d1 - vs


def bs_price(s: float, k: float, t: float, sigma: float, kind: str, r: float | None = None, q: float | None = None) -> float:
    cfg = get_config().pricing
    r = cfg.risk_free_rate if r is None else r
    q = cfg.dividend_yield if q is None else q
    if t <= 0 or sigma <= 0 or s <= 0 or k <= 0:
        intrinsic = max(s - k, 0.0) if kind == "CE" else max(k - s, 0.0)
        return intrinsic
    d1, d2 = _d1d2(s, k, t, r, q, sigma)
    if kind == "CE":
        return s * math.exp(-q * t) * norm.cdf(d1) - k * math.exp(-r * t) * norm.cdf(d2)
    return k * math.exp(-r * t) * norm.cdf(-d2) - s * math.exp(-q * t) * norm.cdf(-d1)


def implied_vol(price: float, s: float, k: float, t: float, kind: str, r: float | None = None, q: float | None = None) -> float:
    """Return IV or NaN when no solution exists (price below intrinsic / above bounds / illiquid)."""
    cfg = get_config().pricing
    r = cfg.risk_free_rate if r is None else r
    q = cfg.dividend_yield if q is None else q
    if price is None or not np.isfinite(price) or price <= 0 or t <= 0 or s <= 0 or k <= 0:
        return float("nan")
    disc_k, disc_s = k * math.exp(-r * t), s * math.exp(-q * t)
    lower = max(disc_s - disc_k, 0.0) if kind == "CE" else max(disc_k - disc_s, 0.0)
    upper = disc_s if kind == "CE" else disc_k
    if price <= lower + 1e-9 or price >= upper - 1e-9:
        return float("nan")
    f = lambda sig: bs_price(s, k, t, sig, kind, r, q) - price  # noqa: E731
    try:
        lo, hi = cfg.iv_min, cfg.iv_max
        if f(lo) > 0 or f(hi) < 0:
            return float("nan")
        return float(brentq(f, lo, hi, xtol=1e-6, maxiter=200))
    except (ValueError, RuntimeError):
        return float("nan")


@dataclass(frozen=True)
class Greeks:
    iv: float
    delta: float
    gamma: float
    theta: float      # per calendar day, in premium points
    vega: float       # per 1 vol point (1%)
    intrinsic: float
    time_value: float
    moneyness: float  # ln(S/K) for CE, ln(K/S) for PE (positive = ITM)

    def as_dict(self) -> dict[str, float]:
        return {k: (round(v, 4) if isinstance(v, float) and np.isfinite(v) else v) for k, v in self.__dict__.items()}


NAN_GREEKS = Greeks(*([float("nan")] * 8))


def greeks(s: float, k: float, t: float, sigma: float, kind: str, r: float | None = None, q: float | None = None) -> Greeks:
    cfg = get_config().pricing
    r = cfg.risk_free_rate if r is None else r
    q = cfg.dividend_yield if q is None else q
    intrinsic = max(s - k, 0.0) if kind == "CE" else max(k - s, 0.0)
    if not np.isfinite(sigma) or sigma <= 0 or t <= 0:
        return NAN_GREEKS
    d1, d2 = _d1d2(s, k, t, r, q, sigma)
    sq_t = math.sqrt(t)
    pdf = norm.pdf(d1)
    eq, er = math.exp(-q * t), math.exp(-r * t)
    if kind == "CE":
        delta = eq * norm.cdf(d1)
        theta = (-(s * eq * pdf * sigma) / (2 * sq_t) - r * k * er * norm.cdf(d2) + q * s * eq * norm.cdf(d1)) / 365
    else:
        delta = -eq * norm.cdf(-d1)
        theta = (-(s * eq * pdf * sigma) / (2 * sq_t) + r * k * er * norm.cdf(-d2) - q * s * eq * norm.cdf(-d1)) / 365
    gamma = eq * pdf / (s * sigma * sq_t)
    vega = s * eq * pdf * sq_t / 100
    price = bs_price(s, k, t, sigma, kind, r, q)
    money = math.log(s / k) if kind == "CE" else math.log(k / s)
    return Greeks(sigma, delta, gamma, theta, vega, intrinsic, max(price - intrinsic, 0.0), money)


def expected_move_from_straddle(straddle_price: float) -> float:
    """Rule of thumb: ATM straddle ~= 0.8 * 1-sigma expected move to expiry. Labelled as heuristic."""
    return straddle_price / 0.8


def expected_move_from_iv(s: float, iv: float, t: float) -> float:
    return s * iv * math.sqrt(t)
