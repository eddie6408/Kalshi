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
| Phase | Scope | Status |
|---|---|---|
| 1 | Event-data collection + timestamp sync | **done 2026-09-28**: live tennis scores every ~4 s (was ~100-200 s); full raw payload kept; match-group companion markets recorded every cycle; event_meta; sport_events timeline (POINT_WON / GAME_WON HOLD-BREAK / SET_WON / STATUS) with provider + receive clocks and quality flags (LAGGED, OUT_OF_ORDER, NO_PROVIDER_TS, GAP, MULTI); exchange-timestamped trade prints since 05:43 |
| 2 | Event-response classification (severity per event type, expected vs actual price response) | needs 1-2 days of dense data; first check the raw payload for a server field and whether provider timestamps exist |
| 3 | Deterministic reference value (tennis: Markov win probability from score state + serve-hold rates implied by the pre-match price) with confidence/range | after phase 2 data; validate offline first |
| 4 | Dislocation / overreaction engine + justified-move filter | offline test: does (reference - market) predict the next minutes' move? |
| 5 | Momentum completion (share of the expected move already done) | with 3-4 |
| 6 | Order flow (signed volume from trade prints) + book imbalance/replenishment | data already recorded (trades, depth) |
| 7 | Unified edge score: strategic edge vs trade quality kept separate | |
| 8 | Enhanced entry/exit (reference reached, edge collapsed, scale-out) | |
| 9 | BASELINE_V1 vs EDGE_ENGINE_V1 A/B in paper | |
| 10 | Analysis with confidence intervals, win/loss attribution engines | |

## Data now recorded (tape.db)
`quotes` (hot + companion markets, 2 s), `depth`, `trades` (exchange prints,
exchange time), `context` (live score incl. raw payload), `sport_events`
(derived timeline), `event_meta` (competitors, best-of, round, match group).

## Open questions for phase 2
* Which raw tennis field names the server (none seen in the 09-27 payload;
  hold/break is currently inferred from service_games_won / breakpoints_won).
* Whether live_data carries a provider timestamp (if not, event time =
  receive time and every event is flagged NO_PROVIDER_TS).
* Mapping competitor1/2 to each market's YES side (event title order,
  competitor ids in event_meta vs market sub-titles).
* The brief's section 28-29 rule change: event data now informs a reference
  value (not a settlement bet); agreed with the user on 2026-09-28.
