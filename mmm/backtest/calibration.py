"""Probability calibration: map a signal score to an out-of-sample win probability, with honest gating.

Fit ONLY on out-of-sample (walk-forward) trades. Isotonic regression (monotone, non-parametric) with a Platt
(logistic) fallback for small samples. The calibrator refuses to produce a number until `min_n` trades exist and
reports a reliability table + Brier score so the user can see how trustworthy it is.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import norm

from mmm.backtest.metrics import wilson_interval
from mmm.data.state_db import StateDB
from mmm.timeutil import now_ist

KEY = "calibrator"


@dataclass
class Calibrator:
    method: str                      # isotonic | platt | none
    n: int
    x: list[float] = field(default_factory=list)   # isotonic knots (scores)
    y: list[float] = field(default_factory=list)   # isotonic values (probabilities)
    a: float = 0.0                   # platt slope
    b: float = 0.0                   # platt intercept
    brier: float | None = None
    base_rate: float | None = None
    reliability: list[dict[str, Any]] = field(default_factory=list)
    fitted_at: str = ""
    min_n: int = 200
    scope: str = "pooled"            # pooled | strategy:PAxx

    @property
    def usable(self) -> bool:
        return self.method != "none" and self.n >= self.min_n

    def predict(self, score: float) -> tuple[float | None, str]:
        """Return (probability, note). None when not usable."""
        if not self.usable:
            return None, f"calibration not usable: n={self.n} < {self.min_n} out-of-sample trades"
        if self.method == "isotonic":
            p = float(np.interp(score, self.x, self.y))
        else:
            p = float(1 / (1 + np.exp(-(self.a * score + self.b))))
        p = float(np.clip(p, 0.01, 0.99))
        # approximate CI for the predicted probability from the bin it falls in
        bin_ = next((r for r in self.reliability if r["lo"] <= score < r["hi"]), None)
        note = f"{self.method} on n={self.n} OOS trades ({self.scope}), Brier {self.brier:.3f}" if self.brier is not None else f"{self.method} on n={self.n}"
        if bin_:
            note += f"; bin n={bin_['n']} observed {bin_['observed']:.0%} [{bin_['ci_lo']:.0%}-{bin_['ci_hi']:.0%}]"
        return p, note

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, s: str) -> "Calibrator":
        return cls(**json.loads(s))


def _pav(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Pool-adjacent-violators isotonic regression (increasing)."""
    order = np.argsort(x)
    xs, ys = x[order], y[order].astype(float)
    blocks = [[xs[i], xs[i], ys[i], 1] for i in range(len(xs))]  # [xmin, xmax, mean, count]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][2] > blocks[i + 1][2]:
            a_, b_ = blocks[i], blocks[i + 1]
            n = a_[3] + b_[3]
            blocks[i] = [a_[0], b_[1], (a_[2] * a_[3] + b_[2] * b_[3]) / n, n]
            del blocks[i + 1]
            i = max(i - 1, 0)
        else:
            i += 1
    knots_x, knots_y = [], []
    for xmin, xmax, m, _n in blocks:
        knots_x += [xmin, xmax]
        knots_y += [m, m]
    return np.array(knots_x), np.array(knots_y)


def fit(trades: pd.DataFrame, score_col: str = "score", win_col: str = "win", min_n: int = 200, bins: int = 5, scope: str = "pooled") -> Calibrator:
    t = trades.dropna(subset=[score_col]).copy()
    n = len(t)
    if n == 0:
        return Calibrator("none", 0, min_n=min_n, scope=scope, fitted_at=now_ist().isoformat())
    x = t[score_col].to_numpy(float)
    y = t[win_col].to_numpy(float)
    base = float(y.mean())
    if n >= min_n:
        kx, ky = _pav(x, y)
        method, kx_l, ky_l, a, b = "isotonic", kx.tolist(), ky.tolist(), 0.0, 0.0
        pred = np.interp(x, kx, ky)
    else:
        # Platt: logistic regression by a few Newton steps (no sklearn dependency)
        a, b = 0.0, float(np.log(base / (1 - base))) if 0 < base < 1 else 0.0
        xs = (x - x.mean()) / (x.std() + 1e-9)
        for _ in range(25):
            p = 1 / (1 + np.exp(-(a * xs + b)))
            w = p * (1 - p) + 1e-9
            g_a, g_b = np.sum((p - y) * xs), np.sum(p - y)
            h_aa, h_bb, h_ab = np.sum(w * xs * xs) + 1e-6, np.sum(w) + 1e-6, np.sum(w * xs)
            det = h_aa * h_bb - h_ab**2
            a -= (h_bb * g_a - h_ab * g_b) / det
            b -= (h_aa * g_b - h_ab * g_a) / det
        # re-express in raw score units
        a_raw = a / (x.std() + 1e-9)
        b_raw = b - a_raw * x.mean()
        method, kx_l, ky_l, a, b = "platt", [], [], float(a_raw), float(b_raw)
        pred = 1 / (1 + np.exp(-(a * x + b)))
    brier = float(np.mean((pred - y) ** 2))
    edges = np.quantile(x, np.linspace(0, 1, bins + 1)) if n >= bins * 2 else np.linspace(x.min(), x.max() + 1e-9, bins + 1)
    rel = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (x >= lo) & (x < hi) if hi < edges[-1] else (x >= lo) & (x <= hi)
        if m.sum() == 0:
            continue
        w_ = int(y[m].sum())
        ci = wilson_interval(w_, int(m.sum()))
        rel.append({"lo": float(lo), "hi": float(hi), "n": int(m.sum()), "predicted": float(pred[m].mean()), "observed": float(y[m].mean()), "ci_lo": ci[0], "ci_hi": ci[1]})
    return Calibrator(method, n, kx_l, ky_l, a, b, brier, base, rel, now_ist().isoformat(), min_n, scope)


def save(cal: Calibrator, state: StateDB | None = None) -> None:
    (state or StateDB()).set_kv(f"{KEY}:{cal.scope}", cal.to_json())


def load(scope: str = "pooled", state: StateDB | None = None) -> Calibrator | None:
    raw = (state or StateDB()).get_kv(f"{KEY}:{scope}")
    return Calibrator.from_json(raw) if raw else None


def z_test_vs_target(wins: int, n: int, target: float = 0.51) -> float:
    """One-sided p-value that the true win rate <= target."""
    if n == 0:
        return 1.0
    p = wins / n
    se = np.sqrt(target * (1 - target) / n)
    return float(1 - norm.cdf((p - target) / se))
