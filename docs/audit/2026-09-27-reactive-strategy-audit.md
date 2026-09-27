# Audit and plan — parallel LIVE_VOLATILITY_REACTIVE strategy

**Date:** 2026-09-27 · **Against:** commit `2c33cfb` (Packages 1-2 live) ·
**Brief:** "Master development prompt — parallel Kalshi trading system with
live-volatility reactive strategy" (sections 1-72). **Read-only; no code
changed.** Builds on `docs/audit/2026-09-27-architecture-audit.md`.

Production remains locked. Nothing proposed here enables live trading,
touches credentials or weakens `env_guard.py`; the brief forbids it (§71)
and the platform's own rules forbid it.

---

## 1. Findings that decide the design (measured today)

### 1.1 The reactive strategy's market exists — on production only
Sampled 12:00 UTC Sunday, last hour of public trade prints per series:

| Series | Production | Demo |
|---|---|---|
| KXATPMATCH (live tennis) | 500+ trades on one match, **26¢ range**, ~89k contracts, 1¢ spread | **0 trades** |
| KXMLBGAME | 16/16 markets traded, up to 91 trades, 1¢ spread | 0 |
| KXNFLGAME / KXWNBAGAME | active pre-game, 1¢ spreads, ~1¢ range | 0 |
| KXNCAAFGAME, KXEPLGAME | near-silent | 0 |

**The demo exchange has no live-sports activity at all.** A strategy that
trades price movement cannot be observed, tuned or paper-traded on demo:
there is no movement and nothing to fill against. It must run PAPER against
**production** market data.

### 1.2 Production real-time data needs a production key
The production WebSocket returns **HTTP 401** without credentials. Using a
production key is prohibited. Production data is therefore available only
through **public REST** (books, markets, trade tape) — polling, read-only,
through the existing credential-free, allowlisted `LiveBookReader`.
Consequence: resolution of ~1-2 s per watched market, a bounded watchlist
(rate limits), and measured - not assumed - latency.

### 1.3 Kalshi's position model
- One **net position per market** per account. There is no separate short
  book: downside exposure is **buying NO** (the V2 order endpoint already
  maps buy NO ↔ sell YES on the single YES book, `client.to_yes_book`).
  "Sell the increase / sell the decline" are therefore implementable as
  buy-NO entries and sell-NO exits - real, not simulated shorts.
- Because the exchange nets per market, **two strategies in the same market
  cannot be told apart on the exchange** (YES from one and NO from the other
  cancel). Attribution must come from our own ledger, and exits by one
  strategy can consume the other's contracts. → Initial rule proposed: one
  strategy per market at a time, enforced by central risk.

### 1.4 What exists vs what the brief needs

| Brief requirement | Exists | Gap |
|---|---|---|
| Strategy #1 (§9) | Weather forecast (`ForecastStrategy` + `weather_temperature` estimator, ensemble + nowcast) **and** structural arbitrage (3 detectors) - so "existing" is two strategies | No common interface object; both are called directly by `arb_engine` |
| Standard interface (§30) | Both emit `Opportunity` (Package 1) into one gate + risk (`_judge`) + `ExecutionManager` | No `Strategy.evaluate()` contract, no strategy registry |
| Central risk (§25-27, §61) | `ProfitabilityGate` + `_risk_ok` (exposure caps, breakers) | No per-strategy budgets, no strategy-level daily loss, no same-market conflict rule |
| Central execution (§37) | `ExecutionManager` (taker, partial fills, recover, reconcile) | Fine for entries; no resting-order management (C2) |
| Exits (§21) | `PositionWorker` - forecast-oriented (stop, thesis, closing soon), structural hold | No profit-target / trailing / momentum / time exits; not strategy-pluggable |
| Market data (§29) | `MarketState` + demo WS feed; `LiveBookReader` for production books | No production tick stream, no volatility metrics, no history |
| Paper mode (§40) | `TradingMode.PAPER` logs one line per accepted opportunity | **No paper fills, positions, exits or P&L** |
| Per-strategy mode / kill switches (§42-43) | Mode is process-wide; strategies on/off only via config/flags | No per-strategy mode, no runtime kill switches |
| Journal / attribution (§24, §51) | Packages 1-2: every opportunity with stage, reason, versions, linked to execution → exit → settlement | Needs sub-strategy id and paper positions |
| Reconciliation before trading (§36, §66) | Reconcile after each trade; settlement from journal | No mandatory start-up reconciliation gate |
| Replay / backtest (§45-47) | None | Needs recorded ticks first |
| Dashboard (§49-50) | Monitor, dashboard (money, sources, market skill) | No per-strategy P&L, no scanner view |
| Event context (§32) | None | No authoritative free real-time sports feed identified; defer |

## 2. Recommended sequence (merged with the research-platform packages)

