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
| V2 logic (information vs flow probability, regime engine, edge-score calibration, ...) | 15-24, 30-36 | only where 81/82 justify it |

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
lvt study gap|decay|calibration|funnel|baseline [--start ISO --end ISO --env PAPER_EDGE]
lvt experiment add --name ... --hypothesis ... --disproof ... | list | result --id ... --status ...
lvt ab
