# Architectural audit — toward a measurable, research-driven trading system

**Date:** 2026-09-27 · **Against:** commit `7553fc6` on the droplet
(`/opt/kalshi/trading/kalshi-bot`) · **Scope:** read-only; no code was changed.
**Brief:** "Kalshi research platform" brief of 2026-09-27 (sections 1–38).
**Baseline:** *Kalshi Trading Bot — Architecture review, 27 September 2026*.

Production remains locked. Nothing in this audit or in the proposal at the end
touches `env_guard.py` or any trading path to production.

---

## A. Current architecture map

One process (`arb.py`, systemd unit `kalshibot`), parallel worker threads,
one SQLite journal, one shared market cache. Live path vs legacy code:

| Layer | Live modules (lines) | Notes |
|---|---|---|
| Entry / orchestration | `arb.py` (702), `arb_engine.py` (883), `workers.py` (331) | Workers on own clocks; heartbeat; supervisor |
| Safety | `env_guard.py` (201), `config.py` (187) | Production lockout (5 layers); strict YAML (duplicate keys refused) |
| Exchange access | `client.py` (367), `clients.py` (240), `auth.py` (81), `livebooks.py` (169) | V2 orders with YES-book translation; read-only production reader |
| Market state | `market_state.py` (469), `wsfeed.py` (495), `wsproto.py` (259), `depth.py` (221) | Shared cache; live WS feed (demo) since 2026-09-27 |
| Discovery | `discovery.py` (298), `liquidity.py` (135) | Focused series + full 70k-market paging for arbitrage |
| Opportunity model | `opportunity.py` (163), `quantity.py` (90), `edge.py` (110) | `ArbitrageOpportunity` is already the common object for both strategies |
| Strategies | `strategies/arbitrage.py` (500), `strategies/forecast.py` (590) | 3 structural detectors; forecast wrapper around estimators |
| Estimators / data | `estimators/weather.py` (375), `nowcast.py` (181), `weatherdata.py` (595), `estimators/llm.py` (119, off) | Ensemble + HRRR nowcast; NWS obs; provenance |
| Gating | `gate.py` (237), `data_gates.py` (143), `market_skill.py` (264), `calibration_gate.py` (251), `coherence.py` (92) | One profitability gate; data, market and calibration gates |
| Execution | `execution_manager.py` (470) | Taker only; thinnest-leg-first; 8 s fill window; unwind; reconcile |
| Positions / settlement | `position_manager.py` (934), `settlement.py` (250) | Exit rules, reduce-only clamp, structural hold; settlement from journal |
| Sources / research | `sources.py` (471), `forecast_scoring.py` (359), `station_bias.py` (188), `execution_probe.py` (115) | Registry, health, provenance; CLI scoring; demo-vs-production books |
| Persistence | `journal.py` (1,250) | 18 tables; migrations additive |
| Ops | `alerts.py` (111), `logging_setup.py`, `scripts/*`, `diagnostics/*` | ntfy alerts; monitor; dashboard; fit/report scripts |
| **Legacy (not on live path)** | `risk.py` (233), `execution.py` (150), `sim_engine.py` (403), `simulate.py` (244) | Used only by `scripts/simulate.py` (retired 2026-09-17 engine). Duplicates risk, broker and fee logic. |

Tests: 41 files, 759 tests; heaviest coverage on arbitrage (59), safety (56),
forecast (33), positions (32), feed (31).

## B. Current data-flow map

**Weather (every 2 min, `weather_temperature` worker)**
```
focused discovery (6 series) -> markets (REST) -> books (MarketState: WS feed, else REST)
 -> WeatherTemperatureEstimator: ensemble (Open-Meteo) | nowcast (HRRR + NWS obs, same-day <=12h)
    [+ NWS gridpoint cross-check; every fetch -> source_observations]
 -> coherence check across the event
 -> ForecastStrategy._price_one: data checks (-> data_checks) -> lead <= 6h -> primary source
    -> data gates -> market-skill gate -> confidence -> edge after taker fee -> disagreement caps
    -> price band -> depth -> spread           [skips: IN MEMORY ONLY]
 -> probability_log (every priced market, hourly dedupe; demo bid/ask)
 -> ArbitrageOpportunity -> ProfitabilityGate -> opportunities table (decision = GATE verdict)
 -> arb_engine._risk_ok (breakers, exposure)  [rejection: LOG ONLY]
 -> ExecutionManager (taker) -> leg_orders, executions (opportunity_id NOT set)
 -> PositionWorker (30 s) exits -> leg_orders ; settlement.py -> SETTLED legs
 -> scoring (hourly, NWS CLI) -> forecasts.actual_f ; settlement -> probability_log.settled_yes
 -> calibration gate + market-skill gate read these back
```
**Arbitrage (every 30 min)** — page ~70,000 markets (~134 s) + liquidity profile
→ batch books → 3 detectors → quantity analysis → gate → opportunities table →
risk → execute (taker) → hold to settlement.