The two briefs converge: both need recorded market data, replay, a fill
model and per-strategy attribution first. Order proposed:

| Package | Content | Trades? |
|---|---|---|
| **R1 = P3a+** | Market-data recorder: demo WS events (existing feed), **production public-REST polling of a bounded watchlist** (sports + weather), production trade tape; volatility metrics computed from it; versions per run. Data only. | No |
| **R2** | Strategy framework: `Strategy` interface, registry, **per-strategy mode** (OFF / PAPER / SHADOW / DEMO_LIVE), per-strategy **kill switches** + global, **risk budgets** per strategy, same-market conflict rule, start-up reconciliation gate. Existing strategies wrapped with **no behaviour change** (golden regression). | No change |
| **R3** | Paper execution engine: simulated fills against the recorded production book (walk + latency + fees), paper positions, paper exits, paper P&L - attributed in the ledger. | Paper only |
| **R4** | LIVE_VOLATILITY_REACTIVE in PAPER on production data: volatility engine, regime classifier, scanner, movement score, DIP / MOMENTUM / REVERSAL / DOWNTREND (via NO), anti-chase and anti-knife rules, exit engine, latency tracking. | Paper only |
| **R5 = P3b** | Deterministic replay + backtest over R1's history (no look-ahead; train / validation / out-of-sample). | No |
| **R6** | Shadow mode, scorecards per strategy / sub-strategy / portfolio, regime analysis, correlation between strategies. | No |
| — | Live: never automatic; only after the production-readiness gate and explicit operator authorization (brief §42, §63 phase 19). | — |

Why R1 first: every later step - tuning thresholds, replay, backtest, paper
fills - needs tick history, and history only accumulates forward. The live
tennis sample above is exactly the data being lost every day it is not
recorded.

## 3. PACKAGE R1 — market-data recorder (proposal; not implemented)

**Goal.** Record the price history both strategies and all research need,
starting now, without trading and without touching production credentials.

**Design.**
- `kalshibot/recorder.py`: an append-only tick store in compressed daily
  files (`data/ticks/YYYY-MM-DD/*.jsonl.gz`), outside SQLite so the journal
  stays small.
- **Demo feed**: every WS snapshot/delta the bot already receives is
  written as it is applied (source=demo).
- **Production watchlist poller** (new worker, public REST only via
  `LiveBookReader`'s allowlisted GET):
  - candidates from open sports series (KXATPMATCH, KXMLBGAME, KXNFLGAME,
    KXWNBAGAME, ... configurable) plus the weather markets the forecast
    strategy prices;
  - a cheap sweep ranks markets by recent trade activity; the top N
    (default 20) are polled for book + trades every `poll_seconds`
    (default 2 s), the rest every few minutes;
  - a request budget well under Kalshi's published limits, with back-off on
    429 and health on the source monitor (`kalshi_live_market_data`).
- Each record: exchange timestamp where given, **our receive timestamp**,
  source, ticker, best bid/ask, depth levels, last trade(s), sequence where
  available - the fields replay needs to enforce "known at the time".
- `kalshibot/volatility.py`: pure functions over a tick window - price
  change, velocity, acceleration, realised volatility, spread, depth
  imbalance, recent high/low, update frequency. Computed and logged for the
  watchlist; nothing acts on them yet.
- Retention and size: measured in the first days; default keep 60 days of
  ticks (configurable), with a disk-usage check that stops recording, not
  trading, if space runs low.
- Report: `scripts/tape_report.py` - per market: updates/min, trades/min,
  price range, spread distribution, realised volatility; so the question
  "which markets have executable volatility?" (§54) is answered from data.

**Not touched:** strategies, gate, risk, execution, `env_guard`, trading
mode. Production access stays read-only, unauthenticated, allowlisted.

**Tests.** Record/read round trip; ordering and timestamps; poller respects
budget and back-off (mock); watchlist ranking; volatility maths on synthetic
series (known velocity, acceleration, volatility); disk-guard; the
allowlist still refuses any non-market-data path; full suite 0 failed on
the server before merge (the deploy gate).

**Acceptance.** Within an hour live: tick files growing for demo feed and
production watchlist; poller within budget with no 429 storms; tape report
shows per-market activity; no change in trading decisions.

## 4. Decisions for the operator

1. **Reactive strategy runs in PAPER against production public data**
   (recommended: demo has no sports activity). The existing strategies keep
   running as they do now on demo.
2. **Sequence R1 → R6 above**, folding the research-platform packages in
   (recommended), rather than building the reactive strategy first.
3. **One strategy per market at a time** until internal sub-ledgers exist
   (recommended; the exchange nets positions per market).
4. **Watchlist budget**: top 20 markets polled every 2 s (recommended
   start; tuned from measured rate-limit headroom).
