# MoneyMakingMachine — NIFTY 50 Price-Action & Options Intelligence

Zerodha-only, Windows-native (no containers), read-only decision-support system for discretionary NIFTY option
trading. It captures the maximum useful Kite Connect data, analyses price action across timeframes, reconstructs
the option chain with derived IV/Greeks, generates two-stage signals with explicit *do-not-execute* reasons, and
validates every strategy with honest statistics before anything is called actionable.

**It never places, modifies or cancels orders.** The Kite client is an allowlist proxy (`mmm/broker/readonly.py`)
and `tests/test_readonly.py` statically scans the code base for any write endpoint.

## Quick start (Windows 11)

```powershell
git clone https://github.com/sampath-nandha/claude_ShareMarket
cd claude_ShareMarket
.\deploy\windows\install.ps1                            # elevated PowerShell; data goes to C:\ShareMarketData by default
.\.venv\Scripts\mmm auth set-keys                            # API key/secret -> Windows Credential Manager
.\.venv\Scripts\mmm auth login                               # every trading morning (Kite redirect is captured locally)
.\.venv\Scripts\mmm data instruments                         # instrument master snapshot + universe
.\.venv\Scripts\mmm data backfill --years 3                  # NIFTY / VIX / futures candles -> Parquet
.\.venv\Scripts\mmm dashboard                                # http://localhost:8501 (also runs as the ShareMarket-Web service)
```

The dashboard works before any data exists: it falls back to **clearly labelled synthetic demo data** so you can
explore the UI. Demo data is never written to the archive and never used for validation claims.

Any OS for development: `uv venv --python 3.12 && uv pip install -e ".[dev]" && pytest`.

## Architecture (local, native)

```
Kite REST + WebSocket ──► ShareMarket-Collector (WinSW service)  ──► Parquet (ticks, depth, 1m candles)
                          │                                      ──► SQLite state (quotes, chain, signals, journal, health)
                          └─ ZeroMQ tick bus (127.0.0.1:5557) ──► ShareMarket-Web (FastAPI, WebSocket push to the browser)
                          ShareMarket-Scheduler (WinSW service)  ──► Parquet option_chain snapshots, EOD update (16:00)
Browser (Lightweight Charts + AG Grid) ◄── REST (DuckDB over Parquet + SQLite) + one WebSocket for live ticks
```

Live path: each tick is published by the collector the moment it is normalised and pushed to every open browser
tab; the forming candle, option-chain cells and header update in place (typically 10-50 ms tick-to-screen on the
same PC). Analytics (structure, levels, signals) refresh at each minute close. If the tick bus is unavailable the
web service falls back to polling the state DB every 500 ms.

| Layer | Module |
|---|---|
| Read-only broker, auth, API registry | `mmm/broker/` |
| Instruments, backfill, ticks, collector, DQ | `mmm/data/` |
| Candles, volatility, swings/structure, levels, regime, futures, breadth | `mmm/analytics/` |
| Options: BS pricing/IV/Greeks, chain analytics, selector, payoff | `mmm/analytics/options/` |
| Strategies PA01–PA14 (deterministic, versioned) | `mmm/strategies/` |
| Two-stage signal engine + scoring | `mmm/signals/` |
| Costs (Zerodha NFO) + risk limits | `mmm/risk/` |
| Event-driven backtest, Wilson/bootstrap stats, walk-forward | `mmm/backtest/` |
| Paper / manual journal | `mmm/paper/` |
| Web UI (FastAPI + WebSocket, 12 pages) | `mmm/web/` |
| Windows services & install | `deploy/windows/` |

## Web UI pages (http://localhost:8501)
Market Overview · **Market Condition** (bullish / bearish / sideways gauges: intraday, 60-minute, daily) · **Pre-Market & Opening** (pivots, CPR, confluence clusters, gap, opening type, initial balance,
pre-open auction) · Price Action · **Futures & Basis** (premium vs fair value, z-score, divergence) · **Options
Intelligence** (live chain, OI structure, **OI flow**: wall migration, defended/unwinding walls, near-ATM PCR trend,
OI-change heatmaps; IV smile/term structure, Greeks, ranked contracts, payoff builder, theta planner) · Breadth ·
Live Signals · Risk & Journal · Backtesting · **Research & Validation** · Market Replay · System Health.

