"""Candlestick patterns on intraday NIFTY bars: 5 momentum/continuation, 5 reversal, 5 indecision.

Intraday index bars open almost exactly at the previous close (no gaps inside the session), so textbook
definitions that need gaps between bodies (stars, piercing line, harami variants) are adapted to body/range
relationships instead. Detection is purely mechanical and context-free; whether a pattern means anything is
decided by the Phase-4 feature study (`mmm.research.features`), where each family is tested only in its
pre-declared context:
  momentum/continuation -> with the context-timeframe trend
  reversal              -> at a strong support/resistance level
  indecision            -> after a run of >= 1 ATR, at the run's extreme (hypothesis: reversal)

`pattern_frame` returns one integer column per pattern: +1 bullish, -1 bearish, 0 none. Indecision patterns
are direction-free and use 1 for "present".
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from mmm.analytics.structure import candle_anatomy

MOMENTUM = ("marubozu", "three_soldiers_crows", "inside_bar_breakout", "nr7_breakout", "three_methods")
REVERSAL = ("engulfing", "hammer_shooting_star", "morning_evening_star", "tweezer", "piercing_dark_cloud")
INDECISION = ("doji", "spinning_top", "inside_bar", "harami", "high_wave")
FAMILY = {**{k: "momentum" for k in MOMENTUM}, **{k: "reversal" for k in REVERSAL}, **{k: "indecision" for k in INDECISION}}

DESCRIPTIONS = {
    "marubozu": "wide-range trend bar: body >= 80% of range and range >= 1.5x the 20-bar average",
    "three_soldiers_crows": "three consecutive same-colour bars, each closing beyond the last, bodies >= 50%, closes in the top/bottom 40%",
    "inside_bar_breakout": "the previous bar was inside its predecessor; this bar closes beyond that mother bar",
    "nr7_breakout": "the previous bar was the narrowest of 7; this bar closes beyond it",
    "three_methods": "strong bar, three small bars held inside its range, then a close beyond the strong bar's close (rising/falling three methods)",
    "engulfing": "body (>= 50% of range, larger than the previous body) engulfs the previous opposite-colour body",
    "hammer_shooting_star": "pin bar (wick >= 60%, body <= 30%) printing a 10-bar low (hammer) or high (shooting star)",
    "morning_evening_star": "strong bar, small-body pause bar, then a bar closing beyond the strong bar's body midpoint (no gap required)",
    "tweezer": "two bars with matching lows/highs (within 10% of average range), opposite colours, at a 10-bar extreme",
    "piercing_dark_cloud": "strong bar, then an opposite bar opening near its close and closing past its body midpoint but not beyond its open",
    "doji": "body <= 10% of range",
    "spinning_top": "body <= 30% with both wicks >= 25% (and not a doji)",
    "inside_bar": "high/low inside the previous bar",
    "harami": "body inside the previous strong body (previous body >= 60%, current body <= 50%)",
    "high_wave": "body <= 15%, both wicks >= 30%, range >= 1.3x the 20-bar average",
}


@dataclass
class PatternHit:
    key: str
    family: str
    direction: int          # +1/-1 for directional patterns, 0 for indecision (direction comes from context)
    description: str

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def pattern_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Vectorised detection over a candle frame (ts, open, high, low, close). Row i uses bars <= i only."""
    if df is None or df.empty:
        return pd.DataFrame(columns=list(FAMILY))
    df = df.reset_index(drop=True)
    a = candle_anatomy(df)
    o, h, lo, c = df["open"], df["high"], df["low"], df["close"]
    rng, br, cl = a["range"], a["body_ratio"].fillna(0), a["close_loc"].fillna(0.5)
    uw, lw = a["upper_wick"].fillna(0), a["lower_wick"].fillna(0)
    avg = rng.rolling(20, min_periods=5).mean()
    bull, bear = c > o, c < o
    out = pd.DataFrame(index=df.index)

    def sgn(up: pd.Series, dn: pd.Series) -> pd.Series:
        return up.fillna(False).astype(int) - dn.fillna(False).astype(int)

    # --- momentum / continuation ---------------------------------------------------------------------
    wide = (br >= 0.8) & (rng >= 1.5 * avg)
    out["marubozu"] = sgn(wide & bull, wide & bear)
    s_up = bull & bull.shift(1) & bull.shift(2) & (c > c.shift(1)) & (c.shift(1) > c.shift(2))
    s_dn = bear & bear.shift(1) & bear.shift(2) & (c < c.shift(1)) & (c.shift(1) < c.shift(2))
    strong3 = (br >= 0.5) & (br.shift(1) >= 0.5) & (br.shift(2) >= 0.5)
    out["three_soldiers_crows"] = sgn(s_up & strong3 & (cl >= 0.6) & (cl.shift(1) >= 0.6) & (cl.shift(2) >= 0.6),
                                      s_dn & strong3 & (cl <= 0.4) & (cl.shift(1) <= 0.4) & (cl.shift(2) <= 0.4))
    was_inside = a["inside"].shift(1).fillna(False).astype(bool)
    out["inside_bar_breakout"] = sgn(was_inside & (c > h.shift(2)), was_inside & (c < lo.shift(2)))
    nr7 = rng.shift(1) <= rng.shift(1).rolling(7, min_periods=7).min()
    out["nr7_breakout"] = sgn(nr7 & (c > h.shift(1)), nr7 & (c < lo.shift(1)))
    h0, l0, c0, r0 = h.shift(4), lo.shift(4), c.shift(4), rng.shift(4)
    first_up = bull.shift(4) & (br.shift(4) >= 0.6) & (r0 >= 1.2 * avg.shift(4))
    first_dn = bear.shift(4) & (br.shift(4) >= 0.6) & (r0 >= 1.2 * avg.shift(4))
    held = pd.Series(True, index=df.index)
    for k in (1, 2, 3):
        held &= (h.shift(k) <= h0) & (lo.shift(k) >= l0) & (rng.shift(k) <= 0.7 * r0)
    out["three_methods"] = sgn(first_up & held & bull & (c > c0), first_dn & held & bear & (c < c0))

    # --- reversal -----------------------------------------------------------------------------------------
    # intraday bars open at the previous close, so plain engulfing fires constantly; require a real body that is
    # larger than the one it engulfs
    body = (c - o).abs()
    real = (br >= 0.5) & (body > body.shift(1))
    out["engulfing"] = sgn(a["bull_engulf"] & real, a["bear_engulf"] & real)
    low10, high10 = lo.rolling(10, min_periods=5).min(), h.rolling(10, min_periods=5).max()
    out["hammer_shooting_star"] = sgn(a["pin_bull"] & (lo <= low10), a["pin_bear"] & (h >= high10))
    mid2 = (o.shift(2) + c.shift(2)) / 2
    pause = (br.shift(1) <= 0.3) & (rng.shift(1) <= 0.6 * avg.shift(1))
    strong2 = (br.shift(2) >= 0.6) & (rng.shift(2) >= avg.shift(2))
    out["morning_evening_star"] = sgn(bear.shift(2) & strong2 & pause & bull & (c > mid2), bull.shift(2) & strong2 & pause & bear & (c < mid2))
    tol = 0.1 * avg
    out["tweezer"] = sgn(((lo - lo.shift(1)).abs() <= tol) & bear.shift(1) & bull & (lo.combine(lo.shift(1), min) <= low10),
                         ((h - h.shift(1)).abs() <= tol) & bull.shift(1) & bear & (h.combine(h.shift(1), max) >= high10))
    mid1 = (o.shift(1) + c.shift(1)) / 2
    strong1 = br.shift(1) >= 0.6
    out["piercing_dark_cloud"] = sgn(bear.shift(1) & strong1 & bull & (o <= c.shift(1) + tol) & (c > mid1) & (c < o.shift(1)),
                                     bull.shift(1) & strong1 & bear & (o >= c.shift(1) - tol) & (c < mid1) & (c > o.shift(1)))

    # --- indecision (presence only) ---------------------------------------------------------------------
    doji = br <= 0.1
    out["doji"] = doji.astype(int)
    out["spinning_top"] = ((br <= 0.3) & (uw >= 0.25) & (lw >= 0.25) & ~doji).astype(int)
    out["inside_bar"] = a["inside"].fillna(False).astype(int)
    body_hi, body_lo = pd.concat([o, c], axis=1).max(axis=1), pd.concat([o, c], axis=1).min(axis=1)
    out["harami"] = ((br.shift(1) >= 0.6) & (br <= 0.5) & (body_hi <= body_hi.shift(1)) & (body_lo >= body_lo.shift(1))).fillna(False).astype(int)
    out["high_wave"] = ((br <= 0.15) & (uw >= 0.3) & (lw >= 0.3) & (rng >= 1.3 * avg)).fillna(False).astype(int)
    return out.fillna(0).astype(int)


