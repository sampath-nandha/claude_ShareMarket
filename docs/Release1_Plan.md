# Release 1 — Intraday Market Reading (plan, not yet developed)

Status: **APPROVED PLAN, DEVELOPMENT NOT STARTED.** Recorded 2026-11-22.

Scope: five intraday reading areas, built in four phases ordered by how fast each can be validated.
Every new feature and strategy enters as an EXPERIMENTAL hypothesis and is promoted only through the existing
validation gate (Phase 4).

| Area | Already built | To add in Release 1 |
|---|---|---|
| 1. Spot-futures intraday parity | Live basis (pts, %), futures OI change, price-OI build-up label, futures VWAP, depth imbalance | Basis vs fair value, intraday premium/discount history, divergence detection, basis as a strategy input |
| 2. Option chain reading (OI, PCR) | PCR by OI / volume / OI change, OI change vs previous snapshot, liquidity score, per-minute chain snapshots archived | Per-strike OI change through the day, build-up type per strike, unwinding detection, near-the-money PCR and its trend |
| 3. Intraday index and stock levels | PDH/PDL/PDC, PWH/PWL, open, session H/L, OR5/15/30, round numbers, scored swing zones | Floor pivots, central pivot range (CPR), confluence clusters, pre-open level sheet, heavyweight stock levels |
| 4. Option support/resistance | Max pain, call/put walls, top OI strikes | Wall migration through the day, wall strength, walls fed into strategies and chart levels, defended vs unwinding walls |
| 5. Trading the market opening | Opening range breakout (PA04) with gap context | Gap classification, opening type, initial balance, pre-open reading, opening drive and opening trap strategies |

## Phase 1 — Levels and opening (index candles only; 3+ years of history already archived)

- Floor pivots (P, R1–R3, S1–S3), central pivot range and its width, confluence score merging nearby levels from all sources.
- Gap classification: size in ATR, position vs previous day's range (inside / beyond PDH-PDL).
- Opening type at 09:30 and 09:45: open drive, open test drive, open rejection reverse, open auction.
- Initial balance at 15 / 30 / 60 minutes and its extensions.
- Pre-open reading 09:00–09:08 from collector ticks — first confirm from the archive that pre-open ticks exist.
- Strategies: opening drive continuation; opening trap (gap fails to hold the opening range).
- Dashboard: new Pre-Market & Opening page (level sheet, gap type, live opening type, initial balance).

## Phase 2 — Spot-futures parity

- Fair-value basis from spot, configured rate and days to expiry; premium vs fair value as an intraday z-score.
- Divergence: spot new high/low while the premium contracts/expands, combined with futures OI change.
- Check whether Kite serves continuous 1-minute futures history (would give years of basis data instead of months).
- Strategy: basis divergence reversal. Dashboard: basis chart with divergence markers on the Futures page.

## Phase 3 — OI, PCR and option walls

- Per-strike OI through the day rebuilt from chain snapshots and option minute candles (which carry OI).
- Build-up type per strike from premium change and OI change; flag a wall as unwinding when its OI falls as price approaches.
- Call/put wall migration through the day; PCR within a few strikes of ATM.
- Walls added to the level list for the false-breakout (PA03) and rejection (PA05) strategies; next wall as a target boundary.
- Strategy: OI wall — defended-wall rejection and unwinding-wall breakout.
- Dashboard: OI change heatmap by strike over time and wall migration chart on the Options page.

## Phase 4 — Validation and scoring

- Walk-forward with untouched holdout for every new feature and strategy.
- Promotion only with >= 200 out-of-sample trades and the 95% lower bound of win rate > 51% after costs.
- Features that pass feed the futures and options components of the signal score.

## Stated limits

- No source identifies who traded. NSE participant-wise OI is outside Zerodha, so "institutional" / "retail" labels are inferences from price and OI and are marked as such.
- Nothing is promised as "pinpoint" or "accurate"; each idea stays EXPERIMENTAL until it passes the gate.
- OI- and basis-based ideas validate slowly: history starts when live capture began, plus the life of each active contract.
- The index has no volume; VWAP and volume reads use NIFTY futures, translated to spot through the basis.
