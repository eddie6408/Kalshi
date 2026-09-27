# Package 2 — Opportunity journal (every evaluated opportunity, its final fate, and its outcome)

**Status: PROPOSED — not implemented.** Awaiting operator approval.
Written 2026-09-27 against commit `bb98eaf` (Package 1 live).
Research brief section 18; audit findings G1, G2, G3 and H1.

Production remains locked; nothing here touches `env_guard.py` or any path
to production.

---

## 1. The question it answers

"How much money did we miss, and why?" - split into model error, data
problems, execution problems, liquidity and risk constraints (brief §18).
Today that cannot be answered, for four measured reasons:

| # | Finding (audit) | Evidence |
|---|---|---|
| G1 | Opportunities rejected before the profitability gate are never stored | Weather: 19 skip reasons in `ForecastStrategy._skip`, held in a list that is discarded every pass (36-72 markets priced per pass). Arbitrage: only the 10 closest near-misses per sweep are kept, in memory |
| G2 | The stored decision is not the final decision | `opportunities.decision` is the gate verdict; a later risk rejection still reads TRADE (12 such rows, 26 Sep 02:13 UTC) |
| G3 | Opportunities are not linked to what happened next | `executions.opportunity_id` set on 0 of 34 rows; `leg_orders` has no opportunity link; exits and settlements are booked by ticker/side only |
| H1 | One SQLite connection shared by all threads with no lock | A commit from one thread can commit another thread's half-written rows |

## 2. Design

### 2.1 Two new tables (additive; nothing existing is altered or dropped)

**`opportunity_ledger`** - one row per evaluated opportunity, keyed by the
Package 1 `opportunity_id`:

| Column group | Columns |
|---|---|
| identity | `opportunity_id` (PK), `detected_at`, `opportunity_class`, `strategy`, `event_id`, `markets`, `direction` |
| economics | `quantity`, `fair_probability`, `market_probability`, `market_price_source`, `touch_ask_cents`, `touch_bid_cents`, `capital_required`, `max_loss`, `max_profit`, `gross_edge`, `fees`, `net_ev`, `ev_complete` |
| context | `settlement_time`, `liquidity_available`, `data_quality`, `source_quality_tier`, `confidence` |
| versions | `model_version`, `code_version`, `config_version`, `fee_schedule_version`, `execution_mode` |
| decision | `decision` (REJECTED / ACCEPTED), `decision_stage`, `rejection_reason`, `final_state` |
| outcome | `outcome` (resolved YES/NO per leg, JSON), `resolved_at`, `realised_pnl`, `hypothetical_pnl`, `hypothetical_basis` |
| full record | `record_json` (Package 1 `to_record()`, incl. the EV breakdown) |

**`opportunity_events`** - the lifecycle, one row per transition:
`opportunity_id, ts, state, detail`, where state is one of
DETECTED, REJECTED, ACCEPTED, SUBMITTED, FILLED, PARTIALLY_FILLED,
NOT_FILLED, EXPIRED (accepted but gone stale before sending), EXITED, SETTLED.

**Links (two new nullable columns):**
`executions.opportunity_uuid` and `leg_orders.opportunity_uuid`. The existing
integer `executions.opportunity_id` is also filled (to the legacy
`opportunities` row) so old reports keep working.

### 2.2 What gets recorded

| Source | Today | After |
|---|---|---|
| Forecast pre-gate skips (19 reasons) | discarded | a ledger row with stage + reason + whatever was known (probability, touch prices, lead) |
| Arbitrage near-misses | top 10 in memory | a ledger row for each relationship that was priced but not crossed (gap in cents), stage MODEL |
| Profitability gate verdict | legacy table | ledger row, stage GATE |
| Risk rejection after the gate | log line only | ledger row updated: REJECTED / RISK / reason |
| Execution | `executions` (unlinked) | events SUBMITTED -> FILLED / PARTIALLY_FILLED / NOT_FILLED; linked by uuid |
| Exit, settlement | ticker/side only | EXITED / SETTLED events; `leg_orders.opportunity_uuid` inherited from the entry legs |

**Skip reasons -> stages** (so errors can be attributed, brief §18):

| Stage | Reasons |
|---|---|
| MODEL | estimator declined; low confidence; edge too small; fees eat the edge; edge gone at size; disagreement too large (points / ratio); event distribution incoherent; sized to zero |
| DATA | degraded data source; data conflict / stale / source unavailable / observation QC (enforced gates) |
| MARKET_SKILL | market forecasts better here |
| POLICY | too far out to have skill (lead gate); price in the tail (5-95c band) |
| LIQUIDITY | no book; no two-sided market; book too thin to exit; spread too wide; book moved while pricing |
| GATE | every ProfitabilityGate reason |
| RISK | breakers, exposure caps, unhedged cap |
| EXECUTION | not filled / partial / failed |

`POLICY` is added to `DecisionStage`: deliberate rules that are neither a
model verdict nor a data or liquidity problem.

**Volume control.** A market re-priced every 2 minutes would otherwise write
~50,000 near-identical rows a day. Skips are de-duplicated per
(ticker, stage, reason) per hour - the same rule `probability_log` already
uses - and near-misses per structure per sweep, keeping the closest 50 plus a
count. Expected volume: a few thousand rows a day, ~5-10 MB/day in the
journal (currently 4.8 MB total); a retention setting prunes `record_json`
of old rejected rows (never of accepted ones).

