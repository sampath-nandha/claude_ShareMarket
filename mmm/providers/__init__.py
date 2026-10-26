"""Market-data providers.

Zerodha (Kite) is the broker: read-only, instrument master of record, fallback history. TrueData and Accelpix
are licensed data vendors used for live ticks and for history Zerodha cannot serve (expired option contracts).
Every provider speaks the same small interface (see `base`), so the collector, backfill and verify code never
know which vendor they are talking to. No provider can place an order: none of them has a trading API.
"""

from __future__ import annotations

from typing import Any

PROVIDER_NAMES = ("zerodha", "truedata", "accelpix")


def history_provider(name: str, **kwargs: Any):
    """Instantiate a history provider by name (credentials resolved from the vendor store)."""
    name = name.lower()
    if name == "truedata":
        from mmm.providers.truedata import TrueDataClient

        return TrueDataClient.from_store(**kwargs)
    if name == "accelpix":
        from mmm.providers.accelpix import AccelpixClient

        return AccelpixClient.from_store(**kwargs)
    if name == "zerodha":
        from mmm.providers.zerodha import ZerodhaHistory

        return ZerodhaHistory.from_store(**kwargs)
    raise ValueError(f"unknown provider '{name}' (expected one of {PROVIDER_NAMES})")


def live_feed(name: str, symbol_map: dict[str, int], **kwargs: Any):
    """Instantiate a live feed by name. `symbol_map` maps vendor symbol -> internal instrument token."""
    name = name.lower()
    if name == "truedata":
        from mmm.providers.truedata import TrueDataFeed

        return TrueDataFeed.from_store(symbol_map, **kwargs)
    if name == "accelpix":
        from mmm.providers.accelpix import AccelpixFeed

        return AccelpixFeed.from_store(symbol_map, **kwargs)
    if name == "zerodha":
        from mmm.providers.zerodha import KiteFeed

        return KiteFeed.from_store(symbol_map, **kwargs)
    raise ValueError(f"unknown provider '{name}' (expected one of {PROVIDER_NAMES})")