**Feed** — WS → `MarketState` (follows the working set, ≤500 markets).
**Probe** (15 min) — working set → demo book vs public production book → `book_quality`.

## C. Current strategy map

| Strategy | Class (brief §5) | Detection | Status / evidence |
|---|---|---|---|
| SameMarketArbitrage | A structural | YES+NO < $1 on one market | Live; rare |
| LadderInversionArbitrage | A structural | Nested thresholds priced out of order | Live; +$4.00 realised (incl. 8 legs wrongly dumped before 2026-09-26) |
| ExclusiveBasketArbitrage | A structural | NO on every outcome of an exclusive event | Live; rare |
| weather_temperature (ensemble) | E forecast | ~120-member distribution, sigma 1.6°F | Live, **blocked** (market out-forecasts it 0–6 h) |
| weather_temperature (nowcast) | E forecast | Readings + bias-corrected HRRR | Live since 2026-09-26, blocked until proven; 177 forecasts, 0 settled |
| llm estimator | G/D | Scaffold | Off |
| B cross-contract, C cross-market, D latency, F market making | — | — | Not present |

## D. Current risk map

| Layer | Control | Fails safe? |
|---|---|---|
| Environment | Production lockout (5 layers), demo-only order method, config refuses `live` without explicit flags | Yes |
| Start-up | Full self-test + lockout check before trading | Yes (refuses to start) |
| Opportunity | ProfitabilityGate (edge, fees, legs, staleness, execution risk, forecast checks) | Yes |
| Data | 5°F conflict cap enforced; stale/source-down/obs-QC in shadow | Partly |
| Model | Calibration gate (vs climatology), market-skill gate (vs market, require: ahead) | Yes |
| Account | Min balance, daily loss, consecutive losses (resettable), exposure caps, unhedged cap | Yes |
| Execution | Re-price before send, 8 s fill window, unwind on partial | Yes |
| Position | Stop-loss, reduce-only clamp, structural hold, exits never gated | Yes |
| **Gaps vs brief §31** | Position mismatch after a trade is **logged, entries continue**; journal write failure **logged, trading continues**; no fee-uncertainty reject; bot does not check its own clock; config/model mismatch undetectable (not versioned) | **No** |

## E. Current research capabilities

Exists: `probability_log` (every priced forecast, model + alternative
probability, demo bid/ask, settlement), `forecasts` (mean/sigma vs official
actual), `source_observations` (provenance), `data_checks`, `book_quality`
(163 demo-vs-production comparisons), `market_skill` (per-contract Brier vs
market with standard errors), calibration gate, `fit_station_model.py`
(walk-forward bias, sigma sizing), `fit_conflict_threshold.py`, dashboard.

Missing: market-data tape (books/deltas/trades are not stored), opportunity
lifecycle, replay/backtest, fill model, maker simulation, versioning,
scorecards, capital/economics model, confidence intervals outside market_skill.

## F. Current execution capabilities

Taker-only limit orders at the walked VWAP limit, on the V2 endpoint with
YES-book translation; multi-leg thinnest-first, 8 s fill window, unwind,
position reconciliation; taker exits with reduce-only clamp; `post_only`
supported in the client but unused; no resting-order management. Demo only.
Production is readable only through **public REST** (books, trade tape):
a production WebSocket needs a production key, which must not be used — so
production-tape shadowing must poll REST within rate limits.

## G. Quantitative deficiencies (evidence-based)

1. **Not every evaluated opportunity is recorded.** Forecast pre-gate skips
   (lead, data, market-skill, confidence, edge, caps, depth, spread) and
   arbitrage near-misses exist only in logs/memory. Missed-money analysis
   (brief §18) is impossible.
2. **The recorded decision is not the final decision.** `opportunities.decision`
   is the profitability gate's verdict; a later risk rejection still reads
   TRADE (e.g. 12 ladder "TRADE" rows at 02:13 UTC on 26 Sep with no orders —
   the breaker had tripped).
