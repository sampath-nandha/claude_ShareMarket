"""Performance statistics with honest uncertainty: win rate with Wilson / bootstrap CIs, expectancy, profit factor,
drawdown, and the strategy acceptance gate (>51% lower confidence bound, >=200 trades, PF>1, positive expectancy)."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.stats import norm

from mmm.settings import get_config


@dataclass
class Stats:
    n: int
    wins: int
    losses: int
    breakeven: int
    win_rate: float | None
    win_rate_ci_low: float | None       # one-sided 95% Wilson lower bound
    win_rate_ci_high: float | None
    boot_ci_low: float | None           # session-block bootstrap lower bound
    gross_pnl: float
    net_pnl: float
    costs: float
    expectancy: float | None            # net per trade
    profit_factor: float | None
    avg_win: float | None
    avg_loss: float | None
    max_drawdown: float
    sharpe_per_trade: float | None
    verdict: str                        # ACCEPTED / INSUFFICIENT_EVIDENCE / REJECTED
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


def wilson_interval(wins: int, n: int, conf: float = 0.95) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    z = norm.ppf(1 - (1 - conf) / 2)
    p = wins / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    half = z * np.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return max(centre - half, 0.0), min(centre + half, 1.0)


def wilson_lower_one_sided(wins: int, n: int, conf: float = 0.95) -> float:
    if n == 0:
        return 0.0
    z = norm.ppf(conf)
    p = wins / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    half = z * np.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return max(centre - half, 0.0)


def block_bootstrap_win_rate(trades: pd.DataFrame, iters: int = 2000, conf: float = 0.95, seed: int = 7) -> float | None:
    """Resample whole sessions (trades within a day are dependent) and return the lower (1-conf) quantile."""
    if trades.empty or "session" not in trades:
        return None
    rng = np.random.default_rng(seed)
    groups = [g["win"].to_numpy() for _, g in trades.groupby("session")]
    if len(groups) < 5:
        return None
    rates = []
    for _ in range(iters):
        pick = rng.integers(0, len(groups), len(groups))
        sample = np.concatenate([groups[i] for i in pick])
        rates.append(sample.mean())
    return float(np.quantile(rates, 1 - conf))


def max_drawdown(pnl: pd.Series) -> float:
    eq = pnl.cumsum()
    return float((eq - eq.cummax()).min()) if len(eq) else 0.0


def compute_stats(trades: pd.DataFrame) -> Stats:
    """trades columns: pnl_net, pnl_gross, costs, session (date). Break-even stays in the denominator."""
    v = get_config().validation
    n = len(trades)
    if n == 0:
        return Stats(0, 0, 0, 0, None, None, None, None, 0, 0, 0, None, None, None, None, 0, None, "INSUFFICIENT_EVIDENCE", ["no trades"])
    t = trades.copy()
    t["win"] = t["pnl_net"] > 0
    wins = int(t["win"].sum())
    losses = int((t["pnl_net"] < 0).sum())
    be = n - wins - losses
    wr = wins / n
    lo, hi = wilson_interval(wins, n, v.confidence_level)
    lo1 = wilson_lower_one_sided(wins, n, v.confidence_level)
    boot = block_bootstrap_win_rate(t, v.bootstrap_iterations, v.confidence_level)
    gp = float(t.loc[t["pnl_net"] > 0, "pnl_net"].sum())
    gl = float(-t.loc[t["pnl_net"] < 0, "pnl_net"].sum())
    pf = (gp / gl) if gl > 0 else (float("inf") if gp > 0 else None)
    exp = float(t["pnl_net"].mean())
    std = float(t["pnl_net"].std(ddof=1)) if n > 1 else 0.0
    sharpe = exp / std if std > 0 else None
    reasons: list[str] = []
    if n < v.min_trades_per_strategy:
        reasons.append(f"n={n} < {v.min_trades_per_strategy} minimum sample")
    lb = min(lo1, boot) if boot is not None else lo1
    if lb <= v.target_win_rate:
        reasons.append(f"95% lower bound of win rate {lb:.1%} <= {v.target_win_rate:.0%}")
    if exp <= 0:
        reasons.append(f"non-positive expectancy ({exp:,.0f}/trade)")
    if pf is not None and pf <= 1:
        reasons.append(f"profit factor {pf:.2f} <= 1")
    if n < v.min_trades_per_strategy:
        verdict = "INSUFFICIENT_EVIDENCE"
    elif reasons:
        verdict = "REJECTED"
    else:
        verdict = "ACCEPTED"
    return Stats(
        n, wins, losses, be, wr, lo1, hi, boot, float(t["pnl_gross"].sum()), float(t["pnl_net"].sum()),
        float(t["costs"].sum()), exp, pf, float(t.loc[t["pnl_net"] > 0, "pnl_net"].mean()) if wins else None,
        float(t.loc[t["pnl_net"] < 0, "pnl_net"].mean()) if losses else None, max_drawdown(t["pnl_net"]), sharpe, verdict, reasons,
    )


def breakdown(trades: pd.DataFrame, by: str) -> pd.DataFrame:
    if trades.empty or by not in trades:
        return pd.DataFrame()
    rows = []
    for key, g in trades.groupby(by):
        s = compute_stats(g)
        rows.append({by: key, "n": s.n, "win_rate": s.win_rate, "net_pnl": s.net_pnl, "expectancy": s.expectancy,
                     "profit_factor": s.profit_factor, "max_dd": s.max_drawdown})
    return pd.DataFrame(rows)


def cost_sensitivity(trades: pd.DataFrame, multipliers: tuple[float, ...] = (0.5, 1.0, 1.5, 2.0, 3.0)) -> pd.DataFrame:
    rows = []
    for m in multipliers:
        t = trades.copy()
        t["pnl_net"] = t["pnl_gross"] - t["costs"] * m
        s = compute_stats(t)
        rows.append({"cost_multiplier": m, "win_rate": s.win_rate, "net_pnl": s.net_pnl, "expectancy": s.expectancy, "verdict": s.verdict})
    return pd.DataFrame(rows)
