"""SYNTHETIC DEMO DATA. Not market data. Never persisted to the archive, never used for validation claims.

Used only so the dashboard can be explored before Kite credentials/history exist. Every page that shows
demo data carries a visible "SYNTHETIC DEMO" banner. Generated with a fixed seed for reproducibility.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from mmm.analytics.options.pricing import bs_price
from mmm.timeutil import IST, trading_days_between, years_to_expiry

DEMO_LABEL = "SYNTHETIC DEMO - not market data"


def demo_minute_bars(days: int = 40, start_price: float = 24_000.0, seed: int = 42, end: date | None = None) -> pd.DataFrame:
    """Regime-switching random-walk 1-minute NIFTY-like bars for `days` trading sessions."""
    rng = np.random.default_rng(seed)
    end = end or date.today()
    sessions = trading_days_between(end - timedelta(days=int(days * 1.7) + 10), end)[-days:]
    rows = []
    price = start_price
    for d in sessions:
        drift = rng.choice([-0.00002, 0.0, 0.00002], p=[0.3, 0.4, 0.3])
        vol = rng.choice([0.00025, 0.0004, 0.0006], p=[0.4, 0.4, 0.2])
        price *= 1 + rng.normal(0, 0.004)  # overnight gap
        t = datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST)
        for m in range(375):
            o = price
            r = rng.normal(drift, vol * (1.6 if m < 30 or m > 345 else 1.0))
            c = o * (1 + r)
            spread = abs(rng.normal(0, vol * 0.8)) * o
            h, l = max(o, c) + spread, min(o, c) - spread
            rows.append({"ts": t + timedelta(minutes=m), "open": o, "high": h, "low": l, "close": c,
                         "volume": int(abs(rng.normal(120_000, 40_000))), "oi": 0})
            price = c
    return pd.DataFrame(rows)


def demo_option_chain(spot: float, expiry: date, as_of: datetime, strikes_each_side: int = 12, step: int = 50, seed: int = 7,
                      base_iv: float = 0.13) -> pd.DataFrame:
    """Chain priced with BS + a smile, plus plausible OI/volume. `prev_oi` ~ 95-105% of oi."""
    rng = np.random.default_rng(seed)
    atm = round(spot / step) * step
    t = years_to_expiry(expiry, as_of)
    rows = []
    for k in range(-strikes_each_side, strikes_each_side + 1):
        strike = atm + k * step
        m = (strike - spot) / spot
        for kind in ("CE", "PE"):
            iv = base_iv + 0.9 * m * m * 10 + (0.015 if kind == "PE" else 0) * max(-m, 0) * 20
            iv += rng.normal(0, 0.003)
            mid = bs_price(spot, strike, t, iv, kind)
            spr = max(0.05, mid * rng.uniform(0.003, 0.02))
            dist = abs(k)
            oi = int(rng.lognormal(13.5 - dist * 0.12, 0.4))
            if (kind == "CE" and strike in (atm + 200, atm + 500)) or (kind == "PE" and strike in (atm - 200, atm - 500)):
                oi = int(oi * 2.2)
            rows.append({
                "tradingsymbol": f"NIFTY{expiry.strftime('%y%b').upper()}{int(strike)}{kind}", "instrument_token": 100000 + k * 2 + (0 if kind == "CE" else 1),
                "expiry": expiry, "strike": float(strike), "option_type": kind, "last_price": round(mid + rng.normal(0, spr / 2), 2),
                "bid": round(mid - spr / 2, 2), "ask": round(mid + spr / 2, 2), "volume": int(rng.lognormal(12.5 - dist * 0.15, 0.5)),
                "oi": oi, "prev_oi": int(oi * rng.uniform(0.93, 1.07)), "prev_close": round(mid * rng.uniform(0.9, 1.1), 2),
            })
    return pd.DataFrame(rows)


def demo_constituent_quotes(n: int = 50, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    syms = [f"STOCK{i:02d}" for i in range(n)]
    close = rng.uniform(200, 4000, n)
    ret = rng.normal(0.002, 0.012, n)
    last = close * (1 + ret)
    open_ = close * (1 + rng.normal(0.001, 0.004, n))
    high = np.maximum(last, open_) * (1 + abs(rng.normal(0, 0.004, n)))
    low = np.minimum(last, open_) * (1 - abs(rng.normal(0, 0.004, n)))
    return pd.DataFrame({"tradingsymbol": syms, "last_price": last, "close": close, "open": open_, "high": high, "low": low})


def demo_futures_quote(spot: float, seed: int = 5) -> dict:
    rng = np.random.default_rng(seed)
    last = spot * (1 + rng.uniform(0.0008, 0.0025))
    return {"last_price": last, "close": last * (1 - rng.normal(0.002, 0.004)), "volume": int(rng.uniform(4e6, 9e6)),
            "oi": int(rng.uniform(1.1e7, 1.4e7)), "bid": last - 0.5, "ask": last + 0.5, "bid_qty": int(rng.uniform(500, 4000)),
            "ask_qty": int(rng.uniform(500, 4000))}


def next_weekly_expiry(d: date) -> date:
    """NIFTY weekly expiry convention used by the demo (Tuesday from Sep-2025). Real expiries come from the instrument master."""
    days_ahead = (1 - d.weekday()) % 7
    return d + timedelta(days=days_ahead if days_ahead else 7)
