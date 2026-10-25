"""Strategy status registry (Part H10), persisted in the state DB.

EXPERIMENTAL -> BACKTESTING -> VALIDATING -> {INSUFFICIENT_EVIDENCE | REJECTED | ACCEPTED_FOR_PAPER_TRADING}
-> FORWARD_VALIDATED (only via forward paper results, never from backtests) -> RETIRED.
Status advances only through `record_backtest` / `record_forward`, which store the evidence alongside.
"""

from __future__ import annotations

from typing import Any

from mmm.backtest.metrics import Stats
from mmm.data.state_db import StateDB
from mmm.settings import get_config
from mmm.timeutil import now_ist

KEY = "strategy_status"
ORDER = ["EXPERIMENTAL", "BACKTESTING", "VALIDATING", "INSUFFICIENT_EVIDENCE", "REJECTED", "ACCEPTED_FOR_PAPER_TRADING",
         "FORWARD_VALIDATED", "RETIRED"]
ACTIONABLE = {"ACCEPTED_FOR_PAPER_TRADING", "FORWARD_VALIDATED"}


def statuses(state: StateDB | None = None) -> dict[str, dict[str, Any]]:
    return (state or StateDB()).get_kv(KEY, {}) or {}


def _save(data: dict[str, dict[str, Any]], state: StateDB) -> None:
    state.set_kv(KEY, data)


def set_status(strategy_id: str, status: str, reason: str, evidence: dict[str, Any] | None = None, state: StateDB | None = None) -> None:
    assert status in ORDER, status
    state = state or StateDB()
    data = statuses(state)
    data[strategy_id] = {"status": status, "reason": reason, "updated_at": now_ist().isoformat(), "evidence": evidence or {}}
    _save(data, state)
    state.audit("registry", "status_change", strategy=strategy_id, status=status, reason=reason)


def record_backtest(strategy_id: str, oos: Stats, in_sample: Stats | None = None, state: StateDB | None = None) -> str:
    """Advance status from out-of-sample walk-forward statistics. Backtests can never grant FORWARD_VALIDATED."""
    state = state or StateDB()
    cur = statuses(state).get(strategy_id, {}).get("status", "EXPERIMENTAL")
    if cur in ("FORWARD_VALIDATED", "RETIRED"):
        return cur  # forward evidence outranks new backtests; retire manually
    v = get_config().validation
    ev = {"oos": oos.as_dict(), "in_sample": in_sample.as_dict() if in_sample else None}
    if oos.n < v.min_trades_per_strategy:
        new, why = "INSUFFICIENT_EVIDENCE", f"{oos.n} OOS trades < {v.min_trades_per_strategy}"
    elif oos.verdict == "ACCEPTED":
        new, why = "ACCEPTED_FOR_PAPER_TRADING", "OOS gates passed: " + ", ".join(f"{k}={v_}" for k, v_ in oos.as_dict().items() if k in ("win_rate", "win_rate_ci_low", "expectancy", "profit_factor"))
    else:
        new, why = "REJECTED", "; ".join(oos.reasons)
    set_status(strategy_id, new, why, ev, state)
    return new


def record_forward(strategy_id: str, forward: Stats, state: StateDB | None = None) -> str:
    """Forward paper-trading evidence. Requires prior ACCEPTED_FOR_PAPER_TRADING."""
    state = state or StateDB()
    cur = statuses(state).get(strategy_id, {}).get("status", "EXPERIMENTAL")
    if cur != "ACCEPTED_FOR_PAPER_TRADING":
        return cur
    v = get_config().validation
    if forward.n >= max(50, v.min_trades_per_strategy // 4) and forward.verdict != "REJECTED" and (forward.win_rate or 0) > v.target_win_rate and (forward.expectancy or 0) > 0:
        set_status(strategy_id, "FORWARD_VALIDATED", f"forward n={forward.n} win_rate={forward.win_rate:.3f} expectancy={forward.expectancy:.0f}", {"forward": forward.as_dict()}, state)
        return "FORWARD_VALIDATED"
    if forward.verdict == "REJECTED":
        set_status(strategy_id, "REJECTED", "forward paper results failed: " + "; ".join(forward.reasons), {"forward": forward.as_dict()}, state)
        return "REJECTED"
    return cur


def is_actionable(strategy_id: str, state: StateDB | None = None) -> bool:
    return statuses(state).get(strategy_id, {}).get("status") in ACTIONABLE
