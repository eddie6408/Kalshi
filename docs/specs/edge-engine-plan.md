# Edge engine plan (brief received 2026-09-28)

Source: "Master strategy enhancement prompt - market-movement edge engine",
supplied by the user on 2026-09-28. This file tracks it against the code.

## Principle
Trade the difference between the market's movement and the movement the
available information justifies - only when that gap is large, confirmed,
liquid and executable enough to leave positive expected NET value.
`NO_EDGE` is a first-class result. The existing strategy stays as the
control group **BASELINE_V1**; the new logic is **EDGE_ENGINE_V1**, evaluated
on the same live data without duplicate trades (A/B, sections 42-44).
If the enhanced version does not beat the baseline on net expectancy after
realistic costs, it is not deployed (section 66).

## Why (evidence, 2026-09-27/28 paper data)
* 414 taker trades: -5.4c/contract net; the price move after entry was -1c
  before costs; round trip ~4.8c. Dip, reversal, momentum and downtrend all
  lost on price movement alone -> price-only patterns carry no information.
* 274 maker trades: costs fell to ~1.4c/contract but the post-entry move
  worsened to -1.9c (adverse selection): -3.4c/contract net.
* 14 replay variants of the price-only strategy all lost (best -$1.20/trade).
The missing input is *why* the price moved: repricing on real information
vs. overreaction to order flow. That needs the live score.

## Phases (section 65) and status
| Phase | Scope | Status (2026-09-28) |
|---|---|---|
| 1 | Event-data collection + timestamp sync | done (60819bb) |
| 2 | Event-response classification | done: `event_responses` per score change and market (kinds, BREAK/HOLD, BREAK_POINT/SET_POINT/MATCH_POINT tags, severity from the model's expected impact), labelled later with the actual 10/30/60/180 s move -> OVERREACTED / PROPORTIONATE / UNDERREACTED / CONTRARY / NOISE |
| 3 | Deterministic reference value | done: `lvt/edge/tennis_model.py` (exact point->game->tiebreak->set->match Markov model) + `reference.py` (serve strengths implied by the market over the last 10 min excluding the last 60 s; range from the IQR of those; confidence from range, samples, score freshness, mapping confirmation) |
| 4 | Dislocation / overreaction + justified-move filter | done: DISLOCATION_REVERSAL / FLOW_REVERSAL vs EVENT_REPRICING, reaction measured from the last poll that still showed the old score |
| 5 | Momentum completion | done: UNFINISHED_MOMENTUM only while < 70% of the justified move is done |
| 6 | Order flow + book confirmation | done: signed taker flow (10 s / 120 s), shock vs sustained, book imbalance / replenishment |
| 7 | Unified edge score | done: STRATEGIC EDGE and TRADE QUALITY scored separately, both must pass; fill probability from the engine's own maker fill rate |
| 8 | Enhanced entry/exit | done: sequential confirmation, MIN_REQUIRED_MOVE, maker entry, ranking by net edge x quality x confidence x fill probability, one position per match, entry decay, REFERENCE_VALUE_REACHED / EDGE_COLLAPSED / THESIS_INVALIDATED exits, scale-out take-profit |
| 9 | BASELINE_V1 vs EDGE_ENGINE_V1 A/B | running in paper: env PAPER (baseline, unchanged) vs PAPER_EDGE (challenger, own account), same live data; `lvt ab` |
| 10 | Analysis | `lvt ab`, `lvt report --env PAPER_EDGE`, trade_diagnostics, counterfactuals; needs data |

Tennis only. Every other sport / market type has no reference and is
therefore NO_EDGE in the challenger (the baseline keeps trading it).

## Result that motivated the A/B (2026-09-28 replay grid)
All 14 price-only variants lost on the training evening AND the validation
night (best validation: bigger moves -$0.91/trade, 678 trades).

## Data now recorded (tape.db)
`quotes` (hot + companion markets, 2 s), `depth`, `trades` (exchange prints,
exchange time), `context` (live score incl. raw payload), `sport_events`
(derived timeline), `event_meta` (competitors, best-of, round, match group).

## Resolved 2026-09-28 (from live payloads)
* `server` = the serving competitor's id (compare with competitor1_id /
  competitor2_id); points are 0/15/30/40 with `advantage` = the id holding it;
  tiebreak points are plain integers at 6-6 games.
* live_data carries no provider timestamp: event time = receive time
  (~4 s resolution), every event flagged NO_PROVIDER_TS; market reaction
  timing comes from exchange-timestamped trade prints.
* competitor1 = first name in the match title = first half of the event
  code; market-side mapping must agree by code and by name, and is then
  confirmed (or rejected) by how prices react to scored points.
* The brief's section 28-29 rule change: event data now informs a reference
  value (not a settlement bet); agreed with the user on 2026-09-28.

## Known limitations of EDGE_ENGINE_V1
* Score polling (~4 s) is slower than Kalshi's repricing: the engine cannot
  front-run events and does not try to; it judges moves after the fact.
* Serve strengths are anchored to the market, so a persistent market error
  lasting > 10 min is absorbed into the reference, not traded.
* Independent-points model: no momentum/fatigue/injury effects; confidence
  and range express the uncertainty of the anchor, not of the model form.
* Set-winner markets are priced only for the current set; game-spread,
  totals and exact-score markets are not modelled (NO reference).
