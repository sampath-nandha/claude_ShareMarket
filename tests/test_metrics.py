from datetime import date, timedelta

import numpy as np
import pandas as pd

from mmm.backtest.metrics import compute_stats, cost_sensitivity, wilson_interval, wilson_lower_one_sided


def _trades(n: int, p_win: float, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    win = rng.random(n) < p_win
    gross = np.where(win, rng.uniform(800, 2500, n), -rng.uniform(600, 1500, n))
    costs = np.full(n, 90.0)
    sess = [date(2025, 1, 1) + timedelta(days=i // 3) for i in range(n)]
    return pd.DataFrame({"pnl_gross": gross, "costs": costs, "pnl_net": gross - costs, "session": sess})


def test_wilson_bounds():
    lo, hi = wilson_interval(52, 100)
    assert lo < 0.52 < hi
    assert wilson_lower_one_sided(52, 100) < 0.51  # 52/100 is NOT evidence of >51%
    assert wilson_lower_one_sided(560, 1000) > 0.51


def test_small_sample_is_insufficient_evidence():
    s = compute_stats(_trades(50, 0.7))
    assert s.verdict == "INSUFFICIENT_EVIDENCE"
    assert s.n == 50 and s.win_rate is not None


def test_breakeven_counted_in_denominator():
    df = _trades(10, 0.5)
    df.loc[0, ["pnl_gross", "pnl_net"]] = [90.0, 0.0]
    s = compute_stats(df)
    assert s.breakeven == 1 and s.wins + s.losses + s.breakeven == 10


def test_accept_and_reject_gates():
    good = compute_stats(_trades(600, 0.62, seed=2))
    assert good.verdict == "ACCEPTED", good.reasons
    bad = compute_stats(_trades(600, 0.50, seed=3))
    assert bad.verdict == "REJECTED" and bad.reasons
    sens = cost_sensitivity(_trades(300, 0.6))
    assert list(sens["cost_multiplier"]) == [0.5, 1.0, 1.5, 2.0, 3.0]
    assert sens["net_pnl"].is_monotonic_decreasing
