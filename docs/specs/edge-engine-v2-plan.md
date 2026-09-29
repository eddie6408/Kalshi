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
* Passive market-making was tested next (EXP-008, below) and also killed.

## Outcome 2026-09-29: passive market-making killed (EXP-008 KILL)

`lvt study mm` replayed 323 tennis match-winner markets (195 matches,
97/98 chronological split) with one-lot post-only bids on both sides,
filled by the paper queue model, 0.5 s order latency, cancel when the
touch moves. Net markout to the side mid 60 s after the fill (cents per
contract, maker fee included):

| variant | DEV | TEST | ALL (95% CI) |
|---|---|---|---|
| join the touch (spread >= 2c) | -2.20 | -1.37 | -1.79 [-1.99, -1.58] |
| improve by 1c (spread >= 3c) | -2.39 | -1.65 | -2.03 [-2.26, -1.82] |
| join + 15 s score guard | -2.21 | -1.23 | -1.77 [-2.02, -1.54] |
| improve + score guard | -2.36 | -1.49 | -1.98 [-2.26, -1.72] |

About 91% of fills are pick-offs (a trade through our price or the book
moving through us) at -1.5 to -3c; the 9% ordinary queue fills make +0.3 to
+0.8c. Orders that had rested more than 2.5 s still lose -1.57c, so this is
not an artifact of placing orders on a stale book. Holding to settlement is
also negative (-2.1c). Faster data would not rescue it: the losses are the
sweeps themselves.

Four independent approaches have now failed on this data (price-only
baseline, maker baseline, fair value, market-making). The common cause:
live tennis prices on Kalshi move before a public REST client can see or
react, and what remains is priced within our costs.

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
lvt study gap|decay|calibration|funnel|baseline|scores|errors|latency|model|mm [--start ISO --end ISO --env PAPER_EDGE]
lvt validation
lvt experiment add --name ... --hypothesis ... --disproof ... | list | result --id ... --status ...
lvt ab

## 2026-09-29: would a faster score feed help? (EXP-009, upper bound)

`scripts/lead_study.py`: 1,340 point changes in 69 matches. By the time
Kalshi's live score shows a point, the market has already made ~95% of its
reaction (1.78c of 1.87c; ~60% of it 10 s earlier; on big points 4.5c of
~5c by 5 s earlier). Upper bound with PERFECT knowledge of each point L s
before Kalshi's feed, taker in and out (+30 s), c/contract net:
L=0 -4.64, L=2 -4.51, L=5 -4.42, L=10 -4.09, L=20 -3.29; biggest points
(model impact >=5c) L=20 -1.08. A typical point moves the match price ~2c
and a round trip costs ~5c, so no feed (paid or courtside) makes point-by-
point trading pay. Not pursued.
