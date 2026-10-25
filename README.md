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
.\deploy\windows\install.ps1 -DataDir D:\ShareMarketData     # elevated PowerShell; creates venv, services, settings
.\.venv\Scripts\mmm auth set-keys                            # API key/secret -> Windows Credential Manager
.\.venv\Scripts\mmm auth login                               # every trading morning (Kite redirect is captured locally)
.\.venv\Scripts\mmm data instruments                         # instrument master snapshot + universe
.\.venv\Scripts\mmm data backfill --years 3                  # NIFTY / VIX / futures candles -> Parquet
.\.venv\Scripts\mmm dashboard                                # http://localhost:8501
```

The dashboard works before any data exists: it falls back to **clearly labelled synthetic demo data** so you can
explore the UI. Demo data is never written to the archive and never used for validation claims.

Any OS for development: `uv venv --python 3.12 && uv pip install -e ".[dev]" && pytest`.

## Architecture (local, native)

```
Kite REST + WebSocket ──► ShareMarket-Collector (WinSW service)  ──► Parquet (ticks, depth, 1m candles)
                          ShareMarket-Scheduler (WinSW service)  ──► SQLite state (quotes, chain, signals, journal, health)
                                                                 ──► Parquet option_chain snapshots
Streamlit dashboard ◄── DuckDB over Parquet + SQLite (WAL, concurrent readers)
```

| Layer | Module |
|---|---|
| Read-only broker, auth, API registry | `mmm/broker/` |
| Instruments, backfill, ticks, collector, DQ | `mmm/data/` |
| Candles, volatility, swings/structure, levels, regime, futures, breadth | `mmm/analytics/` |
| Options: BS pricing/IV/Greeks, chain analytics, selector, payoff | `mmm/analytics/options/` |
| Strategies PA01–PA05 (deterministic, versioned) | `mmm/strategies/` |
| Two-stage signal engine + scoring | `mmm/signals/` |
| Costs (Zerodha NFO) + risk limits | `mmm/risk/` |
| Event-driven backtest, Wilson/bootstrap stats, walk-forward | `mmm/backtest/` |
| Paper / manual journal | `mmm/paper/` |
| Dashboard (10 pages) | `mmm/dashboard/` |
| Windows services & install | `deploy/windows/` |

## Dashboard pages
Market Overview · Price Action · Futures · **Options Intelligence** (chain, OI walls, PCR, max pain, straddle & expected
move, IV smile/term structure, Greeks, ranked contracts with premium R:R and position size, multi-leg payoff builder,
theta & cost planner) · Breadth · Live Signals · Risk & Positions · Backtesting · Market Replay · System Health.

## Honesty rules baked in
* Every signal carries `do_not_execute_reasons`; strategies start as `EXPERIMENTAL` and are labelled research-only.
* Acceptance = ≥200 out-of-sample trades, 95% lower bound of win rate > 51% (Wilson and session-block bootstrap),
  positive expectancy after costs, profit factor > 1 — then forward paper trading before `FORWARD_VALIDATED`.
* Backtest fills: next-bar open ± slippage; stop assumed first when stop and target touch in the same bar.
* IV/Greeks/PCR/max-pain/basis/breadth are DERIVED and labelled so; index packets never get fabricated volume/OI.
* Break-even trades stay in the win-rate denominator. Nothing is removed by hand.

## Configuration
`config/settings.yaml` (universe, session, risk, costs, scoring weights, validation gates),
`config/market_holidays.yaml`. Secrets: Credential Manager or `.env` (git-ignored).

## Status
Releases R01–R03 and the dashboard shell (R08) are implemented and tested (`pytest`: 44 tests). Historical option
capture starts the day the collector runs; validation of the 51% target requires real captured history and has
**not** been demonstrated yet — the backtesting page will say `INSUFFICIENT_EVIDENCE` until it is.
