"""Equal-weight constituent breadth. Kite does not expose index weights, so everything here is EQUAL-WEIGHT
and labelled as such; authoritative breadth claims are suppressed when coverage is incomplete."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Breadth:
    coverage_pct: float
    n: int
    advancing: int
    declining: int
    unchanged: int
    pct_above_prev_close: float | None
    pct_above_open: float | None
    pct_at_day_high: float | None
    pct_at_day_low: float | None
    mean_return_pct: float | None
    median_return_pct: float | None
    dispersion_pct: float | None
    score: float             # -1..+1
    authoritative: bool
    table: pd.DataFrame

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d.pop("table")
        return d


def compute_breadth(quotes: pd.DataFrame, universe_size: int, min_coverage: float = 0.8) -> Breadth:
    """quotes: one row per constituent with last_price, close (prev close), open, high, low."""
    q = quotes.dropna(subset=["last_price", "close"]).copy()
    q = q[q["close"] > 0]
    n = len(q)
    cov = n / universe_size * 100 if universe_size else 0.0
    if n == 0:
        return Breadth(cov, 0, 0, 0, 0, None, None, None, None, None, None, None, 0.0, False, q)
    q["ret_pct"] = (q["last_price"] / q["close"] - 1) * 100
    q["from_open_pct"] = np.where(q["open"] > 0, (q["last_price"] / q["open"] - 1) * 100, np.nan)
    adv = int((q["ret_pct"] > 0).sum())
    dec = int((q["ret_pct"] < 0).sum())
    unch = n - adv - dec
    tol = 0.0005
    at_high = float((q["last_price"] >= q["high"] * (1 - tol)).mean() * 100) if "high" in q else None
    at_low = float((q["last_price"] <= q["low"] * (1 + tol)).mean() * 100) if "low" in q else None
    above_open = float((q["from_open_pct"] > 0).mean() * 100) if q["from_open_pct"].notna().any() else None
    mean_r, med_r, disp = float(q["ret_pct"].mean()), float(q["ret_pct"].median()), float(q["ret_pct"].std(ddof=0))
    score = float(np.clip((adv - dec) / n, -1, 1)) if n else 0.0
    return Breadth(
        cov, n, adv, dec, unch, float(adv / n * 100), above_open, at_high, at_low, mean_r, med_r, disp, score,
        cov >= min_coverage * 100, q.sort_values("ret_pct", ascending=False),
    )


def relative_strength(stock_ret_pct: pd.Series, index_ret_pct: float) -> pd.Series:
    return stock_ret_pct - index_ret_pct