3. **No attribution chain.** 0 of 34 `executions` rows carry an
   `opportunity_id`; P&L cannot be traced to the opportunity, model or
   decision that produced it.
4. **No market-data tape → no replay.** Books, deltas and trades are consumed
   and discarded. Every day without a recorder is a day of replay data lost.
5. **The "market" benchmark is the DEMO market.** Market-skill verdicts compare
   the model with demo prices (71% ask-only). Production prices — the market
   that matters — are publicly readable but not recorded alongside forecasts.
6. **EV is not net of execution.** `ev_usd` = probability × payout − price −
   taker fee. No spread cost beyond the crossed price, no slippage model, no
   fill probability, no adverse selection.
7. **Fees are hard-coded and unversioned.** Kalshi publishes per-series
   `fee_type` (e.g. `quadratic`) and `fee_multiplier`, and a
   `/series/fee_changes` endpoint — none read. Fee uncertainty is not a
   rejection reason.
8. **Nothing is versioned.** No code commit, config hash, model or parameter
   version on any decision; `runs` stores mode and note only.
9. **Sample sizes cannot answer the $1,000/month question.** 30 closed
   positions; 59 contracts in the traded weather window; no bootstrap/CI
   except in the market-skill gate.
10. **No economics.** No capital utilisation, drawdown, profit factor,
    opportunity frequency or capacity figures.
11. **Arbitrage is periodic.** A 30-minute REST sweep that takes ~4 minutes;
    how long a mispricing persists — and so how many are missed — is unmeasured.

## H. Information-system deficiencies

