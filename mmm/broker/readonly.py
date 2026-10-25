"""Read-only Kite Connect adapter.

Every Kite call in this application goes through ReadOnlyKite. It exposes an explicit allowlist of
read methods and raises on anything else, so a bug or a future refactor cannot accidentally place,
modify or cancel an order or touch GTTs.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Methods that may be invoked on the underlying kiteconnect.KiteConnect instance.
ALLOWED_METHODS: frozenset[str] = frozenset(
    {
        # auth / user
        "login_url",
        "generate_session",
        "set_access_token",
        "profile",
        "margins",
        # instruments & quotes
        "instruments",
        "quote",
        "ohlc",
        "ltp",
        "historical_data",
        # read-only account state
        "orders",
        "order_history",
        "trades",
        "order_trades",
        "positions",
        "holdings",
        "get_gtts",
        "get_gtt",
        # margin *calculation* endpoints (no order is created)
        "order_margins",
        "basket_order_margins",
    }
)

# Named explicitly so tests can assert they are blocked.
FORBIDDEN_METHODS: frozenset[str] = frozenset(
    {
        "place_order",
        "modify_order",
        "cancel_order",
        "exit_order",
        "place_gtt",
        "modify_gtt",
        "delete_gtt",
        "convert_position",
        "place_mf_order",
        "cancel_mf_order",
        "place_mf_sip",
        "modify_mf_sip",
        "cancel_mf_sip",
        "invalidate_access_token",
        "invalidate_refresh_token",
        "renew_access_token",
    }
)


class WriteOperationBlocked(RuntimeError):
    """Raised when something tries to reach a trading write endpoint."""


class ReadOnlyKite:
    """Thin allowlist proxy around kiteconnect.KiteConnect with simple rate limiting."""

    def __init__(self, kite: Any, min_interval_s: float = 0.34):
        # kiteconnect.KiteConnect instance (or a test double). Stored privately.
        self.__kite = kite
        self._min_interval_s = min_interval_s
        self._last_call = 0.0

    # --- guard -------------------------------------------------------------------------------
    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name in FORBIDDEN_METHODS:
            log.error("blocked_write_operation", method=name)
            raise WriteOperationBlocked(f"'{name}' is a trading write operation and is prohibited")
        if name not in ALLOWED_METHODS:
            raise AttributeError(f"'{name}' is not in the read-only allowlist")
        target = getattr(self.__kite, name)

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            self._throttle()
            return target(*args, **kwargs)

        return wrapped

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self._min_interval_s:
            time.sleep(self._min_interval_s - elapsed)
        self._last_call = time.monotonic()

    # --- typed conveniences ------------------------------------------------------------------
    def historical(
        self,
        instrument_token: int,
        start: datetime | date,
        end: datetime | date,
        interval: str,
        oi: bool = True,
        continuous: bool = False,
    ) -> list[dict[str, Any]]:
        return self.historical_data(instrument_token, start, end, interval, continuous=continuous, oi=oi)

    def quotes(self, keys: list[str]) -> dict[str, Any]:
        """Batch quote() in chunks of 500 (documented max per request)."""
        out: dict[str, Any] = {}
        for i in range(0, len(keys), 500):
            out.update(self.quote(keys[i : i + 500]))
        return out
