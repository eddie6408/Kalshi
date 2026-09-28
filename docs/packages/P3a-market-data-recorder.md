# Package 3a — Market-data recorder, production prices beside forecasts, run versions

**Status: APPROVED 2026-09-28 and IMPLEMENTED** (with C2 phase M0, which
shares the production watch). Operator decisions: (1) approved as scoped;
(2) record both benchmarks, the gate stays on demo prices until ~2 weeks of
production-benchmarked settlements exist; (3) tick retention 90 days.
Written 2026-09-27 against commit `a0929e9` (Packages 1-2 live).
Scope: the existing weather + arbitrage bot only. The reactive-strategy plan
(`docs/audit/2026-09-27-reactive-strategy-audit.md`) is **on hold**; nothing
here is sports or multi-strategy.
Audit findings G4 (no tape → no replay), G5 (benchmark is the demo market),
G8 (versions only on opportunities, not runs).

Production remains locked. Production is read only through `LiveBookReader`:
GET, unauthenticated, path-allowlisted in `env_guard`. No key, no orders.

---

## 1. The questions it answers

1. **Is the model behind the market, or behind the *demo* market?** The
   market-skill gate that blocks weather entries compares the model with demo
   prices. Checked today: the same weather tickers exist on production with
   real depth and a live trade tape (e.g. `KXHIGHMIA-26SEP27-B85.5`: 50-lot
   prints this morning, a 1-22¢ NO ladder). The market that decides whether
   this strategy can ever make money is production, and it is not recorded.
2. **What did the book look like when we decided?** Books and deltas are
   consumed and thrown away, so no decision can be replayed and no fill model
   can be fitted (Packages 3b, 6, 7 need this history, and it only
   accumulates forward).
3. **Which code, config and models produced a run?** `runs` stores mode and
   a note only.

## 2. Design

### 2.1 Production prices beside every forecast (G5)
- In the weather pass, for each market the strategy prices, one public
  `orderbook` read of the same ticker on production (`LiveBookReader`).
  36-72 markets per 2-minute pass → at most ~0.6 requests/s.
- `probability_log` gains nullable `prod_yes_ask_cents`, `prod_yes_bid_cents`,
  `prod_read_at`; the ledger's `touch_json` gains `prod_yes_ask/bid`.
- `market_skill` computes **both** benchmarks (model vs demo mid, model vs
  production mid), side by side in the report and dashboard.
- **The gate's behaviour does not change** in this package: it keeps
  deciding on demo prices until you approve switching (decision 2) — the
  production benchmark needs its own ~2 weeks of settled contracts first.

### 2.2 Tape recorder (G4)
`kalshibot/recorder.py` writes append-only, gzip JSONL files
`data/ticks/YYYY-MM-DD/{source}.jsonl.gz`, outside SQLite so the journal
stays small. Each record carries our receive time, the exchange time where
given, source, ticker, and the payload replay needs:

| Source | What | How often |
|---|---|---|
| `demo_ws` | every snapshot/delta the WS feed already applies | as received |
| `prod_book` | full production book of each weather market priced | each weather pass (the §2.1 read — no extra requests) |
| `prod_trades` | production trade prints for the weather series, via the public `markets/trades` endpoint, cursor-paged from the last seen trade | every 5 min |
| `demo_book` | the demo book used for each decision (REST or cache) | each pass |

- Writes happen on a background queue; a slow disk or a write error never
  blocks a trading decision (recorder health goes to the source monitor
  and one alert, not to the fail-closed path).
- Budget and back-off: a shared token bucket for production reads (default
  2 req/s, far under published limits); on 429 it backs off and skips, never
  retries in a loop.
- Size: books and trades are small (~10-30 MB/day compressed). The demo
  feed is not: it carries ~80 messages/s (every market the feed follows),
  estimated ~60-150 MB/day compressed - measured on day 1 and reported by
  `tape_report.py`; `recorder.demo_ws: false` keeps only books and trades.
  69 GB free.
  Retention `recorder.retention_days: 90`; recording stops (trading does not)
  if free space falls under 5 GB.
- `scripts/tape_report.py`: per series and day — updates, trades, volume,
  spread distribution, demo-vs-production price gap. This also answers how
  far demo prices drift from the real market.

### 2.3 Run versions (G8)
`runs` gains `code_version`, `config_version`, `model_versions`,
`fee_schedule_version` at start-up (Package 1's `versioning.py`), so every
tick file and ledger row can be tied to the exact build that produced it.

## 3. Files
New: `kalshibot/recorder.py`, `scripts/tape_report.py`.
Changed: `strategies/forecast.py` (hand the production read to the log and
recorder), `journal.py` (additive columns), `market_skill.py` (second
benchmark, report only), `wsfeed.py` (tap to recorder), `arb.py` (trade-tape
worker; run versions), `scripts/dashboard.py` + `monitor.py` (production vs
demo line), `config.yaml` (new `recorder:` section).
**Not touched:** `env_guard.py`, the gate thresholds, risk, sizing, order
placement, exits, trading mode, credentials.

## 4. Tests
Recorder round trip and ordering; queue never blocks the caller (slow-disk
mock); disk guard stops recording only; token bucket and 429 back-off
(mock); trade-tape cursor resumes without duplicates or gaps; additive
migration on a copy of the live journal, idempotent; market-skill report
with both benchmarks on synthetic data, and **gate verdicts identical** to
before; `LiveBookReader` still refuses any non-allowlisted path; Package 1
golden regression unchanged; full suite `0 failed` **on the server** before
merge (the deploy gate), concurrency tests stress-run there.

## 5. Acceptance
1. Within an hour live: tick files growing for all four sources; production
   reads within budget, no 429 storms.
2. New `probability_log` rows carry production prices (target ≥ 95%).
3. `tape_report.py` runs; dashboard shows demo vs production.
4. Trading decisions unchanged (golden regression; skip counts by reason
   match the pre-deploy hours).

## 6. Decisions for the operator
1. Approve the package as scoped (weather + arbitrage only, read-only
   production data). Recommended: yes.
2. Market-skill benchmark: **record both, keep the gate on demo** until
   ~2 weeks of production-benchmarked settlements exist, then decide from
   the numbers (recommended) — or switch the gate to production now.
3. Tick retention: 90 days (recommended; matches the ledger).

## 7. As built (2026-09-28)

- `kalshibot/recorder.py` (tick files), `kalshibot/prodwatch.py` (production
  books + prints for the markets the forecast strategy priced; worker
  `production_watch`, 15 s tick, books every 120 s, prints every 300 s,
  <= 2 req/s, back-off on 429), `LiveBookReader.trades()` (GET
  `/markets/trades`, already inside the public allowlist - env_guard
  unchanged).
- `probability_log.prod_yes_ask_cents / prod_yes_bid_cents / prod_read_at`;
  ledger stub touches carry `prod_yes_ask/bid`; accepted opportunities carry
  `evidence.prod_touch`.
- `market_skill.fetch_rows(..., benchmark="production")` and
  `compare_benchmarks()`; dashboard "vs real exchange" columns. The gate
  (`MarketSkillGate`) is unchanged and decides on demo prices.
- `runs.code_version / config_version / model_versions / fee_schedule_version`.
- `scripts/tape_report.py`; heartbeat lines `RECORDER` and `PRODWATCH`.
- Tests: `tests/test_market_data_recorder.py`.
