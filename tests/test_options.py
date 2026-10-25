import math
from datetime import datetime

import numpy as np

from mmm.analytics.options.chain import chain_table, enrich_chain, max_pain, oi_support_resistance, summarise_chain
from mmm.analytics.options.payoff import Leg, payoff
from mmm.analytics.options.pricing import bs_price, greeks, implied_vol
from mmm.analytics.options.selector import best_option, rank_options
from mmm.demo import demo_option_chain, next_weekly_expiry
from mmm.timeutil import IST


def test_put_call_parity_and_iv_roundtrip():
    s, k, t, r, q, sig = 24000.0, 24100.0, 7 / 365, 0.065, 0.0, 0.14
    c = bs_price(s, k, t, sig, "CE", r, q)
    p = bs_price(s, k, t, sig, "PE", r, q)
    assert abs((c - p) - (s * math.exp(-q * t) - k * math.exp(-r * t))) < 1e-6
    assert abs(implied_vol(c, s, k, t, "CE", r, q) - sig) < 1e-5
    assert abs(implied_vol(p, s, k, t, "PE", r, q) - sig) < 1e-5


def test_iv_nan_for_impossible_prices():
    assert np.isnan(implied_vol(0.01, 24000, 23000, 7 / 365, "CE"))  # below intrinsic
    assert np.isnan(implied_vol(-1, 24000, 24000, 7 / 365, "CE"))


def test_greeks_sanity():
    g = greeks(24000, 24000, 7 / 365, 0.14, "CE")
    assert 0.45 < g.delta < 0.6
    assert g.gamma > 0 and g.vega > 0 and g.theta < 0
    gp = greeks(24000, 24000, 7 / 365, 0.14, "PE")
    assert -0.55 < gp.delta < -0.4
    assert abs(g.gamma - gp.gamma) < 1e-9


def test_chain_enrich_summary_and_selection():
    as_of = datetime(2026, 1, 5, 11, 0, tzinfo=IST)
    exp = next_weekly_expiry(as_of.date())
    spot = 24_012.0
    raw = demo_option_chain(spot, exp, as_of)
    en = enrich_chain(raw, spot, as_of)
    assert en["iv"].notna().mean() > 0.8
    assert (en.loc[en["option_type"] == "CE", "delta"].dropna().between(0, 1)).all()
    summ = summarise_chain(en, spot, exp, expected_strikes=25)
    assert summ.pcr_oi is not None and summ.max_pain is not None and summ.atm_straddle is not None
    assert summ.coverage == "VALID"
    assert summ.put_wall is not None and summ.call_wall is not None
    tbl = chain_table(en, exp)
    assert "CE_oi" in tbl and "PE_oi" in tbl and len(tbl) == 25
    sr = oi_support_resistance(en, exp, spot)
    assert sr["resistance"] and sr["support"]
    cands = rank_options(en, +1, spot, target=spot + 120, stop=spot - 60)
    assert cands and all(c.option_type == "CE" for c in cands)
    assert cands == sorted(cands, key=lambda c: -c.score)
    best = best_option(cands)
    assert best is None or not best.rejections
    assert max_pain(en[en["expiry"] == exp]) in set(en["strike"])


def test_payoff_long_straddle():
    t = 7 / 365
    legs = [Leg("CE", 24000, 1, 150, 0.14, t), Leg("PE", 24000, 1, 140, 0.14, t)]
    res = payoff(legs, 24000)
    assert res.max_profit == float("inf")
    assert abs(res.max_loss + 290 * 75) < 1e-6
    assert len(res.breakevens) == 2
    assert abs(res.breakevens[0] - (24000 - 290)) < 30 and abs(res.breakevens[1] - (24000 + 290)) < 30
    assert abs(res.agg_delta) < 0.2 * 75  # roughly delta-neutral