def hits_at(frame: pd.DataFrame, i: int = -1) -> list[PatternHit]:
    if frame is None or frame.empty:
        return []
    row = frame.iloc[i]
    return [PatternHit(k, FAMILY[k], int(np.sign(v)) if FAMILY[k] != "indecision" else 0, DESCRIPTIONS[k]) for k, v in row.items() if v]


def run_series(setup: pd.DataFrame, atr_series: pd.Series, bars: int = 6) -> pd.Series:
    """Vectorised prior_run for every bar (used to pre-screen indecision events cheaply)."""
    move = setup["close"].shift(1) - setup["close"].shift(1 + bars)
    return ((move >= atr_series).astype(int) - (move <= -atr_series).astype(int)).fillna(0)


def prior_run(setup: pd.DataFrame, atr_value: float, bars: int = 6) -> int:
    """+1 after a rally of >= 1 ATR into the bar before last, -1 after a decline, else 0 (indecision context)."""
    if len(setup) < bars + 2 or not atr_value:
        return 0
    move = float(setup["close"].iloc[-2] - setup["close"].iloc[-2 - bars])
    return 1 if move >= atr_value else (-1 if move <= -atr_value else 0)


def contextual_direction(key: str, raw: int, ctx) -> int:
    """Apply the family's pre-declared context. Returns the hypothesised trade direction, or 0 if out of context."""
    from mmm.analytics.structure import trend_direction

    fam = FAMILY[key]
    bar = ctx.setup.iloc[-1]
    a = ctx.atr
    if fam == "momentum":
        return raw if raw != 0 and trend_direction(ctx.context_structure.trend) == raw else 0
    if fam == "reversal":
        if raw == 0:
            return 0
        strong = ("PDH", "PDL", "PWH", "PWL", "OI_R", "OI_S", "CONF_R", "CONF_S", "ORH", "ORL")
        lvls = [lv.price for lv in ctx.levels if lv.kind in strong or (lv.kind.startswith("ZONE") and lv.strength >= 55)]
        if raw > 0:
            return 1 if any(abs(float(bar["low"]) - p) <= 0.25 * a for p in lvls) else 0
        return -1 if any(abs(float(bar["high"]) - p) <= 0.25 * a for p in lvls) else 0
    run = prior_run(ctx.setup, a)
    if run == 0:
        return 0
    recent = ctx.setup.tail(10)
    at_extreme = float(bar["high"]) >= float(recent["high"].max()) - 0.1 * a if run > 0 else float(bar["low"]) <= float(recent["low"].min()) + 0.1 * a
    return -run if at_extreme else 0


def latest(ctx) -> dict[str, int]:
    """Raw pattern values on the context's decision bar (memoised in ctx.extras)."""
    cached = ctx.extras.get("_patterns_raw") if hasattr(ctx.extras, "get") else None
    if cached is not None:
        return cached
    frame = pattern_frame(ctx.setup.tail(60))
    raw = {k: int(v) for k, v in frame.iloc[-1].items()} if not frame.empty else {}
    try:
        ctx.extras["_patterns_raw"] = raw
    except TypeError:
        pass
    return raw
