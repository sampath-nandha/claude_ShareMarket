"""Market condition gauges: how bullish, how bearish, how sideways the market is right now.

Descriptive, not predictive. Two numbers drive the three gauges:
  direction D in [-1, +1]  from swing structure on three timeframes, price vs an anchor (session VWAP intraday,
                           EMA20 otherwise), price vs the opening range / initial balance (or the recent range),
                           EMA20-vs-EMA50 slope, and equal-weight breadth when it covers >= 80% of the basket
  sideways S in [0, 1]     from the efficiency of the last N bars (net move / path length), staying inside the
                           opening range / initial balance (or recent range), bar overlap, range structure and a
                           mean-reverting regime, and (intraday) a wide CPR
  bullish  = up-evidence x (1 - 0.6 S)     knob: 0 at the left (none) .. 100 at the right (strong)
  bearish  = down-evidence x (1 - 0.6 S)   knob: 100 at the right (none) .. 0 at the left (strong)
  (up/down evidence are the weighted shares of components pointing each way, so both are non-zero when
  timeframes disagree; D = up - down)
  sideways = S                            knob: centred when fully sideways, drifting towards the bias otherwise
Futures parity, option flow and expiry pinning are shown as chips only; they do not move the knobs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any

import numpy as np
import pandas as pd

from mmm.analytics.structure import classify_structure, pivot_swings, trend_direction
from mmm.analytics.volatility import atr

HORIZONS = ("intraday", "60minute", "day")
LABELS = ((70, "STRONG"), (40, "MODERATE"), (20, "WEAK"), (0, "NONE"))


def label(score: float) -> str:
    return next(name for th, name in LABELS if score >= th)


@dataclass
class Gauge:
    name: str               # BULLISH | BEARISH | SIDEWAYS
    score: float            # 0..100
    knob: float             # 0..100, left -> right
    label: str
    reasons: list[str] = field(default_factory=list)
    sublabel: str = ""


@dataclass
class ConditionState:
    horizon: str
    as_of: datetime
    direction: float        # -100..100
    sideways: float         # 0..100
    gauges: list[Gauge]
    components: list[dict[str, Any]]
    chips: list[dict[str, str]]
    state: str              # LIVE | FORMING | AS_OF_CLOSE | STALE | DEMO
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"horizon": self.horizon, "as_of": self.as_of, "direction": self.direction, "sideways": self.sideways,
                "gauges": [g.__dict__ for g in self.gauges], "components": self.components, "chips": self.chips,
                "state": self.state, "notes": self.notes}


def efficiency_ratio(close: pd.Series, n: int) -> float | None:
    c = close.tail(n + 1).to_numpy(float)
    if len(c) < 3:
        return None
    path = np.abs(np.diff(c)).sum()
    return float(abs(c[-1] - c[0]) / path) if path > 0 else 0.0


def _struct_dir(frame: pd.DataFrame, left: int) -> tuple[int, str]:
    if frame is None or len(frame) < 2 * left + 3:
        return 0, "n/a"
    st = classify_structure(frame.reset_index(drop=True), pivot_swings(frame.reset_index(drop=True), left, left))
    return trend_direction(st.trend), st.trend


def _session_twap(intraday: pd.DataFrame) -> float | None:
    if intraday is None or intraday.empty:
        return None
    return float(((intraday["high"] + intraday["low"] + intraday["close"]) / 3).mean())


def compute(ctx, horizon: str = "intraday", *, vwap: float | None = None, breadth: dict | None = None,
            market_open: bool = True, stale: bool = False, demo: bool = False, expiry_pin: bool = False) -> ConditionState:
    if horizon not in HORIZONS:
        raise ValueError(f"horizon must be one of {HORIZONS}")
    comps: list[dict[str, Any]] = []      # name, group (direction|sideways), value -1..1 or 0..1, weight, text

    def add(name: str, group: str, value: float | None, weight: float, text: str) -> None:
        if value is None or not np.isfinite(value):
            comps.append({"name": name, "group": group, "value": None, "weight": weight, "text": text + " (n/a)"})
            return
        comps.append({"name": name, "group": group, "value": float(value), "weight": weight, "text": text})

    if horizon == "intraday":
        frames = (("5m", ctx.setup, 3, 0.20), ("15m", ctx.context, 3, 0.25), ("60m", ctx.trend, 2, 0.15))
        f = ctx.setup
        n_eff = 12                                    # last hour of 5-minute bars
    elif horizon == "60minute":
        frames = (("15m", ctx.context, 3, 0.15), ("60m", ctx.trend, 2, 0.30), ("day", ctx.daily, 2, 0.15))
        f = ctx.trend
        n_eff = 12
    else:
        frames = (("60m", ctx.trend, 2, 0.15), ("day", ctx.daily, 2, 0.35), ("week", _weekly(ctx.daily), 2, 0.10))
        f = ctx.daily
        n_eff = 10
    f = f.reset_index(drop=True) if f is not None else pd.DataFrame()
    a_s = atr(f, 14).dropna() if len(f) > 15 else pd.Series(dtype=float)
    a = float(a_s.iloc[-1]) if len(a_s) else (float((f["high"] - f["low"]).mean()) if len(f) else 1.0)
    close = float(f["close"].iloc[-1]) if len(f) else float("nan")

    # --- direction -------------------------------------------------------------------------------------
    tf_dirs = {}
    for name, frame, left, w in frames:
        d, trend = _struct_dir(frame, left)
        tf_dirs[name] = d
        add(f"structure {name}", "direction", float(d), w, f"{name} structure {trend}")
    if horizon == "intraday":
        anchor = vwap if vwap is not None else _session_twap(ctx.intraday_today)
        anchor_name = "session VWAP" if vwap is not None else "session average price"
    else:
        anchor = float(f["close"].ewm(span=20, adjust=False).mean().iloc[-1]) if len(f) else None
        anchor_name = "EMA20"
    add("price vs anchor", "direction", None if anchor is None else float(np.clip((close - anchor) / a, -1, 1)), 0.15,
        f"price {'above' if anchor is not None and close >= anchor else 'below'} {anchor_name}" if anchor is not None else anchor_name)
    if len(f) >= 50:
        e20, e50 = f["close"].ewm(span=20, adjust=False).mean().iloc[-1], f["close"].ewm(span=50, adjust=False).mean().iloc[-1]
        add("EMA20 vs EMA50", "direction", float(np.clip((e20 - e50) / a, -1, 1)), 0.10, f"EMA20 {'above' if e20 >= e50 else 'below'} EMA50")
    else:
        add("EMA20 vs EMA50", "direction", None, 0.10, "EMA20 vs EMA50")
    lo_r, hi_r, rng_name = _reference_range(ctx, horizon, f)
    pos = None
    if lo_r is not None and hi_r is not None and hi_r > lo_r:
        if close > hi_r:
            pos = 1.0
        elif close < lo_r:
            pos = -1.0
        else:
            pos = ((close - lo_r) / (hi_r - lo_r) - 0.5)
    add("position in range", "direction", pos, 0.15,
        f"price {'above' if pos == 1 else 'below' if pos == -1 else 'inside'} {rng_name}" if pos is not None else rng_name)
    if horizon == "intraday":
        it, daily = ctx.intraday_today, ctx.daily
        if it is not None and not it.empty and daily is not None and len(daily) > 15:
            da = atr(daily.reset_index(drop=True), 14).dropna()
            ref = float(daily["close"].iloc[-1])
            if len(da):
                mv = (close - ref) / float(da.iloc[-1])
                add("session move", "direction", float(np.clip(mv / 0.6, -1, 1)), 0.15,
                    f"{close - ref:+,.0f} pts from the previous close ({mv:+.2f} daily ATR)")
    if horizon == "intraday" and breadth is not None:
        if breadth.get("authoritative"):
            add("breadth", "direction", float(np.clip(breadth.get("score", 0.0), -1, 1)), 0.10,
                f"breadth {breadth.get('advancing', 0)} up / {breadth.get('declining', 0)} down")
        else:
            comps.append({"name": "breadth", "group": "direction", "value": None, "weight": 0.10,
                          "text": f"breadth excluded: coverage {breadth.get('coverage_pct', 0):.0f}%"})

    # --- sideways --------------------------------------------------------------------------------------------
    er = efficiency_ratio(f["close"], n_eff) if len(f) else None
    add("low efficiency", "sideways", None if er is None else float(np.clip((0.55 - er) / 0.45, 0, 1)), 0.45,
        f"efficiency {er:.2f} over the last {n_eff} bars (1 = straight line)" if er is not None else "efficiency")
    inside = None if pos is None else (1.0 if abs(pos) < 1 else 0.0)
    add("inside range", "sideways", inside, 0.20, f"{'inside' if inside else 'outside'} {rng_name}" if inside is not None else rng_name)
    ranging = np.mean([1.0 if d == 0 else 0.0 for d in tf_dirs.values()])
    mr = 1.0 if ctx.regime.behaviour in ("MEAN_REVERTING", "COMPRESSING") or ctx.regime.direction == "SIDEWAYS" else 0.0
    add("range structure / regime", "sideways", float(0.6 * ranging + 0.4 * mr), 0.25,
        f"{int(ranging * len(tf_dirs))}/{len(tf_dirs)} timeframes ranging, regime {ctx.regime.behaviour.lower()}")
    if horizon == "intraday":
        c = ctx.extras.get("cpr") if hasattr(ctx.extras, "get") else None
        add("wide CPR", "sideways", None if c is None else {"WIDE": 1.0, "NORMAL": 0.4, "NARROW": 0.0}.get(c.width_class, 0.4), 0.10,
            f"{c.width_class.lower()} CPR" if c is not None else "CPR")

    def combine(group: str) -> float:
        xs = [(x["value"], x["weight"]) for x in comps if x["group"] == group and x["value"] is not None]
        w = sum(w for _v, w in xs)
        return float(sum(v * w for v, w in xs) / w) if w else 0.0

    dx = [(x["value"], x["weight"]) for x in comps if x["group"] == "direction" and x["value"] is not None]
    wsum = sum(w for _v, w in dx) or 1.0
    up = sum(max(v, 0.0) * w for v, w in dx) / wsum          # evidence pointing up
    dn = sum(max(-v, 0.0) * w for v, w in dx) / wsum         # evidence pointing down (both can be > 0 in a conflict)
    D = float(np.clip(up - dn, -1, 1))
    S = float(np.clip(combine("sideways"), 0, 1))
    bull, bear = up * (1 - 0.6 * S) * 100, dn * (1 - 0.6 * S) * 100
    side = S * 100
    vol_ratio = None
    if horizon == "intraday" and len(f) > 30:
        # today's typical 5-minute range versus the previous sessions' (so an all-day chop still reads as volatile)
        today = f[f["ts"].dt.date == ctx.today]
        before = f[f["ts"].dt.date < ctx.today]
        if len(today) >= 3 and len(before) >= 20:
            vol_ratio = float((today["high"] - today["low"]).mean() / max((before["high"] - before["low"]).mean(), 1e-9))
    elif len(f) > 25:
        a5, a20 = atr(f, 5).iloc[-1], atr(f, 20).iloc[-1]
        vol_ratio = float(a5 / a20) if a20 else None
    side_sub = ("volatile chop" if vol_ratio is not None and vol_ratio >= 1.3 else "quiet range") if side >= 40 else ("trending" if abs(D) > 0.3 else "")

    def reasons(sign: int) -> list[str]:
        pool = [x for x in comps if x["group"] == "direction" and x["value"] is not None and x["value"] * sign > 0.05]
        return [x["text"] for x in sorted(pool, key=lambda x: -abs(x["value"]) * x["weight"])[:3]]

    side_reasons = [x["text"] for x in sorted([x for x in comps if x["group"] == "sideways" and (x["value"] or 0) > 0.2],
                                              key=lambda x: -x["value"] * x["weight"])[:3]]
    gauges = [
        Gauge("BULLISH", round(bull, 1), round(bull, 1), label(bull), reasons(1)),
        Gauge("BEARISH", round(bear, 1), round(100 - bear, 1), label(bear), reasons(-1)),
        Gauge("SIDEWAYS", round(side, 1), round(float(np.clip(50 + D * (1 - S) * 50, 0, 100)), 1), label(side), side_reasons, side_sub),
    ]

    chips: list[dict[str, str]] = []
    if bull > 35 and bear > 35 or (tf_dirs and max(tf_dirs.values()) == 1 and min(tf_dirs.values()) == -1):
        chips.append({"text": "conflicting timeframes", "tone": "warn"})
    b = ctx.extras.get("basis") if hasattr(ctx.extras, "get") else None
    if b is not None and horizon != "day":
        if b.divergence is not None:
            chips.append({"text": f"futures not confirming ({b.divergence.kind.lower()} divergence)", "tone": "warn"})
        elif b.z is not None and abs(b.z) >= 1 and np.sign(b.z) == np.sign(D) and abs(D) > 0.2:
            chips.append({"text": "futures premium confirming", "tone": "ok"})
    oi = ctx.extras.get("oi") if hasattr(ctx.extras, "get") else None
    if oi is not None and horizon == "intraday":
        if oi.pcr_trend in ("RISING", "FALLING"):
            agree = (oi.pcr_trend == "RISING") == (D > 0)
            chips.append({"text": f"near-ATM PCR {oi.pcr_trend.lower()} ({'confirming' if agree and abs(D) > 0.2 else 'not confirming' if abs(D) > 0.2 else 'neutral'})",
                          "tone": "ok" if agree and abs(D) > 0.2 else "info"})
        for w in (oi.call_wall, oi.put_wall):
            if w is not None and w.status != "NEUTRAL":
                chips.append({"text": f"{w.side.lower()} wall {w.strike:,.0f} {w.status.lower()}", "tone": "info"})
    if expiry_pin:
        chips.append({"text": "expiry pin near max pain", "tone": "info"})

    as_of = ctx.as_of
    if demo:
        state = "DEMO"
    elif stale:
        state = "STALE"
    elif not market_open:
        state = "AS_OF_CLOSE"
    elif horizon == "intraday" and as_of.time() < time(9, 30):
        state = "FORMING"
    else:
        state = "LIVE"
    notes = ["Descriptive gauge of the current state, not a forecast; not yet validated as an edge.",
             "Futures, option-flow and expiry chips are information only and do not move the sliders."]
    return ConditionState(horizon, as_of, round(D * 100, 1), round(side, 1), gauges, comps, chips, state, notes)


def _weekly(daily: pd.DataFrame) -> pd.DataFrame:
    if daily is None or daily.empty:
        return pd.DataFrame()
    x = daily.set_index("ts")
    w = x.resample("W-FRI").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna().reset_index()
    return w


def _reference_range(ctx, horizon: str, f: pd.DataFrame) -> tuple[float | None, float | None, str]:
    if horizon == "intraday":
        ib = ctx.extras.get("ib60") if hasattr(ctx.extras, "get") else None
        if ib is not None and ib.complete:
            return ib.low, ib.high, "the 60-minute initial balance"
        it = ctx.intraday_today
        if it is not None and len(it) >= 15:
            orng = it[it["ts"] < it["ts"].iloc[0] + timedelta(minutes=15)]
            return float(orng["low"].min()), float(orng["high"].max()), "the 15-minute opening range"
        return None, None, "the opening range"
    n = 24 if horizon == "60minute" else 20
    if len(f) < n + 1:
        return None, None, "the recent range"
    prior = f.iloc[-n - 1:-1]
    return float(prior["low"].min()), float(prior["high"].max()), f"the prior {n}-bar range"


def path(cache_or_bars, day_bars: pd.DataFrame, aux=None, step_minutes: int = 15) -> pd.DataFrame:
    """Intraday path of the three scores through today's session (one point per `step_minutes`)."""
    from mmm.signals.engine import FrameCache, build_context

    if day_bars is None or day_bars.empty:
        return pd.DataFrame()
    cache = cache_or_bars if isinstance(cache_or_bars, FrameCache) else FrameCache(cache_or_bars, "5minute", "15minute", "60minute")
    d = day_bars["ts"].iloc[0].date()
    start = datetime(d.year, d.month, d.day, 9, 15, tzinfo=day_bars["ts"].iloc[0].tzinfo)
    last = day_bars["ts"].iloc[-1] + pd.Timedelta(minutes=1)
    rows = []
    t = start + timedelta(minutes=step_minutes)
    while t <= last:
        ctx = build_context(None, t, cache=cache, aux=aux)
        if ctx is not None:
            st = compute(ctx, "intraday")
            rows.append({"ts": t, "bullish": st.gauges[0].score, "bearish": st.gauges[1].score, "sideways": st.gauges[2].score})
        t += timedelta(minutes=step_minutes)
    return pd.DataFrame(rows)
