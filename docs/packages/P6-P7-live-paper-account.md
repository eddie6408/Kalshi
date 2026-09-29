# Packages 6/7 (first cut) — a fake-money account on production prices

**Status: IMPLEMENTED 2026-09-29** (operator: "complete those tasks while we
are waiting"). Runs alongside demo trading, which is unchanged.

## Why
Measured 2026-09-28, last 24 h: production weather markets traded
1,000-5,400 times per city at 1c spreads; demo 6-32 times at spreads up to
94c. NFL totals: 108 production trades, 0 on demo. Demo cannot say whether
the strategy makes money against a real market, and the demo arbitrage
profit (+$50, 27-28 Sep) came from demo's broken books.

## What it does (kalshibot/paper_live.py)
- **Weather**: on each production book read (Package 3a watch), a second
  `ForecastStrategy` - same estimator, same config, no journal, no
  market-skill gate - prices the market on the PRODUCTION book, then the
  engine's ProfitabilityGate. Every live rule applies unchanged (edge,
  confidence allowance, price band, relative disagreement, spread, depth,
  Kelly and caps). The market-skill gate's verdict is RECORDED
  (`gate_blocked`), not obeyed (`include_gate_blocked: true`).
- **Arbitrage** (`ProdArbCheck`): every event the demo sweep inspects is
  queued; its markets' production books are read (1 req/s, own worker) and
  scanned by the same structural scanner and gate. Each check is a row in
  `prod_arb_checks`; one the gate accepts is paper-traded.
- **Fills**: taker, walking the production book as read (no queue, no
  latency: an upper bound for taker execution). **Exits**: held to
  settlement. **Money**: $500 fake, <= 30 open, <= $250 exposure, one
  position per market.
- Settled hourly (scoring worker). `paper_positions`, `prod_arb_checks`;
  `scripts/paper_report.py`; WATCH BOT line "LIVE PAPER"; heartbeat lines
  `PAPER LIVE` and `PROD ARB`.

## Not in this cut
Exits before settlement, latency, queue position for taker fills, other
estimators than the first forecast strategy.

No order can be sent: production is reached only through LiveBookReader
(GET, unauthenticated, allowlisted); the module has no execution client.
