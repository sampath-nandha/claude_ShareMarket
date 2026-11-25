"""Stage-2 confirmations from a data bundle (futures, options, breadth, liquidity, option pick).

Shared by the Live Signals page and the automatic paper trader so both score signals identically.
"""

from __future__ import annotations

import pandas as pd

from mmm.analytics.breadth import compute_breadth
from mmm.analytics.futures import futures_snapshot
from mmm.analytics.options.chain import summarise_chain
from mmm.analytics.options.selector import best_option, rank_options
from mmm.data.quality import DQ
from mmm.settings import get_config


def confirmations(b, ctx, cand):
    from mmm.signals.engine import Confirmations

    cfg = get_config()
    d = 1 if cand.direction == "BULLISH" else -1
    fut = None
    if b.futures_quote:
        day = b.minute_bars[b.minute_bars["ts"].dt.date == b.as_of.date()]
        prev_oi = int(b.futures_quote.get("oi", 0) * 0.98) if b.is_demo and b.futures_quote.get("oi") else None
        fs = futures_snapshot(b.futures_quote, b.spot, prev_oi, day, b.futures_quote.get("volume", 0) / 1.1 if b.is_demo and b.futures_quote.get("volume") else None, d)
        fut = {"confirmation_score": fs.confirmation_score, "basis": fs.basis, "oi_change_pct": fs.oi_change_pct, "rel_volume": fs.rel_volume,
               "price_oi_label": fs.price_oi_label, "notes": fs.notes}
        bs = ctx.extras.get("basis")
        if bs is not None:
            fut["premium_vs_fair"] = bs.premium
            fut["premium_z"] = bs.z
            fut["divergence"] = bs.divergence.kind if bs.divergence else None
    opts, rec, alts, liq, prem_risk = None, None, [], None, None
    if not b.chain.empty and b.expiries:
        e = b.expiries[0]
        summ = summarise_chain(b.chain, b.spot, e, 2 * cfg.universe.option_strikes_each_side + 1)
        score = 0.0
        if summ.pcr_oi is not None:
            score += 0.3 * d * (1 if summ.pcr_oi > 1.1 else -1 if summ.pcr_oi < 0.8 else 0)
        if summ.pcr_oi_change is not None and summ.pcr_oi_change > 0:
            score += 0.2 * d * (1 if summ.pcr_oi_change > 1 else -1)
        opts = {"confirmation_score": max(min(score, 1), -1), "pcr_oi": summ.pcr_oi, "max_pain": summ.max_pain, "call_wall": summ.call_wall,
                "put_wall": summ.put_wall, "atm_iv": summ.atm_iv, "expected_move": summ.expected_move, "coverage": summ.coverage}
        oi = ctx.extras.get("oi")
        if oi is not None:
            opts["pcr_near_trend"] = oi.pcr_trend
            opts["call_wall_status"] = oi.call_wall.status if oi.call_wall else None
            opts["put_wall_status"] = oi.put_wall.status if oi.put_wall else None
        cands = rank_options(b.chain[pd.to_datetime(b.chain["expiry"]).dt.date == e], d, b.spot, cand.target1, cand.stop)
        bo = best_option(cands)
        alts = [c.as_dict() for c in cands]
        if bo:
            rec = bo.as_dict()
            liq = {"score": bo.liquidity_score / 100, "spread_pct": bo.spread_pct}
            prem_risk = (bo.premium - bo.expected_premium_at_stop) if bo.expected_premium_at_stop is not None else bo.premium * 0.4
    brd = None
    if not b.constituents.empty:
        bb = compute_breadth(b.constituents, b.universe_size)
        brd = {"score": bb.score, "authoritative": bb.authoritative, "coverage_pct": bb.coverage_pct, "advancing": bb.advancing, "declining": bb.declining}
    return Confirmations(fut, opts, brd, liq, rec, alts, DQ.PARTIAL if b.is_demo else DQ.VALID, prem_risk)
