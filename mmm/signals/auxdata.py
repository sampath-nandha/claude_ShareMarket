"""Auxiliary market data for the signal context: futures parity (Phase 2) and option OI flow (Phase 3).

`AuxData` is built once over a whole history (backtest) or from the live archive (dashboard), and every
`build_context` call reads only rows strictly before `as_of`. When the data does not cover a day, the extras
are simply absent and strategies that need them stay silent - nothing is guessed.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd
import structlog

from mmm.analytics.basis import basis_frame, basis_state
from mmm.timeutil import IST

log = structlog.get_logger(__name__)


def _ns(ts: Any) -> np.datetime64:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize(IST)
    return np.datetime64(t.tz_convert("UTC").tz_localize(None), "ns")


class AuxData:
    def __init__(self, basis: pd.DataFrame | None = None, chain: Any = None):
        self.basis = basis.sort_values("ts").reset_index(drop=True) if basis is not None and not basis.empty else None
        if self.basis is not None:
            self._b_ts = self.basis["ts"].dt.tz_convert(None).to_numpy()
            self._b_days = set(self.basis["ts"].dt.date.unique())
        else:
            self._b_ts, self._b_days = np.array([], dtype="datetime64[ns]"), set()
        self.chain = chain

    # --- construction -------------------------------------------------------------------------------
    @classmethod
    def from_archive(cls, index_minutes: pd.DataFrame, start: date | None = None, end: date | None = None,
                     with_chain: bool = True) -> AuxData | None:
        from mmm.data.futures_series import continuous_futures

        basis = None
        try:
            fut = continuous_futures(start=start, end=end)
            if not fut.empty and index_minutes is not None and not index_minutes.empty:
                basis = basis_frame(index_minutes, fut)
        except Exception as exc:  # noqa: BLE001 - missing futures history disables basis features only
            log.warning("aux_basis_unavailable", error=str(exc))
        chain = None
        if with_chain:
            try:
                from mmm.analytics.oi_flow import ChainHistory

                chain = ChainHistory.from_archive(start=start, end=end)
                if chain is not None and chain.empty:
                    chain = None
            except Exception as exc:  # noqa: BLE001
                log.warning("aux_chain_unavailable", error=str(exc))
        if basis is None and chain is None:
            return None
        return cls(basis, chain)

    def slice(self, lo: date, hi: date) -> AuxData:
        """Subset for one worker process (keeps pickles small)."""
        b = None
        if self.basis is not None:
            d = self.basis["ts"].dt.date
            b = self.basis[(d >= lo) & (d <= hi)]
        c = self.chain.slice(lo, hi) if self.chain is not None else None
        return AuxData(b, c)

    def coverage(self) -> dict[str, Any]:
        return {"basis_days": len(self._b_days), "basis_first": str(min(self._b_days)) if self._b_days else None,
                "basis_last": str(max(self._b_days)) if self._b_days else None,
                "chain_days": self.chain.n_days if self.chain is not None else 0}

    # --- causal access ------------------------------------------------------------------------------
    def basis_today(self, as_of: datetime) -> pd.DataFrame | None:
        if self.basis is None or as_of.date() not in self._b_days:
            return None
        d = as_of.date()
        lo = int(np.searchsorted(self._b_ts, _ns(datetime(d.year, d.month, d.day, tzinfo=IST)), side="left"))
        hi = int(np.searchsorted(self._b_ts, _ns(as_of), side="left"))
        return self.basis.iloc[lo:hi] if hi > lo else None

    def basis_history(self, as_of: datetime, minutes: int = 375 * 3) -> pd.DataFrame | None:
        if self.basis is None:
            return None
        hi = int(np.searchsorted(self._b_ts, _ns(as_of), side="left"))
        return self.basis.iloc[max(0, hi - minutes):hi]

    def enrich(self, ctx) -> None:
        """Attach lazily computed `basis` and (eagerly, because walls are levels) `oi` state to the context."""
        add = getattr(ctx.extras, "add", None)
        as_of = ctx.as_of

        def _basis():
            return basis_state(self.basis_today(as_of)) if self.basis is not None else None

        if add is not None:
            add("basis", _basis)
        else:
            ctx.extras["basis"] = _basis()
        if self.chain is not None and as_of.date() in self.chain.days:
            st = self.chain.state(as_of, ctx.close)
            if st is not None:
                ctx.extras["oi"] = st
                ctx.levels.extend(st.wall_levels())

