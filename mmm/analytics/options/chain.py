"""Option-chain reconstruction and analytics: PCR, OI walls, max pain, straddle, IV smile/skew, OI build-up.

Input is a flat DataFrame (one row per contract) with columns:
  tradingsymbol, expiry (date/str), strike, option_type (CE/PE), last_price, bid, ask, volume, oi, prev_oi
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np
import pandas as pd

from mmm.analytics.futures import price_oi_label
from mmm.analytics.options.pricing import expected_move_from_straddle, greeks, implied_vol
from mmm.data.quality import DQ, chain_coverage_quality
from mmm.timeutil import now_ist, years_to_expiry


def mid_or_ltp(row: pd.Series) -> float | None:
    """Prefer bid/ask mid when the spread is sane; fall back to LTP. None when nothing usable."""
    b, a, ltp = row.get("bid"), row.get("ask"), row.get("last_price")
    if b and a and a >= b > 0 and (a - b) / ((a + b) / 2) < 0.25:
        return float((a + b) / 2)
    if ltp and ltp > 0:
        return float(ltp)
    return None


def enrich_chain(chain: pd.DataFrame, spot: float, as_of: datetime | None = None) -> pd.DataFrame:
    """Add mid, IV, Greeks, OI change, spread %, liquidity score per contract (all DERIVED)."""
    if chain.empty:
        return chain.copy()
    as_of = as_of or now_ist()
    df = chain.copy()
    df["expiry"] = pd.to_datetime(df["expiry"]).dt.date
    df["strike"] = df["strike"].astype(float)
    df["mid"] = df.apply(mid_or_ltp, axis=1)
    df["spread_pct"] = np.where(
        (df["bid"] > 0) & (df["ask"] > 0), (df["ask"] - df["bid"]) / ((df["ask"] + df["bid"]) / 2) * 100, np.nan
    )
    df["oi_change"] = df["oi"] - df["prev_oi"] if "prev_oi" in df else np.nan
    df["oi_change_pct"] = np.where(df.get("prev_oi", pd.Series(dtype=float)).fillna(0) > 0, df["oi_change"] / df["prev_oi"] * 100, np.nan)
    df["t"] = df["expiry"].map(lambda e: years_to_expiry(e, as_of))
    ivs, deltas, gammas, thetas, vegas, intr, tv = [], [], [], [], [], [], []
    for _, r in df.iterrows():
        iv = implied_vol(r["mid"], spot, r["strike"], r["t"], r["option_type"]) if r["mid"] else float("nan")
        g = greeks(spot, r["strike"], r["t"], iv, r["option_type"])
        ivs.append(iv); deltas.append(g.delta); gammas.append(g.gamma); thetas.append(g.theta); vegas.append(g.vega)
        intr.append(g.intrinsic); tv.append(g.time_value if np.isfinite(g.time_value) else (r["mid"] or 0) - g.intrinsic)
    df["iv"] = ivs
    df["delta"], df["gamma"], df["theta"], df["vega"] = deltas, gammas, thetas, vegas
    df["intrinsic"], df["time_value"] = intr, tv
    df["moneyness"] = np.where(df["option_type"] == "CE", spot / df["strike"] - 1, df["strike"] / spot - 1) * 100
    df["liquidity_score"] = liquidity_score(df)
    df["buildup"] = [
        price_oi_label(r["last_price"] - r["prev_close"] if r.get("prev_close") else None, r["oi_change"])
        if "prev_close" in df.columns else price_oi_label(None, None)
        for _, r in df.iterrows()
    ]
    return df


def liquidity_score(df: pd.DataFrame) -> pd.Series:
    """0-100: spread (40%), OI (30%), volume (30%) with log scaling. Missing bid/ask -> spread component 0."""
    spread = df["spread_pct"].fillna(np.inf)
    s_spread = np.clip(1 - spread / 3.0, 0, 1)  # 0% spread -> 1, 3%+ -> 0
    s_oi = np.clip(np.log10(df["oi"].fillna(0) + 1) / 6.5, 0, 1)        # ~3M OI -> 1
    s_vol = np.clip(np.log10(df["volume"].fillna(0) + 1) / 6.5, 0, 1)
    return (40 * s_spread + 30 * s_oi + 30 * s_vol).round(1)


@dataclass
class ChainSummary:
    expiry: date
    spot: float
    atm_strike: float
    pcr_oi: float | None
    pcr_volume: float | None
    pcr_oi_change: float | None
    max_pain: float | None
    call_wall: float | None       # strike with highest call OI (resistance by convention)
    put_wall: float | None        # strike with highest put OI (support by convention)
    atm_straddle: float | None
    expected_move: float | None   # heuristic: straddle / 0.8
    expected_move_pct: float | None
    atm_iv: float | None
    iv_skew_25d: float | None     # IV(put ~25Δ) - IV(call ~25Δ)
    call_oi_total: int
    put_oi_total: int
    call_oi_change: int | None
    put_oi_change: int | None
    coverage: str
    strikes_present: int
    strikes_expected: int
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["expiry"] = str(self.expiry)
        return d


def max_pain(df: pd.DataFrame) -> float | None:
    """Strike minimising total intrinsic payout to option holders at expiry."""
    if df.empty:
        return None
    strikes = np.sort(df["strike"].unique())
    ce = df[df["option_type"] == "CE"].groupby("strike")["oi"].sum().reindex(strikes).fillna(0).to_numpy()
    pe = df[df["option_type"] == "PE"].groupby("strike")["oi"].sum().reindex(strikes).fillna(0).to_numpy()
    pain = []
    for s in strikes:
        pain.append(float((np.maximum(s - strikes, 0) * ce).sum() + (np.maximum(strikes - s, 0) * pe).sum()))
    return float(strikes[int(np.argmin(pain))])


def summarise_chain(enriched: pd.DataFrame, spot: float, expiry: date, expected_strikes: int, step: int = 50) -> ChainSummary:
    df = enriched[pd.to_datetime(enriched["expiry"]).dt.date == expiry]
    notes: list[str] = []
    strikes = sorted(df["strike"].unique())
    cov = chain_coverage_quality(len(strikes), expected_strikes)
    if df.empty:
        return ChainSummary(expiry, spot, round(spot / step) * step, None, None, None, None, None, None, None, None, None,
                            None, None, 0, 0, None, None, cov.name, 0, expected_strikes, ["no contracts"])
    atm = float(min(strikes, key=lambda k: abs(k - spot)))
    ce, pe = df[df["option_type"] == "CE"], df[df["option_type"] == "PE"]
    c_oi, p_oi = int(ce["oi"].fillna(0).sum()), int(pe["oi"].fillna(0).sum())
    c_vol, p_vol = ce["volume"].fillna(0).sum(), pe["volume"].fillna(0).sum()
    c_chg = int(ce["oi_change"].sum()) if ce["oi_change"].notna().any() else None
    p_chg = int(pe["oi_change"].sum()) if pe["oi_change"].notna().any() else None
    pcr_oi = p_oi / c_oi if c_oi else None
    pcr_vol = p_vol / c_vol if c_vol else None
    pcr_chg = (p_chg / c_chg) if (c_chg and p_chg is not None and c_chg != 0) else None
    call_wall = float(ce.loc[ce["oi"].idxmax(), "strike"]) if c_oi else None
    put_wall = float(pe.loc[pe["oi"].idxmax(), "strike"]) if p_oi else None
    atm_ce = ce[ce["strike"] == atm]["mid"].dropna()
    atm_pe = pe[pe["strike"] == atm]["mid"].dropna()
    straddle = float(atm_ce.iloc[0] + atm_pe.iloc[0]) if len(atm_ce) and len(atm_pe) else None
    em = expected_move_from_straddle(straddle) if straddle else None
    atm_ivs = pd.concat([ce[ce["strike"] == atm]["iv"], pe[pe["strike"] == atm]["iv"]]).dropna()
    atm_iv = float(atm_ivs.mean()) if len(atm_ivs) else None
    skew = None
    if ce["delta"].notna().any() and pe["delta"].notna().any():
        c25 = ce.iloc[(ce["delta"] - 0.25).abs().argsort()[:1]]
        p25 = pe.iloc[(pe["delta"] + 0.25).abs().argsort()[:1]]
        if len(c25) and len(p25) and np.isfinite(c25["iv"].iloc[0]) and np.isfinite(p25["iv"].iloc[0]):
            skew = float(p25["iv"].iloc[0] - c25["iv"].iloc[0])
    if cov < DQ.PARTIAL:
        notes.append("chain coverage incomplete; PCR/max-pain unreliable")
    if atm_iv is None:
        notes.append("ATM IV unavailable (no usable quotes)")
    return ChainSummary(
        expiry, spot, atm, pcr_oi, pcr_vol, pcr_chg, max_pain(df), call_wall, put_wall, straddle, em,
        (em / spot * 100) if em else None, atm_iv, skew, c_oi, p_oi, c_chg, p_chg, cov.name, len(strikes), expected_strikes, notes,
    )


def chain_table(enriched: pd.DataFrame, expiry: date) -> pd.DataFrame:
    """Classic side-by-side CE | strike | PE view for the dashboard."""
    df = enriched[pd.to_datetime(enriched["expiry"]).dt.date == expiry]
    cols = ["oi", "oi_change", "volume", "iv", "delta", "theta", "last_price", "bid", "ask", "spread_pct", "liquidity_score", "buildup"]
    ce = df[df["option_type"] == "CE"].set_index("strike")[cols].add_prefix("CE_")
    pe = df[df["option_type"] == "PE"].set_index("strike")[cols].add_prefix("PE_")
    out = ce.join(pe, how="outer").sort_index()
    out.index.name = "strike"
    return out.reset_index()


def oi_support_resistance(enriched: pd.DataFrame, expiry: date, spot: float, top_n: int = 3) -> dict[str, list[tuple[float, int]]]:
    df = enriched[pd.to_datetime(enriched["expiry"]).dt.date == expiry]
    ce = df[(df["option_type"] == "CE") & (df["strike"] >= spot)].nlargest(top_n, "oi")
    pe = df[(df["option_type"] == "PE") & (df["strike"] <= spot)].nlargest(top_n, "oi")
    return {
        "resistance": [(float(r.strike), int(r.oi)) for r in ce.itertuples()],
        "support": [(float(r.strike), int(r.oi)) for r in pe.itertuples()],
    }