## Release 1 (implemented)
See `docs/Release1_Plan.md`. Phase 1 levels & opening (PA11 opening drive, PA12 opening trap), Phase 2 spot-futures
parity (PA13 basis divergence; `mmm data probe-continuous` / `backfill-futures-continuous`), Phase 3 option OI flow
(PA14 OI wall; walls are levels for PA03/PA05), Phase 4 feature study (`mmm research features`, also run by
`mmm research run`): only features that PASS out-of-sample change a signal's score.

## Honesty rules baked in
* Every signal carries `do_not_execute_reasons`; strategies start as `EXPERIMENTAL` and are labelled research-only.
* Acceptance = ≥200 out-of-sample trades, 95% lower bound of win rate > 51% (Wilson and session-block bootstrap),
  positive expectancy after costs, profit factor > 1 — then forward paper trading before `FORWARD_VALIDATED`.
* Backtest fills: next-bar open ± slippage; stop assumed first when stop and target touch in the same bar.
* IV/Greeks/PCR/max-pain/basis/breadth are DERIVED and labelled so; index packets never get fabricated volume/OI.
* Break-even trades stay in the win-rate denominator. Nothing is removed by hand.

## Automatic paper trading (simulated - no orders)
The scheduler service runs `mmm/paper/auto.py` every minute in market hours. At each 5-minute bar close (09:30-14:45)
it takes the best Stage-1 setup from strategies research has not REJECTED, skips it if the risk gate says NO TRADE,
and "buys" the nearest-expiry NIFTY CE/PE closest to the money that fits `paper.capital` (Rs 10,000), at the live
ask. Exits at the bid: underlying stop (checked first) or target 1, a 50% premium stop, 120-minute time stop, or 15:15.
Limits: 3 trades and Rs 3,000 loss per day. Everything is on the **Paper Trading** page with a full decision log.
`mmm paper backtest` (or the page's button) backtests the same rules: direction confidence on all index history,
option confidence where option candles are archived. `mmm paper status | pause | resume`.

## Candlestick pattern trial (from 2026-11-23)
15 patterns (`mmm/analytics/patterns.py`): momentum/continuation (marubozu, three soldiers/crows, inside-bar breakout,
NR7 breakout, three methods), reversal (engulfing, hammer/shooting star, morning/evening star, tweezer, piercing/dark
cloud) and indecision (doji, spinning top, inside bar, harami, high-wave), adapted to gap-free intraday bars. Each
family is tested by `mmm research features` only in its pre-declared context (momentum with the context trend,
reversal at a strong level, indecision after a 1-ATR run) with a Bonferroni-corrected gate; only a PASS changes a
signal's score. The Research page shows a live watch of every in-context event since `patterns.observe_from`.
**Rollback:** set `patterns.enabled: false` in `config/settings.yaml` and restart the ShareMarket-Web service.

## Configuration
`config/settings.yaml` (universe, session, risk, costs, scoring weights, validation gates),
`config/market_holidays.yaml`. Secrets: Credential Manager or `.env` (git-ignored).

## Status
Implemented and tested (`pytest`): R01–R03 data foundation, all ten price-action strategies PA01–PA10 with
same-bar de-duplication, futures/options/breadth analytics, two-stage signal engine, risk and cost models, event-driven
backtester, walk-forward with untouched holdout, out-of-sample probability calibration (gated; `None` until usable),
option-leg P&L evaluation from archived option candles, strategy status registry (H10), broker fill reconciliation,
research report runner (`mmm research run`), 10-page dashboard, Windows services.

Not yet: ML layer (R10), formal documentation set. **No strategy has been validated** — the registry keeps every
strategy EXPERIMENTAL/INSUFFICIENT_EVIDENCE until real archived history produces ≥200 out-of-sample trades that pass
the gates, and only forward paper trading can mark one FORWARD_VALIDATED.

## Bootstrapping data
`mmm data instruments` → `mmm data backfill --years 3` → `mmm data backfill-options` (active contracts only; expired
strikes are not retrievable) → `mmm data options-snapshot` (latest closing chain incl. OI) → `mmm research run`.
`mmm data export-seed` copies the archive into `./seed` for a one-time commit; `mmm data import-seed` restores it.
