"""Zerodha NFO options cost model (brokerage, STT, exchange txn, SEBI, stamp duty, GST, slippage).

Rates live in settings.yaml -> costs and must be re-verified against https://zerodha.com/charges.
"""

from __future__ import annotations

from dataclasses import dataclass

from mmm.settings import get_config


@dataclass(frozen=True)
class CostBreakdown:
    brokerage: float
    stt: float
    exchange_txn: float
    sebi: float
    stamp: float
    gst: float
    slippage: float
    total: float

    def as_dict(self) -> dict[str, float]:
        return {k: round(v, 2) for k, v in self.__dict__.items()}


def round_trip_costs(buy_premium: float, sell_premium: float, qty: int, include_slippage: bool = True) -> CostBreakdown:
    """Costs for buying `qty` units at buy_premium and selling at sell_premium (one order each side)."""
    c = get_config().costs
    buy_val, sell_val = buy_premium * qty, sell_premium * qty
    turnover = buy_val + sell_val
    brokerage = min(c.brokerage_per_order, 0.03 / 100 * buy_val) + min(c.brokerage_per_order, 0.03 / 100 * sell_val)
    stt = c.stt_sell_pct / 100 * sell_val
    exch = c.exchange_txn_pct / 100 * turnover
    sebi = c.sebi_pct / 100 * turnover
    stamp = c.stamp_buy_pct / 100 * buy_val
    gst = c.gst_pct / 100 * (brokerage + exch + sebi)
    slip = (2 * c.slippage_ticks * c.tick_size * qty) if include_slippage else 0.0
    total = brokerage + stt + exch + sebi + stamp + gst + slip
    return CostBreakdown(brokerage, stt, exch, sebi, stamp, gst, slip, total)


def breakeven_move(premium: float, qty: int) -> float:
    """Premium move (points) needed to cover round-trip costs at this premium."""
    costs = round_trip_costs(premium, premium, qty).total
    return costs / qty
