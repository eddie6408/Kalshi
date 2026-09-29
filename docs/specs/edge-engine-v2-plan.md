# EDGE ENGINE V2 - execution path (user brief, 2026-09-28)

The user's "Master refinement prompt - Edge Engine V2" is the execution path
from 2026-09-28. Principle: move EDGE_ENGINE_V1 from *logically plausible* to
*empirically validated* - or reject it. No new trading logic until the
fair-value gap is shown to predict movement (section 81) and to be executable
(section 82).

## Agreed caveats (raised when adopting the brief)
1. **Resolution limit.** Quotes are polled every ~2 s and scores every ~4 s,
   and the score feed has no provider timestamp. Edge decay at 1 s is not
   measurable; the latency chain starts at "score received". Sub-second work
   needs Kalshi's WebSocket, which requires an API key - the user's call.
2. **SHADOW (section 65)** means observe-only signals that touch no account.
   The existing SHADOW *mode* (live order path against a simulated venue) is
   part of the LIVE gate, so observe-only runs as a per-strategy flag instead
   of redefining that mode.
3. **Sport adapters (section 25)** beyond tennis are deferred until tennis
   shows an edge - building models for sports without evidence contradicts
   sections 60 and 84.
4. **Tennis model V2 (section 26):** no free player-rating or surface feed
   exists; serve strength is already implied by the market. Extra inputs
   are added only if they improve out-of-sample calibration.
5. **Sample size.** One position per match and tennis only means tens of
   trades per day; promotion gates will take weeks, which is intended.

## Order of work
| Task | Brief | Status |
|---|---|---|
| Current state audit | 79 | done 2026-09-28 (see below) |
| Data gaps: settled outcomes, dense dislocation DB, experiment registry, study tooling | 5, 8, 9, 58 | done (this commit) |
| Baseline report on identical data | 80 | `lvt study baseline` - run once >=1-2 days of dense data |
| Prove the fair-value gap | 81 | `lvt study gap` - same |
| Prove executability / edge decay | 82, 10 | `lvt study decay`, fills in paper |
| Where the edge exists | 83 | after 81/82 |
| Calibration | 8, 27 | `lvt study calibration` (needs settled outcomes) |
| Opportunity funnel | 39 | `lvt study funnel` |
| Edge-score calibration, false positives/negatives, latency chain | 11, 22, 37, 38 | `lvt study scores|errors|latency` (done) |
| Degradation monitor + stop-trading conditions + incident snapshots | 67, 74 | done: OK / DEGRADED / SUSPENDED (dashboard "Health") |
| Observe-only mode | 65 | done: `[edge] observe_only = true` records SHADOW signals, no account change |
| Fault/timing/execution/strategy/risk tests | 61 | done: tests/test_v2_robustness.py |
| Strategy validation report | 45, 47, 76, 77 | `lvt validation` (done): ten criteria PASS/FAIL/INSUFFICIENT |
| V2 logic (information vs flow probability, regime engine, ...) | 15-24, 30-36 | only where 81/82 justify it |

EDGE_ENGINE_V1 is FROZEN while EXP-003 (the A/B) runs: changing it would
contaminate the experiment. V2 changes go into a new version.

## Outcome 2026-09-29: fair-value approach retired (EXP-006 KILL)

Kill rule registered before the study ran (see `lvt experiment list`):
no variant reaches disagreement-set Brier <= market Brier on the DEV half,
or the dev-chosen variant fails on TEST -> retire.

`lvt study model` rebuilt P(player 1 wins) from tape for 72 settled matches
(7,598 observations, chronological 36/36 split; player mapping checked
against the payload's winner field: 55 OK, 0 flipped). Model minus market
Brier where they disagree by >=3c:

| variant | DEV | TEST | ALL (95% CI, match bootstrap) |
|---|---|---|---|
| V1 anchor [t-600, t-60] (live) | +0.0093 | +0.0129 | +0.0110 [0.003, 0.020] |
| anchor [t-300, t-10] | +0.0068 | +0.0059 | +0.0064 [0.002, 0.011] |
| anchor [t-1800, t-60] | +0.0144 | +0.0194 | +0.0167 [0.000, 0.034] |
| in-match serve stats (market-free) | +0.0610 | +0.0194 | +0.0393 [-0.006, 0.081] |
| fixed early-match anchor | +0.0197 | +0.0244 | +0.0219 [-0.002, 0.046] |

Every variant is worse than the market on both halves. The market does
drift toward the model afterwards (about +0.5-1c at 30-120 s, the same effect
as the gap study), but by far less than the gap, and outcomes side with the
market: the model is directionally informative for a few seconds and
overconfident in size. Consequences, as pre-registered:

* `[edge] observe_only = true`: EDGE_ENGINE_V1 records SHADOW signals and
  dislocations, places no paper orders. BASELINE_V1 trading was stopped the
  same day. Both arms of EXP-003 are closed; the bot is a data recorder.
* No new fair-value trading logic. EXP-005 (built on the same reference)
  is withdrawn.
* Still open: EXP-007 longshot bias (passive; 55 of 100 matches needed;
  observed 0.236 vs price 0.176, breakeven 0.200, CI [0.144, 0.363]).
* The only idea left on the table is passive market-making in thin tennis
  markets (spread capture, no fair-value claim). It needs its own
  hypothesis and kill rule and has not been started.

## Hypotheses queued for V2 (not implemented)
* (WITHDRAWN 2026-09-29, see above) EXP-005 pool order flow across mirror markets: YES on player A and NO on
  player B are the same risk; V1 reads flow per market, so selling in one
  market does not block the mirror trade (found by the section 61 conflicting-
  flow test).

## Audit summary (2026-09-28 12:35 UTC)
* Live: PAPER (BASELINE_V1) + PAPER_EDGE (EDGE_ENGINE_V1) on the same data;
  172 tests green; no fail-closed flags; 5 req/s, no 429s.
* Baseline since maker switch (05:41): 416 trades, -$746.79, 17% win;
  tennis 374 trades -$619.88; maker entry fill rate 46%.
* Challenger: first 10 min, 181 evaluations -> 44 justified repricing,
  116 NO_EDGE, 16 reversal WATCH, 1 BUY (risk-rejected); 0 trades yet.
* Data: quotes since 09-27 13:54; trade prints since 09-28 05:43; dense
  tennis scores only since 09-28 11:54 (15 matches). Missing: settled
  outcomes (0), dense fair-value history (134 throttled rows).

## Commands
lvt study gap|decay|calibration|funnel|baseline|scores|errors|latency|model [--start ISO --end ISO --env PAPER_EDGE]
lvt validation
lvt experiment add --name ... --hypothesis ... --disproof ... | list | result --id ... --status ...
lvt ab
