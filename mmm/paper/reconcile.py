"""Reconcile MANUAL journal entries with broker-observed fills (read-only `trades()` / `positions()`).

Matching: same tradingsymbol, same side, fill within +/- `window_min` of the journal entry time. Produces a table
of signal price vs journal price vs broker average fill, and flags journal trades with no broker fill and broker
fills with no journal entry.
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd

from mmm.data.state_db import StateDB
from mmm.paper.journal import Journal
from mmm.timeutil import to_ist


def reconcile(client, state: StateDB | None = None, window_min: int = 10) -> dict[str, pd.DataFrame]:
    state = state or StateDB()
    j = Journal(state)
    fills = pd.DataFrame(client.trades())
    manual = j.frame("MANUAL")
    if fills.empty:
        fills = pd.DataFrame(columns=["tradingsymbol", "transaction_type", "quantity", "average_price", "fill_timestamp"])
    fills["fill_timestamp"] = pd.to_datetime(fills["fill_timestamp"], errors="coerce").map(lambda x: to_ist(x) if pd.notna(x) else x)
    matched, unmatched_journal = [], []
    used: set[int] = set()
    for _, t in manual.iterrows():
        side = "BUY" if t["direction"] in ("BUY", "LONG", "BULLISH") else "SELL"
        ets = pd.Timestamp(t["entry_ts"]) if t.get("entry_ts") else None
        cand = fills[(fills["tradingsymbol"] == t["instrument"]) & (fills["transaction_type"] == side)]
        if ets is not None:
            cand = cand[(cand["fill_timestamp"] >= ets - timedelta(minutes=window_min)) & (cand["fill_timestamp"] <= ets + timedelta(minutes=window_min))]
        cand = cand[~cand.index.isin(used)]
        if cand.empty:
            unmatched_journal.append(t)
            continue
        f = cand.iloc[0]
        used.add(cand.index[0])
        sig_price = (t.get("payload") or {}).get("signal", {}).get("recommended_option", {}).get("premium") if isinstance(t.get("payload"), dict) else None
        matched.append({"trade_id": t["trade_id"], "instrument": t["instrument"], "side": side, "journal_qty": t["qty"], "broker_qty": f["quantity"],
                        "signal_price": sig_price, "journal_price": t["entry_price"], "broker_fill": f["average_price"],
                        "slippage_vs_signal": (f["average_price"] - sig_price) if sig_price else None, "fill_ts": f["fill_timestamp"]})
    unmatched_broker = fills[~fills.index.isin(used)]
    return {"matched": pd.DataFrame(matched), "journal_without_fill": pd.DataFrame(unmatched_journal), "broker_without_journal": unmatched_broker}