### 2.3 What would have happened (the counterfactual)

An hourly pass (inside the existing scoring worker) resolves every ledger row
whose settlement time has passed, using the market's own result from Kalshi
(the same reader `settlement.py` uses), and writes:

- **`realised_pnl`** for traded opportunities - from the linked fills,
  exits and settlements (actual money).
- **`hypothetical_pnl`** for rejected and skipped ones - ONE contract (or one
  set) bought at the touch recorded at decision time, taker fee, held to
  settlement. `hypothetical_basis` says exactly that. It is a SIMULATED
  number, labelled as such everywhere, never mixed with realised P&L
  (brief §21). It ignores fills and adverse selection on purpose; Packages
  6-7 replace it with the fill model.

This is what turns "we rejected 400 markets for low confidence" into "the
low-confidence rule avoided $X of losses / missed $Y of gains, ± CI".

### 2.4 Journal hardening (H1) - and one behaviour change for approval

- All journal writes go through one lock, so concurrent workers cannot
  interleave transactions. No behaviour change.
- **Fail closed (needs your approval):** if the ledger cannot record an
  ACCEPTED opportunity, the bot does not trade it (brief §31: journal failure
  → no new positions). This can only ever prevent a trade, never create one.
  It is off-able (`journal.fail_closed: true`).

### 2.5 Reporting

- `scripts/opportunity_report.py`: counts by class / stage / reason;
  accepted vs submitted vs filled; realised P&L of traded opportunities;
  hypothetical P&L of rejected ones by stage, each with sample size and a
  bootstrap confidence interval (clustered by contract, as market_skill does).
- WATCH BOT: one "OPPORTUNITY FLOW (today)" line - detected, rejected by
  stage, accepted, traded, filled.
- The legacy `opportunities` table keeps being written exactly as now (the
  dashboard and monitor read it); the ledger becomes the authoritative
  record. Retiring the legacy table is left to Package 10 (scorecards).

## 3. Files

`kalshibot/journal.py` (tables, lock, ledger writes, outcome pass),
`kalshibot/opportunity.py` (`DecisionStage.POLICY`, `Opportunity.stub()` for
skips that never priced a full trade), `strategies/forecast.py` (skips ->
ledger), `strategies/arbitrage.py` + `arb_engine.py` (near-misses, final
decision), `execution_manager.py` (link executions and entry legs),
`position_manager.py` + `settlement.py` (inherit the link on exit and
settlement), `arb.py` (outcome pass in the scoring worker), new
`scripts/opportunity_report.py`, `scripts/monitor.py` (one line).
**Not touched:** `env_guard.py`, the gate, risk limits, sizing, order
placement, exit rules, `config.yaml` values (one new key: `journal.fail_closed`).

## 4. Tests

Schema migration on a copy of an old journal (additive, idempotent); every
skip reason maps to a stage (a new reason without a mapping fails the test);
de-duplication (one row per ticker/reason/hour); near-miss capping; final
decision after a risk rejection reads REJECTED/RISK; the full chain
opportunity -> execution -> legs -> exit -> settlement shares one uuid;
realised P&L from linked fills equals `pnl_summary` for the same lines;
hypothetical P&L for a rejected YES that settled YES/NO; unresolved markets
left pending; the lock serialises concurrent writers (threaded test); fail
closed blocks the trade and records why; the Package 1 golden regression
still identical; full suite 0 failed.

## 5. Acceptance criteria

1. Full suite passes locally and on the server.
2. Within one hour live: ledger rows for every weather market priced (with
   stage + reason) and for arbitrage relationships evaluated; zero accepted
   opportunities without a ledger row.
3. The next executed trade (when one occurs) is linked end to end:
   ledger -> execution -> legs -> exit/settlement, same uuid.
4. After the first settlements: `realised_pnl` / `hypothetical_pnl` filled,
   and the report runs.
5. Trading decisions unchanged (golden regression; rejection counts by
   reason match the pre-deploy log over the first hours), except that a
   failed ledger write now blocks a trade if fail-closed is approved.
6. Production lockout untouched.

## 6. Risks

- More journal writes (mitigated: de-duplication, lock, measured volume).
- The counterfactual could be over-read: it is labelled SIMULATED, excludes
  fills and adverse selection, and reports sample size and CI with every
  figure.
- Touches the execution and position paths - but only to pass an id through
  and write a row; no pricing, sizing or exit logic changes, and the
  regression test guards the decisions.

## 7. How it moves toward validated expectancy

Every opportunity becomes a row with a fate and, eventually, an outcome.
That is what lets later packages measure expectancy per opportunity class,
per stage and per rule - and tell a strategy with no edge apart from one
whose edge is being lost to execution, liquidity or over-strict risk.

## 8. Decisions for the operator

1. Approve **fail-closed** on ledger write failure (recommended: yes).
2. Counterfactual size: **one contract/set at the recorded touch**
   (recommended; simple, honest, replaced by the fill model later) or the
   size the strategy would have chosen.
3. Retention of `record_json` for old rejected rows: recommended 90 days.
