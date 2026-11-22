"""Phase 4: out-of-sample validation of Release-1 market-reading features, and the gate that lets them score.

Each feature is a fixed, pre-declared hypothesis: a rule that, at a decision time, says "price should move in
direction d over the next H minutes". Nothing is fitted. For every session the first decision time where the
rule is active becomes one event; its outcome is the NIFTY index move from the next minute's open to the close
H minutes later (or the session close), net of a round-trip cost in index points. Sessions are chronological:
the first `train_days` are reported as in-sample, the rest of the work range is out-of-sample, and the final
`holdout_days` are never touched (the same holdout as strategy research).

A feature PASSES only with >= `min_events` OOS events, a one-sided 95% Wilson lower bound of the hit rate above
the target (51%), and a positive mean OOS move after costs. Only PASS features change a signal's score
(`adjustments`); every other feature is shown as information and ignored.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import structlog

from mmm.backtest.metrics import wilson_lower_one_sided
from mmm.data.state_db import StateDB
from mmm.settings import get_config
from mmm.timeutil import IST, now_ist, session_open

log = structlog.get_logger(__name__)

KEY = "feature_status"
COST_POINTS = 2.0                  # round-trip slippage proxy in index points (matches the backtest default)
MIN_EVENTS = 100


@dataclass(frozen=True)
class Feature:
    key: str
    phase: int
    group: str                      # session | futures | options  (which score component it feeds)
    horizon: int                    # minutes; 0 = to the session close
    times: tuple[time, ...]         # decision times (bar closes) at which the rule is evaluated
    description: str
    rule: Callable[[Any], int]      # ctx -> +1 / -1 / 0 (inactive)


def _every(start: time, end: time, step: int = 15) -> tuple[time, ...]:
    out, t = [], datetime(2000, 1, 1, start.hour, start.minute)
    while t.time() <= end:
        out.append(t.time())
        t += timedelta(minutes=step)
    return tuple(out)


# --- rules (each reads only ctx, i.e. data before the decision time) -------------------------------------------
def _gap(ctx):
    g = ctx.extras.get("gap")
    return g if g is not None and g.direction != 0 and g.atr_multiple is not None else None


def r_gap_fade(ctx) -> int:
    g = _gap(ctx)
    return -g.direction if g is not None and abs(g.atr_multiple) >= 0.3 and not g.filled else 0


def r_gap_outside_go(ctx) -> int:
    g = _gap(ctx)
    return g.direction if g is not None and g.kind.endswith("OUTSIDE") else 0


def r_open_drive(ctx) -> int:
    o = ctx.extras.get("open15")
    return o.direction if o is not None and o.code in ("OPEN_DRIVE", "OPEN_TEST_DRIVE") else 0


def r_open_rejection(ctx) -> int:
    o = ctx.extras.get("open15")
    return o.direction if o is not None and o.code == "OPEN_REJECTION_REVERSE" else 0


def r_narrow_cpr_trend(ctx) -> int:
    c, o = ctx.extras.get("cpr"), ctx.extras.get("open30")
    if c is None or c.width_class != "NARROW" or o is None or o.code in ("FORMING", "UNKNOWN"):
        return 0
    return 1 if o.close > o.open else (-1 if o.close < o.open else 0)


def r_ib_break(ctx) -> int:
    ib = ctx.extras.get("ib60")
    if ib is None or not ib.complete or ctx.intraday_today.empty:
        return 0
    last = ctx.intraday_today.iloc[-1]
    if ib.status == "EXT_UP" and float(last["close"]) > ib.high:
        return 1
    if ib.status == "EXT_DOWN" and float(last["close"]) < ib.low:
        return -1
    return 0


def r_basis_divergence(ctx) -> int:
    b = ctx.extras.get("basis")
    if b is None or b.divergence is None:
        return 0
    return -1 if b.divergence.kind == "BEARISH" else 1


def r_premium_extreme(ctx) -> int:
    b = ctx.extras.get("basis")
    if b is None or b.z is None or abs(b.z) < 2:
        return 0
    return 1 if b.z > 0 else -1


def r_pcr_trend(ctx) -> int:
    o = ctx.extras.get("oi")
    if o is None:
        return 0
    return {"RISING": 1, "FALLING": -1}.get(o.pcr_trend, 0)


def r_wall_unwinding(ctx) -> int:
    o = ctx.extras.get("oi")
    if o is None:
        return 0
    step = get_config().universe.strike_step
    if o.call_wall is not None and o.call_wall.status == "UNWINDING" and 0 <= o.call_wall.strike - o.spot <= 2 * step:
        return 1
    if o.put_wall is not None and o.put_wall.status == "UNWINDING" and 0 <= o.spot - o.put_wall.strike <= 2 * step:
        return -1
    return 0


def r_wall_defended(ctx) -> int:
    o = ctx.extras.get("oi")
    if o is None:
        return 0
    step = get_config().universe.strike_step
    if o.call_wall is not None and o.call_wall.status == "DEFENDED" and 0 <= o.call_wall.strike - o.spot <= step:
        return -1
    if o.put_wall is not None and o.put_wall.status == "DEFENDED" and 0 <= o.spot - o.put_wall.strike <= step:
        return 1
    return 0


INTRADAY = _every(time(9, 45), time(14, 30))
FEATURES: list[Feature] = [
    Feature("gap_fade", 1, "session", 60, (time(9, 20),), "gap >= 0.3 daily ATR, unfilled at 09:20 -> trade towards the fill for 60 min", r_gap_fade),
    Feature("gap_outside_go", 1, "session", 0, (time(9, 30),), "open beyond the previous day's range -> gap direction to the close", r_gap_outside_go),
    Feature("open_drive", 1, "session", 60, (time(9, 30),), "15-minute open drive / test drive -> drive direction for 60 min", r_open_drive),
    Feature("open_rejection_reverse", 1, "session", 60, (time(9, 30),), "15-minute open rejection-reverse -> away from the rejected side, 60 min", r_open_rejection),
    Feature("narrow_cpr_trend", 1, "session", 0, (time(9, 45),), "narrow CPR (bottom 20% of 60 days) -> direction of the first 30 minutes, to the close", r_narrow_cpr_trend),
    Feature("ib60_break", 1, "session", 60, _every(time(10, 15), time(14, 0)), "first close beyond the 60-minute initial balance -> break direction, 60 min", r_ib_break),
    Feature("basis_divergence", 2, "futures", 30, INTRADAY, "premium-over-fair divergence at a 30m extreme -> reversal direction, 30 min", r_basis_divergence),
    Feature("premium_extreme", 2, "futures", 30, INTRADAY, "premium z-score beyond +/-2 -> premium direction, 30 min", r_premium_extreme),
    Feature("pcr_near_trend", 3, "options", 30, INTRADAY, "near-ATM PCR rising/falling over 30 min -> up/down, 30 min", r_pcr_trend),
    Feature("wall_unwinding", 3, "options", 30, INTRADAY, "nearest wall unwinding within 2 strikes -> through the wall, 30 min", r_wall_unwinding),
    Feature("wall_defended", 3, "options", 30, INTRADAY, "nearest wall defended within 1 strike -> away from the wall, 30 min", r_wall_defended),
]
BY_KEY = {f.key: f for f in FEATURES}


def _forward_move(minute: pd.DataFrame, ts_ns: np.ndarray, as_of: datetime, horizon: int, d: int) -> float | None:
    i = int(np.searchsorted(ts_ns, np.datetime64(pd.Timestamp(as_of).tz_convert("UTC").tz_localize(None), "ns"), side="left"))
    if i >= len(minute) or minute["ts"].iloc[i].date() != as_of.date():
        return None
    entry = float(minute["open"].iloc[i])
    day = as_of.date()
    if horizon:
        end_t = as_of + timedelta(minutes=horizon)
        j = int(np.searchsorted(ts_ns, np.datetime64(pd.Timestamp(end_t).tz_convert("UTC").tz_localize(None), "ns"), side="left")) - 1
    else:
        j = i
        while j + 1 < len(minute) and minute["ts"].iloc[j + 1].date() == day:
            j += 1
    if j < i or minute["ts"].iloc[j].date() != day:
        return None
    return d * (float(minute["close"].iloc[j]) - entry)


def collect_events(bars: pd.DataFrame, aux=None, sessions: list | None = None, progress=None) -> pd.DataFrame:
    """One event per (feature, session): the first decision time at which the rule is active."""
    from mmm.signals.engine import FrameCache, build_context

    scfg = get_config().strategies
    minute = bars.sort_values("ts").reset_index(drop=True)
    ts_ns = minute["ts"].dt.tz_convert(None).to_numpy()
    cache = FrameCache(minute, scfg.setup_timeframe, scfg.context_timeframe, scfg.trend_timeframe)
    days = sessions or sorted(minute["ts"].dt.date.unique())
    times = sorted({t for f in FEATURES for t in f.times})
    rows = []
    for k, day in enumerate(days):
        pending = {f.key for f in FEATURES}
        for t in times:
            if not pending:
                break
            as_of = datetime.combine(day, t, tzinfo=IST)
            if as_of <= session_open(day):
                continue
            ctx = None
            for f in FEATURES:
                if f.key not in pending or t not in f.times:
                    continue
                if ctx is None:
                    ctx = build_context(None, as_of, cache=cache, aux=aux)
                    if ctx is None:
                        break
                try:
                    d = f.rule(ctx)
                except Exception as exc:  # noqa: BLE001 - one broken rule must not stop the study
                    log.warning("feature_rule_error", feature=f.key, error=str(exc))
                    d = 0
                if d:
                    mv = _forward_move(minute, ts_ns, as_of, f.horizon, d)
                    if mv is not None:
                        rows.append({"feature": f.key, "session": day, "ts": as_of, "direction": d, "move": mv,
                                     "net": mv - COST_POINTS, "hit": mv - COST_POINTS > 0})
                    pending.discard(f.key)
        if progress and (k + 1) % 25 == 0:
            progress(k + 1, len(days))
    return pd.DataFrame(rows, columns=["feature", "session", "ts", "direction", "move", "net", "hit"])


def evaluate(events: pd.DataFrame, sessions: list, train_days: int, min_events: int = MIN_EVENTS) -> dict[str, dict[str, Any]]:
    v = get_config().validation
    cut = sessions[train_days] if len(sessions) > train_days else None
    out: dict[str, dict[str, Any]] = {}
    for f in FEATURES:
        e = events[events["feature"] == f.key] if not events.empty else events
        ins = e[e["session"] < cut] if cut is not None else e
        oos = e[e["session"] >= cut] if cut is not None else e.iloc[0:0]

        def _s(x: pd.DataFrame) -> dict[str, Any]:
            n = len(x)
            wins = int(x["hit"].sum()) if n else 0
            return {"n": n, "hit_rate": wins / n if n else None, "lb": wilson_lower_one_sided(wins, n, v.confidence_level) if n else None,
                    "mean_net_pts": float(x["net"].mean()) if n else None, "mean_move_pts": float(x["move"].mean()) if n else None}

        s_in, s_out = _s(ins), _s(oos)
        if s_out["n"] < min_events:
            status, why = "INSUFFICIENT", f"{s_out['n']} OOS events < {min_events}"
        elif (s_out["lb"] or 0) > v.target_win_rate and (s_out["mean_net_pts"] or 0) > 0:
            status, why = "PASS", f"OOS hit {s_out['hit_rate']:.1%} (LB {s_out['lb']:.1%}) mean {s_out['mean_net_pts']:+.1f} pts after costs"
        else:
            status, why = "FAIL", f"OOS hit {s_out['hit_rate']:.1%} (LB {s_out['lb']:.1%}) mean {s_out['mean_net_pts']:+.1f} pts after costs"
        out[f.key] = {"status": status, "reason": why, "phase": f.phase, "group": f.group, "horizon": f.horizon,
                      "description": f.description, "in_sample": s_in, "oos": s_out}
    return out


def run_study(bars: pd.DataFrame, aux=None, train_days: int = 120, holdout_days: int = 60, state: StateDB | None = None,
              out_dir: Path | None = None, min_events: int = MIN_EVENTS, progress=None) -> dict[str, Any]:
    from mmm.backtest.engine import WARMUP_SESSIONS
    from mmm.data import paths

    state = state or StateDB()
    sessions = sorted(bars["ts"].dt.date.unique())
    usable = sessions[WARMUP_SESSIONS:]
    work = usable[: len(usable) - holdout_days] if holdout_days and len(usable) > holdout_days else usable
    events = collect_events(bars, aux, work, progress)
    res = evaluate(events, work, min(train_days, max(len(work) - 1, 0)), min_events)
    state.set_kv(KEY, {k: {**v, "updated_at": now_ist().isoformat()} for k, v in res.items()})
    report = {"generated_at": now_ist().isoformat(), "sessions": len(work), "holdout_sessions": len(usable) - len(work),
              "train_days": train_days, "min_events": min_events, "cost_points": COST_POINTS,
              "aux": aux.coverage() if aux is not None else None, "features": res}
    out_dir = out_dir or (paths.data_dir() / "reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_ist().strftime("%Y%m%d_%H%M")
    (out_dir / f"features_{stamp}.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (out_dir / f"features_{stamp}.md").write_text(_markdown(report), encoding="utf-8")
    events.to_csv(out_dir / f"features_{stamp}_events.csv", index=False)
    report["paths"] = [str(out_dir / f"features_{stamp}.json"), str(out_dir / f"features_{stamp}.md")]
    return report


def _markdown(r: dict[str, Any]) -> str:
    def pct(x):
        return "–" if x is None else f"{x:.1%}"

    def pts(x):
        return "–" if x is None else f"{x:+.1f}"

    intro = (f"Work range {r['sessions']} sessions (first {r['train_days']} in-sample), holdout {r['holdout_sessions']} untouched. "
             f"Cost {r['cost_points']} pts per event. PASS needs >= {r['min_events']} OOS events, Wilson LB > target, mean > 0.")
    lines = [f"# Feature study {r['generated_at']}", "", intro, "",
             "| Feature | Phase | Horizon | IS n | IS hit | OOS n | OOS hit | OOS LB | OOS mean pts | Status |", "|---|---|---|---|---|---|---|---|---|---|"]
    for k, v in r["features"].items():
        i, o = v["in_sample"], v["oos"]
        lines.append(f"| {k} | {v['phase']} | {v['horizon'] or 'close'} | {i['n']} | {pct(i['hit_rate'])} | {o['n']} | {pct(o['hit_rate'])} | "
                     f"{pct(o['lb'])} | {pts(o['mean_net_pts'])} | {v['status']} |")
    lines += ["", "Definitions:"] + [f"- **{k}**: {v['description']}" for k, v in r["features"].items()]
    return "\n".join(lines)


def statuses(state: StateDB | None = None) -> dict[str, dict[str, Any]]:
    try:
        return (state or StateDB()).get_kv(KEY, {}) or {}
    except Exception:  # noqa: BLE001 - no data dir yet
        return {}


def adjustments(ctx, direction: int, registry: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Score adjustments from features active at ctx. Only PASS features count; others are listed as ignored.

    Returns pa_delta (points on the 0-100 price-action score), futures/options votes (-1..+1) and notes.
    """
    registry = statuses() if registry is None else registry
    out: dict[str, Any] = {"pa_delta": 0.0, "futures": 0.0, "options": 0.0, "evidence": [], "contradictions": [], "ignored": []}
    for f in FEATURES:
        try:
            d = f.rule(ctx)
        except Exception:  # noqa: BLE001
            d = 0
        if not d:
            continue
        st = registry.get(f.key, {}).get("status", "UNTESTED")
        agree = d == direction
        if st != "PASS":
            out["ignored"].append(f"{f.key} ({'agrees' if agree else 'disagrees'}; {st.lower()}, not used)")
            continue
        sign = 1 if agree else -1
        note = f"validated feature {f.key} {'agrees' if agree else 'disagrees'}"
        (out["evidence"] if agree else out["contradictions"]).append(note)
        if f.group == "session":
            out["pa_delta"] += 5 * sign
        else:
            out[f.group] = float(np.clip(out[f.group] + 0.5 * sign, -1, 1))
    return out
