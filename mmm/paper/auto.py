"""Automatic paper trading: simulated NIFTY option trades from live signals. No order is ever sent anywhere.

Runs inside the scheduler service once a minute (at :15 s, after the collector has flushed the closed minute).

Each 5-minute bar close, 09:30-14:45:
  1. Build the signal context (same engine as Live Signals) and run Stage 1 on the enabled strategies, minus any
     strategy the research registry marked REJECTED.
  2. Score the best candidate with Stage 2 and skip it if the risk gate says NO_TRADE (reward:risk, spread, data
     quality, signal age). The score itself is logged but not filtered on, so the index backtest of these rules
     (`backtest_paper`) matches apart from live-only gate inputs such as the option spread.
  3. Pick a contract: nearest NIFTY expiry (the next one after 13:00 on expiry day), CE for bullish / PE for bearish,
     the strike closest to ATM whose premium x lot size + costs fits the capital, inside the delta band.
  4. "Buy" at the live ask (LTP + 2 ticks if no ask), as many lots as fit (capped at max_lots).
Every minute while a position is open: exit at the bid when the underlying touches the stop (checked first) or
target 1, the option's bid falls `premium_stop_pct` below entry, the hold time runs out, or at 15:15.
Costs follow the Zerodha options model. Every decision - including the trades not taken and why - is logged.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol

import numpy as np
import pandas as pd
import structlog

from mmm.data.state_db import StateDB
from mmm.paper.journal import Journal
from mmm.risk.costs import round_trip_costs
from mmm.settings import get_config
from mmm.timeutil import IST, is_market_open, now_ist, parse_hhmm

log = structlog.get_logger(__name__)
POS_KEY, PAUSE_KEY, EOD_KEY, SCAN_KEY = "paper_position", "paper_paused", "paper_eod_date", "paper_last_scan"


@dataclass
class Position:
    trade_id: str
    tradingsymbol: str
    token: int
    expiry: str
    strike: float
    option_type: str
    lots: int
    lot_size: int
    qty: int
    entry_price: float
    entry_ts: str
    underlying_entry: float
    stop: float
    target1: float
    target2: float
    direction: str
    strategy_id: str
    signal_id: str
    deadline: str
    premium_stop: float


class Provider(Protocol):
    """Market data the trader needs. LiveProvider reads the archive and state DB; tests supply a fake."""

    def now(self) -> datetime: ...
    def market_open(self) -> bool: ...
    def bundle(self): ...                                   # mmm.web.data.Bundle-like (minute_bars, chain, spot, ...)
    def aux(self, b): ...
    def quote(self, token: int) -> dict[str, Any] | None: ...
    def lot_size(self, token: int) -> int: ...


class LiveProvider:
    def now(self) -> datetime:
        return now_ist()

    def market_open(self) -> bool:
        return is_market_open()

    def bundle(self):
        from mmm.web.data import _load_bundle

        return _load_bundle(40)

    def aux(self, b):
        from mmm.web.data import aux

        return aux(b)

    def quote(self, token: int) -> dict[str, Any] | None:
        rows = StateDB().quotes([int(token)])
        return rows[0] if rows else None

    def lot_size(self, token: int) -> int:
        from mmm.data.instruments import load_instruments

        inst = load_instruments()
        row = inst[inst["instrument_token"] == int(token)] if not inst.empty else inst
        if not row.empty and row.iloc[0].get("lot_size"):
            return int(row.iloc[0]["lot_size"])
        return int(get_config().risk.nifty_lot_size)


def _ts(x: Any) -> datetime:
    t = pd.Timestamp(x)
    return (t.tz_localize(IST) if t.tzinfo is None else t.tz_convert(IST)).to_pydatetime()


def nearest_expiry(expiries: list[date], now: datetime) -> date | None:
    live = sorted(e for e in expiries if e >= now.date())
    if not live:
        return None
    if live[0] == now.date() and now.time() >= time(13, 0) and len(live) > 1:
        return live[1]
    return live[0]


def pick_contract(chain: pd.DataFrame, direction: int, spot: float, expiry: date, budget: float, lot_size_of,
                  delta_band: tuple[float, float], max_lots: int) -> tuple[dict | None, list[str]]:
    """Strike closest to ATM (scanning OTM-wards) whose cost for >= 1 lot fits the budget. Returns (pick, reasons)."""
    kind = "CE" if direction > 0 else "PE"
    df = chain[(pd.to_datetime(chain["expiry"]).dt.date == expiry) & (chain["option_type"] == kind)].copy()
    if df.empty:
        return None, [f"no {kind} quotes for expiry {expiry}"]
    df["dist"] = (df["strike"] - spot) * direction            # >= 0 means OTM for the trade direction
    step = get_config().universe.strike_step
    # ATM and OTM strikes first, nearest to the money first; ITM strikes only after them (they cost more)
    df = df.assign(_itm=df["dist"] < -step / 2, _abs=df["dist"].abs()).sort_values(["_itm", "_abs"])
    reasons = []
    lo, hi = delta_band
    for r in df.itertuples():
        price = r.ask if getattr(r, "ask", None) and r.ask == r.ask and r.ask > 0 else r.last_price
        if not price or pd.isna(price) or price <= 0:
            continue
        d = abs(getattr(r, "delta", np.nan)) if getattr(r, "delta", None) is not None else np.nan
        if not pd.isna(d) and not (lo <= d <= hi):
            reasons.append(f"{r.tradingsymbol}: delta {d:.2f} outside {lo}-{hi}")
            continue
        lot = int(lot_size_of(int(r.instrument_token)))
        one = price * lot + round_trip_costs(price, price, lot).total
        if one > budget:
            reasons.append(f"{r.tradingsymbol}: 1 lot costs Rs {one:,.0f} > budget Rs {budget:,.0f}")
            continue
        lots = int(min(max_lots, max(1, budget // one)))
        while lots > 1 and price * lot * lots + round_trip_costs(price, price, lot * lots).total > budget:
            lots -= 1
        return ({"tradingsymbol": r.tradingsymbol, "token": int(r.instrument_token), "strike": float(r.strike), "option_type": kind,
                 "expiry": str(expiry), "price": float(price), "lot_size": lot, "lots": lots, "delta": None if pd.isna(d) else float(d)}, reasons)
    return None, reasons or ["no quotable contract"]


class PaperTrader:
    def __init__(self, provider: Provider | None = None, state: StateDB | None = None):
        self.cfg = get_config().paper
        self.p = provider or LiveProvider()
        self.state = state or StateDB()
        self.journal = Journal(self.state)

    # --- persistence -------------------------------------------------------------------------------------------
    def log(self, event: str, message: str, now: datetime | None = None, **detail: Any) -> None:
        self.state.paper_log(event, message, ts=now, **detail)
        log.info("paper", paper_event=event, msg=message)

    def position(self) -> Position | None:
        d = self.state.get_kv(POS_KEY)
        return Position(**d) if d else None

    def _set_position(self, p: Position | None) -> None:
        self.state.set_kv(POS_KEY, asdict(p) if p else None)

    def paused(self) -> bool:
        return bool(self.state.get_kv(PAUSE_KEY, False))

    def set_paused(self, paused: bool) -> None:
        self.state.set_kv(PAUSE_KEY, bool(paused))
        self.log("CONTROL", "paused" if paused else "resumed")

    def trades(self) -> pd.DataFrame:
        df = self.journal.frame("PAPER")
        if df.empty:
            return df
        auto = df["payload_json"].fillna("").str.contains('"auto": true') if "payload_json" in df else pd.Series(False, index=df.index)
        return df[auto].copy()

    def equity(self) -> float:
        t = self.trades()
        realised = float(t["pnl_net"].fillna(0).sum()) if not t.empty and "pnl_net" in t else 0.0
        return self.cfg.capital + realised

    def _today_stats(self, d: date) -> tuple[int, float]:
        t = self.trades()
        if t.empty:
            return 0, 0.0
        tod = t[pd.to_datetime(t["entry_ts"], utc=True).dt.tz_convert(IST).dt.date == d]
        return len(tod), float(tod["pnl_net"].fillna(0).sum())

    # --- main loop ---------------------------------------------------------------------------------------------------
    def step(self) -> None:
        if not self.cfg.enabled:
            return
        now = self.p.now().replace(microsecond=0)
        if not self.p.market_open():
            self._eod(now)
            return
        if self.paused():
            return
        b = self.p.bundle()
        if b.is_demo:
            if self.state.get_kv("paper_demo_note") != now.date().isoformat():
                self.log("SKIP", "demo data only - the paper trader needs real captured data", now)
                self.state.set_kv("paper_demo_note", now.date().isoformat())
            return
        pos = self.position()
        if pos is not None:
            self._manage(pos, b, now)
            return
        bar_close = now.replace(second=0) - timedelta(minutes=(now.minute % 5))
        last = self.state.get_kv(SCAN_KEY)
        if last == bar_close.isoformat() or now.minute % 5 != 0:
            return
        self.state.set_kv(SCAN_KEY, bar_close.isoformat())
        self._scan(b, bar_close)

    # --- entries ---------------------------------------------------------------------------------------------------
    def _scan(self, b, as_of: datetime) -> None:
        from mmm.signals.confirm import confirmations
        from mmm.signals.engine import build_context, stage1, stage2

        w0, w1 = (parse_hhmm(x) for x in self.cfg.entry_window)
        if not (w0 <= as_of.time() <= w1):
            return
        n_today, pnl_today = self._today_stats(as_of.date())
        limit = None
        if n_today >= self.cfg.max_trades_per_day:
            limit = f"daily trade limit reached ({n_today}/{self.cfg.max_trades_per_day}); no more entries today"
        elif pnl_today <= -self.cfg.max_daily_loss:
            limit = f"daily loss limit reached (Rs {pnl_today:,.0f}); no more entries today"
        if limit:
            if self.state.get_kv("paper_limit_note") != as_of.date().isoformat():       # say it once per day
                self.state.set_kv("paper_limit_note", as_of.date().isoformat())
                self.log("SKIP", limit, as_of)
            return
        ctx = build_context(b.minute_bars, as_of, aux=self.p.aux(b))
        if ctx is None:
            self.log("SKIP", "not enough history to build the signal context", as_of)
            return
        strategies = paper_strategies()
        cands = stage1(ctx, strategies)
        if not cands:
            self.log("SCAN", f"bar {as_of:%H:%M}: no setup (regime {ctx.regime.code})", as_of, regime=ctx.regime.code)
            return
        strat, cand = max(cands, key=lambda sc: sc[1].pa_score)
        sig = stage2(ctx, strat, cand, confirmations(b, ctx, cand))
        detail = {"strategy": strat.id, "direction": cand.direction, "trigger": cand.trigger_event, "pa_score": cand.pa_score,
                  "score": sig.score, "status": sig.status, "entry": cand.entry, "stop": cand.stop, "target1": cand.target1,
                  "evidence": cand.evidence, "contradictions": sig.contradictions, "others": [s.id for s, _c in cands if s.id != strat.id]}
        if sig.status == "NO_TRADE":
            gate = [r for r in sig.do_not_execute_reasons if "research-only" not in r]
            self.log("SKIP", f"{strat.id} {cand.direction} {cand.trigger_event}: risk gate says NO TRADE", as_of, reasons=gate, **detail)
            return
        expiry = nearest_expiry(b.expiries, as_of)
        if expiry is None or b.chain.empty:
            self.log("SKIP", f"{strat.id} {cand.direction}: no option chain available", as_of, **detail)
            return
        budget = min(self.cfg.capital, self.equity())
        d = 1 if cand.direction == "BULLISH" else -1
        pick, why = pick_contract(b.chain, d, b.spot, expiry, budget, self.p.lot_size, tuple(self.cfg.delta_band), self.cfg.max_lots)
        if pick is None:
            self.log("SKIP", f"{strat.id} {cand.direction}: no {('CE' if d > 0 else 'PE')} within Rs {budget:,.0f}", as_of, reasons=why[:6], **detail)
            return
        q = self.p.quote(pick["token"])
        if q is None or q.get("received_ts") is None or (self.p.now() - _ts(q["received_ts"])).total_seconds() > self.cfg.max_quote_age_s:
            self.log("SKIP", f"{pick['tradingsymbol']}: option quote missing or stale", as_of, **detail)
            return
        tick = get_config().costs.tick_size
        price = float(q.get("ask") or 0) or float(q.get("last_price") or pick["price"]) + 2 * tick
        qty = pick["lots"] * pick["lot_size"]
        tid = self.journal.open_trade("PAPER", pick["tradingsymbol"], "BUY", qty, price, stop=cand.stop, target1=cand.target1,
                                      target2=cand.target2, signal_id=sig.signal_id, entry_ts=as_of, notes=f"auto {strat.id}",
                                      payload={"auto": True, "strategy": strat.id, "direction": cand.direction, "signal": detail, "contract": pick})
        pos = Position(tid, pick["tradingsymbol"], pick["token"], pick["expiry"], pick["strike"], pick["option_type"], pick["lots"], pick["lot_size"],
                       qty, price, as_of.isoformat(), float(b.spot), float(cand.stop), float(cand.target1), float(cand.target2), cand.direction,
                       strat.id, sig.signal_id, (as_of + timedelta(minutes=self.cfg.max_hold_minutes)).isoformat(),
                       round(price * (1 - self.cfg.premium_stop_pct / 100), 2))
        self._set_position(pos)
        self.log("ENTRY", f"BUY {pos.lots} lot(s) {pos.tradingsymbol} @ {price:.2f} (Rs {price * qty:,.0f}) on {strat.id} {cand.direction} "
                 f"{cand.trigger_event}; underlying stop {cand.stop:,.0f} / T1 {cand.target1:,.0f}", as_of, **detail, contract=pick, qty=qty)

    # --- exits -------------------------------------------------------------------------------------------------------
    def _manage(self, pos: Position, b, now: datetime) -> None:
        q = self.p.quote(pos.token) or {}
        bid = float(q.get("bid") or 0) or None
        ltp = float(q.get("last_price") or 0) or None
        mark = bid or ltp
        if mark:
            self.journal.mark(pos.trade_id, mark)
        entry_ts = _ts(pos.entry_ts)
        bars = b.minute_bars[(b.minute_bars["ts"] >= entry_ts) & (b.minute_bars["ts"] < now.replace(second=0))]
        d = 1 if pos.direction == "BULLISH" else -1
        reason = None
        if not bars.empty:
            hit_stop = (bars["low"] <= pos.stop) if d > 0 else (bars["high"] >= pos.stop)
            hit_t1 = (bars["high"] >= pos.target1) if d > 0 else (bars["low"] <= pos.target1)
            i_s = int(np.argmax(hit_stop.to_numpy())) if hit_stop.any() else None
            i_t = int(np.argmax(hit_t1.to_numpy())) if hit_t1.any() else None
            if i_s is not None and (i_t is None or i_s <= i_t):
                reason = "STOP"
            elif i_t is not None:
                reason = "TARGET1"
        if reason is None and mark is not None and mark <= pos.premium_stop:
            reason = "PREMIUM_STOP"
        if reason is None and now >= _ts(pos.deadline):
            reason = "TIME"
        if reason is None and now.time() >= parse_hhmm(self.cfg.force_exit):
            reason = "SESSION_END"
        if reason is None:
            if now.minute % 5 == 0 and mark:
                self.log("MARK", f"{pos.tradingsymbol} bid/ltp {mark:.2f} vs entry {pos.entry_price:.2f} -> "
                         f"Rs {(mark - pos.entry_price) * pos.qty:+,.0f} unrealised; spot {b.spot:,.0f}", now, mark=mark, spot=b.spot)
            return
        if mark is None:
            self.log("WARN", f"exit due ({reason}) but no quote for {pos.tradingsymbol}; retrying next minute", now)
            return
        tick = get_config().costs.tick_size
        exit_price = bid if bid else max(ltp - 2 * tick, tick)
        t = self.journal.close_trade(pos.trade_id, exit_price, reason, exit_ts=now)
        self._set_position(None)
        self.log("EXIT", f"SELL {pos.tradingsymbol} @ {exit_price:.2f} ({reason}); net Rs {t['pnl_net']:+,.0f} after Rs {t['costs']:,.0f} costs; "
                 f"equity Rs {self.equity():,.0f}", now, reason=reason, exit_price=exit_price, pnl_net=t["pnl_net"], pnl_gross=t["pnl_gross"],
                 costs=t["costs"], spot=b.spot)

    def _eod(self, now: datetime) -> None:
        pos = self.position()
        if pos is not None and now.date() > _ts(pos.entry_ts).date():
            self.log("WARN", f"position {pos.tradingsymbol} still open from {pos.entry_ts[:10]} (no quote at the close); close it manually", now)
        if now.time() < time(15, 30) or self.state.get_kv(EOD_KEY) == now.date().isoformat():
            return
        n, pnl = self._today_stats(now.date())
        self.state.set_kv(EOD_KEY, now.date().isoformat())
        if n:
            self.log("EOD", f"{now:%d %b}: {n} trade(s), net Rs {pnl:+,.0f}; equity Rs {self.equity():,.0f}", now, trades=n, pnl=pnl)


def paper_strategies():
    from mmm.strategies import load_strategies

    cfg = get_config()
    strategies = load_strategies(cfg.strategies.enabled)
    if cfg.paper.exclude_rejected:
        strategies = [s for s in strategies if s.status != "REJECTED"]
    return strategies


# --- backtest of the same rules (the "confidence" of the paper strategy) ------------------------------------------------
def backtest_paper(bars: pd.DataFrame, aux=None, days: int | None = None, workers: int = 1, progress=None) -> dict[str, Any]:
    """Index-level backtest of the paper rules (direction confidence, full history) plus an option-level replay with
    the same budget, contract choice and exits wherever archived option candles exist (option confidence)."""
    from mmm.backtest import calibration
    from mmm.backtest.engine import BacktestConfig, run_backtest
    from mmm.backtest.metrics import compute_stats
    from mmm.data import parquet_store
    from mmm.data.instruments import load_instruments

    cfg = get_config()
    pc = cfg.paper
    if days:
        bars = bars[bars["ts"] >= bars["ts"].iloc[-1] - pd.Timedelta(days=days)]
    strategies = [s.id for s in paper_strategies()]
    bt = BacktestConfig(strategies=strategies, max_hold_bars=pc.max_hold_minutes, last_entry=parse_hhmm(pc.entry_window[1]),
                        force_exit=parse_hhmm(pc.force_exit))
    res = run_backtest(bars, bt, research_mode=False, aux=aux, workers=workers, progress=progress, log_every=0)
    t = res.trades
    rules = (f"Same rules as live paper trading: one trade at a time, best setup per bar, entry window {pc.entry_window[0]}-"
             f"{pc.entry_window[1]}, max hold {pc.max_hold_minutes} min, exit by {pc.force_exit}.")
    out: dict[str, Any] = {"generated_at": now_ist().isoformat(), "sessions": res.sessions, "strategies": strategies,
                           "range": [str(bars["ts"].iloc[0].date()), str(bars["ts"].iloc[-1].date())] if not bars.empty else None,
                           "notes": [*res.notes, rules]}
    ds = compute_stats(t) if not t.empty else None
    out["direction"] = (ds.as_dict() | {"reasons": ds.reasons}) if ds is not None else None
    cal = calibration.load("pooled")
    out["calibration"] = {"usable": bool(cal and cal.usable), "method": cal.method if cal else None, "n": cal.n if cal else 0}
    # option replay with the budget
    rows, reasons = [], {}
    cache: dict[int, pd.DataFrame] = {}
    inst_cache: dict[date, pd.DataFrame] = {}
    step = cfg.universe.strike_step
    for tr in t.itertuples() if not t.empty else []:
        e_ts, x_ts = pd.Timestamp(tr.entry_ts), pd.Timestamp(tr.exit_ts)
        dday = e_ts.date()
        if dday not in inst_cache:
            inst = load_instruments(dday)
            inst_cache[dday] = inst[(inst["exchange"] == cfg.universe.fno_exchange) & (inst["name"] == cfg.universe.fno_name)
                                    & inst["instrument_type"].isin(["CE", "PE"])] if not inst.empty else inst
        opts = inst_cache[dday]
        if opts.empty:
            reasons["no instrument snapshot"] = reasons.get("no instrument snapshot", 0) + 1
            continue
        exp = nearest_expiry(sorted(set(opts["expiry"].dt.date)), e_ts.to_pydatetime())
        dirn = 1 if tr.direction == "BULLISH" else -1
        kind = "CE" if dirn > 0 else "PE"
        atm = round(float(tr.entry) / step) * step
        chosen = None
        for k in range(12):
            strike = atm + dirn * k * step
            row = opts[(opts["expiry"].dt.date == exp) & (opts["instrument_type"] == kind) & (opts["strike"] == strike)]
            if row.empty:
                continue
            tok = int(row.iloc[0]["instrument_token"])
            if tok not in cache:
                cache[tok] = parquet_store.read_candles("minute", tok)
            oc = cache[tok]
            eb = oc[oc["ts"] >= e_ts].head(1)
            if eb.empty or eb["ts"].iloc[0] > e_ts + pd.Timedelta(minutes=3):
                continue
            price = float(eb["open"].iloc[0]) + 2 * cfg.costs.tick_size
            lot = int(row.iloc[0].get("lot_size") or cfg.risk.nifty_lot_size)
            if price * lot + round_trip_costs(price, price, lot).total <= pc.capital:
                chosen = (row.iloc[0], oc, price, lot)
                break
        if chosen is None:
            reasons["no archived option within budget"] = reasons.get("no archived option within budget", 0) + 1
            continue
        r, oc, price, lot = chosen
        lots = 1
        while lots < pc.max_lots and price * lot * (lots + 1) + round_trip_costs(price, price, lot * (lots + 1)).total <= pc.capital:
            lots += 1
        win = oc[(oc["ts"] >= e_ts) & (oc["ts"] <= x_ts)]
        stop_px = price * (1 - pc.premium_stop_pct / 100)
        hit = win[win["low"] <= stop_px]
        if not hit.empty and hit["ts"].iloc[0] < x_ts:
            sell, why = stop_px, "PREMIUM_STOP"
        else:
            xb = oc[oc["ts"] <= x_ts].tail(1)
            sell, why = (max(float(xb["close"].iloc[0]) - 2 * cfg.costs.tick_size, cfg.costs.tick_size), tr.exit_reason) if not xb.empty else (None, None)
        if sell is None:
            reasons["option candles missing at exit"] = reasons.get("option candles missing at exit", 0) + 1
            continue
        qty = lots * lot
        costs = round_trip_costs(price, sell, qty, include_slippage=False).total
        pnl = (sell - price) * qty - costs
        rows.append({"entry_ts": e_ts, "exit_ts": x_ts, "strategy_id": tr.strategy_id, "direction": tr.direction, "tradingsymbol": r["tradingsymbol"],
                     "lots": lots, "qty": qty, "buy": price, "sell": sell, "invested": price * qty, "exit_reason": why, "pnl_net": pnl,
                     "pnl_gross": (sell - price) * qty, "costs": costs, "session": e_ts.date()})
    ot = pd.DataFrame(rows)
    out["option"] = {"evaluated": len(ot), "not_evaluated": int(len(t) - len(ot)) if not t.empty else 0, "reasons": reasons,
                     "stats": (compute_stats(ot).as_dict() | {"reasons": compute_stats(ot).reasons}) if not ot.empty else None,
                     "trades": ot.sort_values("entry_ts", ascending=False).head(500).to_dict("records") if not ot.empty else []}
    out["index_trades"] = t.sort_values("entry_ts", ascending=False).head(500).to_dict("records") if not t.empty else []
    return out