1. One SQLite connection shared by all threads without a lock; transactions
   can interleave (commit from one thread commits another's writes).
2. `journal.py` (1,250 lines) mixes schema, writes, P&L and analytics.
3. Legacy duplicates on disk: `risk.py`, `execution.py`, `sim_engine.py`,
   `simulate.py`; unused-on-live tables `decisions`, `trades`.
4. Timestamps: most rows carry write time only; decision time vs source time
   vs retrieval time exists only for weather provenance (14A).
5. Execution modes are SCAN_ONLY / PAPER / DEMO_LIVE; brief §2 needs DEMO,
   SHADOW, REPLAY, PAPER/production-market simulation, LIMITED and FULL
   PRODUCTION as distinct modes (the last two stay locked).
6. Observability shows P&L, sources and orders, not opportunity flow
   (detected / rejected by stage / executed / missed / expected vs realised EV).
7. Failure modes (brief §31) partly unhandled — see D.

## I. Opportunities for improvement (reuse first)

| Need (brief) | Reuse | Build |
|---|---|---|
| Opportunity object (§4) | `ArbitrageOpportunity`, `Leg`, `Walk`, `quantity.analyse` | Extend with lifecycle, EV breakdown, versions, stage |
| Opportunity DB (§18) | `opportunities` table, `log_opportunity` | Lifecycle table, final decision + stage, links to executions/settlement |
| Replay (§8) | Provenance (14A), probability_log, feed messages | Market-data recorder first; replay engine after data exists |
| Generalised arbitrage (§6) | 3 detectors, depth walk, quantity analysis | Relationship graph; worst/best case; leg risk |
| Event-driven (§7) | WS feed + `MarketState` | Dependency index: ticker → relationships |
| Production shadow (§10–12) | `livebooks`, `execution_probe`, public trade tape | Tape poller, fill simulator, maker shadow (C2 M0) |
| Fee engine (§13) | `fees.py`, config rates | Series `fee_type`/`fee_multiplier`, fee_changes, versioning |
| Scorecards (§19) | dashboard, market_skill stats, pnl_summary | Per-strategy standard report |
| Weather research (§15) | fit_station_model, nowcast, CLI scoring | Walk-forward parameter fitting |

## J. Recommended implementation sequence

The brief's order, with two justified changes:

1. **P1 Opportunity data model** (below).
2. **P2 Opportunity journal** — lifecycle, final decision and stage, links
   opportunity → execution → position → settlement; records every skip.
3. **P3a Market-data recorder + versioning** *(moved up)* — record WS book
   events, production book snapshots and the public trade tape for the working
   set, plus code/config/model versions on every run and decision. Reason: a
   replay engine needs history, history only accumulates forward, and the
   recorder is cheap. Also records **production** mid prices beside every
   forecast, fixing G5.
4. **P8 Fee engine** *(moved up)* — small; every EV number in later packages
   depends on it; Kalshi already publishes the inputs.
5. **P3b Replay engine** — once P3a has accumulated data.
6. P6 production-tape shadow + P7 fill simulator + P9 maker shadow (= C2 M0).
7. P4 generalised arbitrage, P5 event-driven detection.
8. P10 scorecards, P11 capital-efficiency / $1,000-month model.
9. P12 information-latency research, P13 weather research, P14 readiness.
10. Hardening alongside: journal write lock; fail-closed on journal failure,
    position mismatch and fee uncertainty; retire legacy modules.

Package 14A (conflict-threshold fit, due 30 Sep – 2 Oct) and staged C2 fold
into this sequence (C2 M0 = P9).

---

# PACKAGE 1 — Opportunity data model (proposal; not implemented)

## Goal
One standard `Opportunity` object that every strategy emits and every later
package reads, carrying the fields in brief §4 — with **no change to what the
bot trades**. It is a data-model change only.

## Design
- **Extend, don't replace.** `ArbitrageOpportunity` already flows through the
  gate, risk, execution and journal. Add the new fields with safe defaults and
  make `Opportunity` the canonical name (`ArbitrageOpportunity` stays as an
  alias so nothing breaks).
- **New fields:** `opportunity_id` (uuid), `detected_at`, `opportunity_class`
  (enum: STRUCTURAL_ARB, CROSS_CONTRACT, CROSS_MARKET, INFO_LATENCY,
  WEATHER_FORECAST, MARKET_MAKING, STAT_MISPRICING), `direction`,
  `fair_probability`, `market_probability` (+ `market_price_source`:
  demo/production), `guaranteed_value`, `spread_cost`, `estimated_slippage`
  (from the existing depth walk: VWAP − best), `est_fill_probability`,
  `adverse_selection_estimate`, `capital_required`, `max_loss`, `max_profit`,
  `settlement_time`, `holding_period_est`, `liquidity` (depth at best,
  available), `data_quality`, `source_quality` (lowest tier used),
  `model_version`, `config_version`, `code_version`, `fee_schedule_version`,
  `risk_score`, `execution_mode`, `decision`, `decision_stage`
  (MODEL/DATA/MARKET_SKILL/GATE/RISK/LIQUIDITY/EXECUTION), `rejection_reason`.
- **Net EV breakdown** — `ev_breakdown()` returns gross EV, fees, spread cost,
  slippage, adverse selection, execution loss, risk adjustment and **net EV**,
  with each component marked MEASURED / MODELLED / NOT_MODELLED. Components not
  yet modelled are reported as unknown, never silently zero, so later packages
  can see exactly what is still assumed.
- **Constructors** — `Opportunity.structural(...)` for the three detectors and
  `Opportunity.forecast(...)` for ForecastStrategy fill the new fields from data
  they already compute.
- **Versions** — a small `versioning.py` computes the git commit and a hash of
  `config.yaml` once at start-up; model versions are declared per estimator
  (`weather_temperature/ensemble@1`, `.../nowcast@1`).
- **`to_record()`** — a flat, JSON-safe dict for Package 2.

## Files
`kalshibot/opportunity.py` (extend), new `kalshibot/versioning.py`,
`strategies/arbitrage.py` and `strategies/forecast.py` (fill new fields),
`estimators/weather.py` (model version string). No changes to gate, risk,
execution, journal schema or `env_guard.py`.

## Database changes
None (Package 2 persists the new fields).

## Tests
Construction for each class; net-EV arithmetic; NOT_MODELLED components never
treated as zero; slippage equals the depth walk; fingerprint unchanged for
existing structures; `to_record()` JSON round-trip; version strings present;
**regression:** existing 759 tests unchanged, and the gate/risk decisions for
a fixed set of recorded opportunities are identical before and after.

## Acceptance criteria
1. Full suite passes (759 + new), locally and on the server.
2. After deployment the bot's trade/reject decisions are unchanged (gate and
   risk code untouched; verified by the regression test and by comparing
   rejection counts over the first hours).
3. Every opportunity carries an id, class, net-EV breakdown and versions.
4. Production lockout untouched (diff shows no change to `env_guard.py`).

## Risks
Low. Touches the two places opportunities are built; mitigated by defaults,
the alias and the regression test. No behaviour, schema or safety change.

## How it moves toward validated expectancy
Every later package — the journal, replay, fill model, scorecards, the
$1,000/month model — needs one object that says what was expected, from which
model and version, and what was assumed rather than measured. Package 1 makes
"what did we think this was worth, and how sure were we of each component"
explicit on every opportunity.

**Awaiting approval before implementation.**
