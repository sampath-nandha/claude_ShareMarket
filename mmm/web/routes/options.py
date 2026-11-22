"""Options pages: chain, OI structure, IV & Greeks, OI flow (Phase 3), ranked contracts, payoff builder, theta planner."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from mmm.analytics.options.chain import chain_table, oi_support_resistance, summarise_chain
from mmm.analytics.options.payoff import Leg, payoff
from mmm.analytics.options.pricing import bs_price, expected_move_from_iv
from mmm.analytics.options.selector import best_option, rank_options
from mmm.risk.costs import breakeven_move, round_trip_costs
from mmm.risk.limits import position_size
from mmm.settings import get_config
from mmm.timeutil import years_to_expiry
from mmm.web.data import DERIVED_NOTE, aux, bundle, chart_time, clean, records

router = APIRouter(prefix="/api/options")


def _chain_or_404():
    b = bundle()
    if b.chain.empty:
        raise HTTPException(409, "no option chain yet - start the collector during market hours")
    return b


def _expiry(b, expiry: str | None) -> date:
    exps = sorted(pd.to_datetime(b.chain["expiry"]).dt.date.unique())
    if expiry:
        e = date.fromisoformat(expiry)
        if e not in exps:
            raise HTTPException(400, f"unknown expiry {expiry}")
        return e
    return exps[0]


@router.get("")
def options(expiry: str | None = None, width: int = 10):
    b = _chain_or_404()
    cfg = get_config()
    exps = sorted(pd.to_datetime(b.chain["expiry"]).dt.date.unique())
    exp = _expiry(b, expiry)
    step = cfg.universe.strike_step
    n_exp = 2 * cfg.universe.option_strikes_each_side + 1
    summ = summarise_chain(b.chain, b.spot, exp, n_exp, step)
    t_years = years_to_expiry(exp, b.as_of)
    en = b.chain[pd.to_datetime(b.chain["expiry"]).dt.date == exp].copy()
    tbl = chain_table(b.chain, exp)
    tbl = tbl[(tbl["strike"] - summ.atm_strike).abs() <= width * step]
    if "instrument_token" in en.columns:
        tok = en.pivot_table(index="strike", columns="option_type", values="instrument_token", aggfunc="first")
        tbl = tbl.merge(tok.rename(columns={"CE": "CE_token", "PE": "PE_token"}).reset_index(), on="strike", how="left")
    sub = en[(en["strike"] - summ.atm_strike).abs() <= 10 * step]
    ce, pe = sub[sub["option_type"] == "CE"].sort_values("strike"), sub[sub["option_type"] == "PE"].sort_values("strike")
    out = {
        "expiries": [{"expiry": e, "dte": (e - b.as_of.date()).days} for e in exps], "expiry": exp, "spot": b.spot, "as_of": b.as_of,
        "summary": summ.as_dict(), "t_years": t_years, "derived_note": DERIVED_NOTE,
        "iv_move": expected_move_from_iv(b.spot, summ.atm_iv, t_years) if summ.atm_iv else None,
        "chain": records(tbl),
        "oi_bars": {"strikes": ce["strike"].tolist(), "ce_oi": ce["oi"].tolist(), "pe_oi": pe.set_index("strike").reindex(ce["strike"])["oi"].tolist(),
                    "ce_chg": ce["oi_change"].tolist(), "pe_chg": pe.set_index("strike").reindex(ce["strike"])["oi_change"].tolist()},
        "walls": oi_support_resistance(b.chain, exp, b.spot),
        "buildup": records(sub[["tradingsymbol", "strike", "option_type", "last_price", "oi", "oi_change", "oi_change_pct", "buildup"]].sort_values(["strike", "option_type"])),
        "smile": {"ce": [[k, iv * 100] for k, iv in zip(ce["strike"], ce["iv"]) if pd.notna(iv)], "pe": [[k, iv * 100] for k, iv in zip(pe["strike"], pe["iv"]) if pd.notna(iv)]},
        "greeks": records(sub.assign(iv=sub["iv"] * 100, theta_pct_day=sub["theta"] / sub["mid"] * 100)[
            ["tradingsymbol", "strike", "option_type", "mid", "iv", "delta", "gamma", "theta", "vega", "intrinsic", "time_value", "spread_pct", "liquidity_score", "theta_pct_day"]]
            .sort_values(["strike", "option_type"])),
    }
    term = []
    for e in exps:
        s = summarise_chain(b.chain, b.spot, e, n_exp, step)
        term.append({"expiry": e, "atm_iv": s.atm_iv * 100 if s.atm_iv else None, "straddle": s.atm_straddle, "expected_move": s.expected_move,
                     "pcr": s.pcr_oi, "max_pain": s.max_pain})
    out["term"] = term
    out["flow"] = _flow(b)
    return clean(out)


def _flow(b) -> dict:
    """Phase 3 OI flow for today from archived option minute candles."""
    a = aux(b)
    if a is None or a.chain is None or b.as_of.date() not in a.chain.days:
        return {"available": False, "note": "needs archived option minute candles for today (collector running, or `mmm data backfill-options`)"}
    st = a.chain.state(b.as_of, b.spot)
    if st is None:
        return {"available": False, "note": "no option candles printed yet today"}
    hm = a.chain.heatmap(b.as_of.date(), st.expiry)
    ws = a.chain.wall_series(b.as_of.date(), b.minute_bars[b.minute_bars["ts"].dt.date == b.as_of.date()])
    return {"available": True, "state": st.as_dict(), "table": records(st.table),
            "heatmap": records(hm.assign(time=hm["ts"].map(chart_time)).drop(columns="ts")) if not hm.empty else [],
            "walls": records(ws.assign(time=ws["ts"].map(chart_time)).drop(columns="ts")) if not ws.empty else []}


class RankReq(BaseModel):
    expiry: str | None = None
    direction: str = "BULLISH"
    target: float | None = None
    stop: float | None = None
    hold_hours: float = 1.5
    delta_lo: float = 0.35
    delta_hi: float = 0.65


@router.post("/rank")
def rank(req: RankReq):
    b = _chain_or_404()
    cfg = get_config()
    exp = _expiry(b, req.expiry)
    d = 1 if req.direction == "BULLISH" else -1
    tgt = req.target if req.target is not None else b.spot + d * 120
    stp = req.stop if req.stop is not None else b.spot - d * 60
    cands = rank_options(b.chain[pd.to_datetime(b.chain["expiry"]).dt.date == exp], d, b.spot, tgt, stp, req.hold_hours / 6.25,
                         (req.delta_lo, req.delta_hi), top_n=8)  # hold as a fraction of a 6.25h session, as before
    best = best_option(cands)
    out: dict = {"candidates": [{**c.as_dict(), "reasons": "; ".join(c.reasons), "rejections": "; ".join(c.rejections)} for c in cands],
                 "best": best.as_dict() if best else None, "target": tgt, "stop": stp}
    if best:
        lot = cfg.risk.nifty_lot_size
        risk_unit = best.premium - (best.expected_premium_at_stop or 0)
        lots, rupees = position_size(risk_unit)
        out["sizing"] = {"lots": lots, "qty": lots * lot, "premium_at_risk": rupees, "risk_pct": cfg.risk.max_risk_per_trade_pct,
                         "capital": cfg.risk.account_capital, "round_trip_cost_1lot": round_trip_costs(best.premium, best.premium, lot).total,
                         "breakeven_premium_pts": breakeven_move(best.premium, lot)}
    return clean(out)


class LegReq(BaseModel):
    kind: str
    strike: float
    qty: int


class PayoffReq(BaseModel):
    expiry: str | None = None
    legs: list[LegReq] = Field(default_factory=list)
    preset: str | None = None
    days_forward: float = 0.0


PRESETS = ("Long straddle", "Short straddle", "Bull call spread", "Bear put spread", "Iron condor", "Long strangle")


@router.post("/payoff")
def payoff_route(req: PayoffReq):
    b = _chain_or_404()
    cfg = get_config()
    exp = _expiry(b, req.expiry)
    en = b.chain[pd.to_datetime(b.chain["expiry"]).dt.date == exp]
    t_years = years_to_expiry(exp, b.as_of)
    lot = cfg.risk.nifty_lot_size
    summ = summarise_chain(b.chain, b.spot, exp, 2 * cfg.universe.option_strikes_each_side + 1, cfg.universe.strike_step)

    def mk(k: float, kind: str, q: int) -> Leg:
        r = en[(en["strike"] == k) & (en["option_type"] == kind)]
        p = float(r["mid"].iloc[0]) if not r.empty and pd.notna(r["mid"].iloc[0]) else 0.0
        v = float(r["iv"].iloc[0]) if not r.empty and pd.notna(r["iv"].iloc[0]) else None
        return Leg(kind, float(k), q, p, v, t_years, lot)

    legs = [mk(lg.strike, lg.kind, lg.qty) for lg in req.legs]
    if req.preset:
        a, s = summ.atm_strike, cfg.universe.strike_step
        presets = {
            "Long straddle": [(a, "CE", 1), (a, "PE", 1)], "Short straddle": [(a, "CE", -1), (a, "PE", -1)],
            "Bull call spread": [(a, "CE", 1), (a + 4 * s, "CE", -1)], "Bear put spread": [(a, "PE", 1), (a - 4 * s, "PE", -1)],
            "Iron condor": [(a + 4 * s, "CE", -1), (a + 8 * s, "CE", 1), (a - 4 * s, "PE", -1), (a - 8 * s, "PE", 1)],
            "Long strangle": [(a + 3 * s, "CE", 1), (a - 3 * s, "PE", 1)],
        }
        if req.preset not in presets:
            raise HTTPException(400, f"preset must be one of {PRESETS}")
        legs = [mk(k, kd, q) for k, kd, q in presets[req.preset]]
    if not legs:
        return {"legs": [], "presets": PRESETS, "strikes": sorted(en["strike"].unique().tolist()), "atm": summ.atm_strike}
    res = payoff(legs, b.spot, width_pct=5.0, days_forward=req.days_forward)
    g = res.grid
    return clean({
        "legs": [{"kind": lg.kind, "strike": lg.strike, "qty": lg.qty, "premium": lg.premium, "iv": (lg.iv or np.nan) * 100} for lg in legs],
        "grid": {"spot": g["spot"].round(1).tolist(), "expiry": g["expiry_pnl"].round(0).tolist(),
                 "t0": g["t0_pnl"].round(0).tolist() if g["t0_pnl"].notna().any() else []},
        "net_premium": res.net_premium, "max_profit": "unlimited" if res.max_profit == float("inf") else res.max_profit,
        "max_loss": "unlimited" if res.max_loss == float("-inf") else res.max_loss, "breakevens": res.breakevens,
        "greeks": {"delta": res.agg_delta, "gamma": res.agg_gamma, "theta": res.agg_theta, "vega": res.agg_vega},
        "notes": res.notes, "spot": b.spot, "expected_move": summ.expected_move, "presets": PRESETS,
        "strikes": sorted(en["strike"].unique().tolist()), "atm": summ.atm_strike, "max_days": max(t_years * 365 - 0.1, 0.1)})


@router.get("/theta")
def theta(kind: str = "CE", strike: float | None = None, lots: int = 1, expiry: str | None = None):
    b = _chain_or_404()
    cfg = get_config()
    exp = _expiry(b, expiry)
    en = b.chain[pd.to_datetime(b.chain["expiry"]).dt.date == exp]
    summ = summarise_chain(b.chain, b.spot, exp, 2 * cfg.universe.option_strikes_each_side + 1, cfg.universe.strike_step)
    k = strike if strike is not None else summ.atm_strike
    row = en[(en["strike"] == k) & (en["option_type"] == kind)]
    if row.empty or pd.isna(row["iv"].iloc[0]) or pd.isna(row["mid"].iloc[0]):
        raise HTTPException(409, "no usable quote/IV for that contract")
    r = row.iloc[0]
    prem, iv, lot = float(r["mid"]), float(r["iv"]), cfg.risk.nifty_lot_size
    t_years = years_to_expiry(exp, b.as_of)
    moves = [-150, -100, -50, 0, 50, 100, 150, 200]
    table = []
    for h in (0, 0.5, 1, 2, 3, 4, 6.25):
        t_rem = max(t_years - h / (24 * 365), 1e-6)
        rec = {"hours": h}
        for mv in moves:
            p = bs_price(b.spot + mv, k, t_rem, iv, kind)
            rec[f"{mv:+d}"] = (p - prem) * lot * lots - round_trip_costs(prem, p, lot * lots).total
        table.append(rec)
    return clean({"kind": kind, "strike": k, "premium": prem, "iv": iv * 100, "theta": float(r["theta"]), "lots": lots, "moves": moves,
                  "table": table, "theta_rupees_per_day": float(r["theta"]) * lot * lots, "strikes": sorted(en["strike"].unique().tolist())})
