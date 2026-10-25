"""NIFTY futures features: basis, OI change, relative volume, VWAP, depth imbalance. Statistical features only -
price/OI combinations are labelled by the classic convention but are NOT treated as proof of positioning."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class FuturesSnapshot:
    last: float | None
    spot: float | None
    basis: float | None
    basis_pct: float | None
    volume: int | None
    rel_volume: float | None
    oi: int | None
    oi_change: int | None
    oi_change_pct: float | None
    price_oi_label: str
    vwap: float | None
    spread: float | None
    spread_bps: float | None
    depth_imbalance: float | None
    confirmation_score: float      # -1..+1 directional confirmation
    notes: list[str]


def price_oi_label(price_chg: float | None, oi_chg: float | None) -> str:
    """Conventional labels. Treated as a feature hypothesis, not a fact about who is positioned."""
    if price_chg is None or oi_chg is None:
        return "UNKNOWN"
    if price_chg > 0 and oi_chg > 0:
        return "LONG_BUILDUP"
    if price_chg < 0 and oi_chg > 0:
        return "SHORT_BUILDUP"
    if price_chg > 0 and oi_chg < 0:
        return "SHORT_COVERING"
    if price_chg < 0 and oi_chg < 0:
        return "LONG_UNWINDING"
    return "FLAT"


def depth_imbalance(bid_qty: float | None, ask_qty: float | None) -> float | None:
    if not bid_qty and not ask_qty:
        return None
    b, a = float(bid_qty or 0), float(ask_qty or 0)
    return (b - a) / (b + a) if (b + a) > 0 else None


def session_vwap(candles: pd.DataFrame) -> float | None:
    """VWAP approximation from 1-minute candles (typical price x volume). Labelled APPROX in UI."""
    if candles.empty or candles["volume"].sum() == 0:
        return None
    tp = (candles["high"] + candles["low"] + candles["close"]) / 3
    return float((tp * candles["volume"]).sum() / candles["volume"].sum())


def futures_snapshot(
    quote: dict,
    spot: float | None,
    prev_close_oi: int | None,
    today_minute_candles: pd.DataFrame,
    avg_volume_same_time: float | None,
    direction_hint: int = 0,
) -> FuturesSnapshot:
    notes: list[str] = []
    last = quote.get("last_price")
    basis = (last - spot) if (last is not None and spot) else None
    basis_pct = (basis / spot * 100) if (basis is not None and spot) else None
    vol = quote.get("volume")
    rel_vol = (vol / avg_volume_same_time) if (vol and avg_volume_same_time) else None
    oi = quote.get("oi")
    oi_chg = (oi - prev_close_oi) if (oi is not None and prev_close_oi) else None
    oi_chg_pct = (oi_chg / prev_close_oi * 100) if (oi_chg is not None and prev_close_oi) else None
    price_chg = (last - quote["close"]) if (last is not None and quote.get("close")) else None
    label = price_oi_label(price_chg, oi_chg)
    vwap = session_vwap(today_minute_candles)
    bid, ask = quote.get("bid"), quote.get("ask")
    spread = (ask - bid) if (bid and ask) else None
    spread_bps = (spread / ((ask + bid) / 2) * 1e4) if spread is not None else None
    imb = depth_imbalance(quote.get("bid_qty"), quote.get("ask_qty"))

    score = 0.0
    if direction_hint != 0:
        if rel_vol is not None:
            score += 0.3 * np.tanh(rel_vol - 1)
            if rel_vol < 0.7:
                notes.append("futures volume below average")
        if label in ("LONG_BUILDUP", "SHORT_COVERING") and direction_hint > 0:
            score += 0.3
        elif label in ("SHORT_BUILDUP", "LONG_UNWINDING") and direction_hint < 0:
            score += 0.3
        elif label not in ("UNKNOWN", "FLAT"):
            score -= 0.2
            notes.append(f"price/OI pattern ({label}) disagrees with direction")
        if vwap is not None and last is not None:
            score += 0.2 * direction_hint * np.sign(last - vwap)
        if imb is not None:
            score += 0.2 * direction_hint * imb
    return FuturesSnapshot(last, spot, basis, basis_pct, vol, rel_vol, oi, oi_chg, oi_chg_pct, label, vwap, spread,
                           spread_bps, imb, float(np.clip(score, -1, 1)), notes)
