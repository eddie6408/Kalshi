# Package 14A — Authoritative sources & data provenance

**Status: STAGED — not implemented.** Do not start until the operator gives the
signal ("execute 14A" or similar). Analysis done 2026-09-25 against commit
`81714de` on the droplet (`/opt/kalshi/trading/kalshi-bot`).

Source spec: section 14A "Authoritative information sources & data provenance"
(pasted by the operator 2026-09-25). This file is the implementation plan for
it, scoped to what this bot actually does today.

---

## 1. Scope decision

The bot trades exactly one external-data strategy: **weather_temperature**
(daily highs, 6 cities). Everything else it reads is Kalshi's own exchange data.

So 14A is implemented as:

- **Built now:** the general machinery (source registry, provenance records,
  source health, conflict gate, no-trade-on-bad-data) wired to the weather and
  Kalshi sources that exist.
- **Framework-ready, not built:** economic/Fed, crypto, sports, political and
  news sources. No strategy consumes them, so building clients for them now is
  code nothing runs. The registry schema accepts them; each gets built with the
  strategy that needs it, and must enter through the registry.
- **Rule recorded, not built:** the Claude estimator (`estimators/llm.py`) may
  not be wired to any web/news input until that input goes through this
  package's provenance + validation path. "The internet said..." is never an
  input.

---

## 2. Audit — every place the bot reads the internet

| Data type | Current source | Tier | Authoritative source | Backup | API / structured? | Latency | Freshness control today | Action |
|---|---|---|---|---|---|---|---|---|
| Kalshi markets, books, orders, fills, balance (demo) | `external-api.demo.kalshi.co` REST (`client.py`) | 1 | Same (the exchange) | none needed | Yes, JSON REST | ~0.1–0.4 s/call; full sweep ~134 s | Market-state cache, 20 s book age | Keep. Add to health monitor. |
| Kalshi order-book stream (demo) | `external-api-ws.demo.kalshi.co` WS (`wsfeed.py`) — **built, disabled** | 1 | Same | REST (automatic) | Yes, WS JSON | push, ms | Books marked untrusted on gap/drop | Enabled by the separate feed package. Add to health monitor. |
| Kalshi production books (read-only probe) | `external-api.kalshi.com` public GET (`livebooks.py`) | 1 | Same | none | Yes | ~0.3 s | every 900 s | Keep. Add to health monitor. |
| Temperature forecast (primary) | Open-Meteo ensemble API: GFS, ICON, ECMWF IFS members (`weatherdata.py`) | **2** — reputable redistributor of NOAA/DWD/ECMWF model output | NOAA GEFS (NOMADS), ECMWF open data, DWD ICON-EPS | NWS gridpoint | Yes, JSON | ~1 s | In-process cache 1800 s keyed on **fetch** time; **model run time not recorded** | Keep as primary (raw GRIB is heavy, and Open-Meteo is structured and stable). Record model names, model-run/issue time where exposed, retrieval time. Promote NWS gridpoint to **cross-check** (see gate G2). Future option: NWS National Blend of Models as a Tier-1 probabilistic source. |
| Temperature forecast (fallback) | NWS `api.weather.gov` gridpoint `maxTemperature` | 1 | Same | none | Yes, JSON | ~1 s | `updateTime` returned but **not recorded** | Keep. Record `updateTime`. Already barred from trading by `require_primary_source: true`. Becomes the secondary for the conflict gate. |
| Observed high so far today (live floor) | NWS `stations/{id}/observations` (`weatherdata.observed_extreme_today`) | 1 (government) — **not the settlement source** | Settlement = **The Weather Company** (Kalshi series `settlement_sources`, confirmed via API 2026-09-25) | none | Yes, JSON | ~1 s | **None.** No observation-age check, no QC flag check, **no spike filter** (the scoring path has one; the live path does not) | Fix: add spike filter + NWS `qualityControl` check + max observation age. A single bad sensor reading today can falsely "rule out" outcomes → false certainty. Highest-risk finding. |
| Observed daily max (scoring / calibration) | NWS `stations/{id}/observations` (`forecast_scoring.py`) | 1 | NWS Daily Climate Report (CLI) is the official NWS daily max; settlement is TWC | NCEI GHCN-Daily (lags days) | Yes (CLI is a text product via `api.weather.gov/products`) | ~1 s | 6 h settle delay, spike filter | Record source + retrieval time. Evaluate CLI as the scoring truth (closer to what TWC reports for `CLINYC`-style stations). |
| Settlement source per series | Hard-coded comment in `config.yaml` | — | Kalshi series API `settlement_sources` | — | Yes | — | none | Read at runtime from Kalshi, store per series, alert if it changes. |
| TWC settlement values | **not read** | 1 for these markets | weather.com/kalshi | — | Unknown — check for a structured feed/licensing; **do not scrape** | — | — | Investigate only. If no licensed structured source, keep NWS as the documented stand-in with the existing `observation_margin_f`. |

Nothing in the bot currently reads Tier-4 sources (blogs, social, search
results). Good starting position.

---

## 3. Gaps against 14A

1. **No provenance record.** `opportunities.evidence` stores `source`,
   `degraded_source`, ensemble stats and `observed_so_far_f`, but not: endpoint,
   data timestamp (model run / obs time), retrieval time, data age at decision.
   `forecasts` table has no source column at all. `decisions` table is empty
   (unused).
2. **No source health.** Failures are log lines only; nothing counts
   consecutive failures, error rate, latency, or freshness per source; nothing
   blocks trading on an unhealthy required source except the ensemble fallback
   rule.
3. **No redundancy check.** NWS gridpoint is only fetched when the ensemble
   fails; the two are never compared. A bad ensemble pull with a plausible shape
   trades unchallenged.
4. **Live observation not quality-controlled** (see audit table).
5. **Cache freshness is fetch-based**, not data-based: a 30-min-old fetch of a
   6-hour-old model run looks "fresh".
6. **Settlement source is a comment**, not data.
7. **NWS etiquette:** User-Agent lacks a contact address, which NWS asks for.

---

## 4. Implementation plan (phased; each phase is its own commit)

Principle: **measure before enforcing.** Phases 1–3 are logging/observability
only; trading behaviour changes only in Phase 4, with thresholds set from data
collected in Phases 1–3.

### Phase 1 — Source registry (no behaviour change)
- `config.yaml`: new `sources:` section. Per source: `id`, `tier`, `publisher`,
  `endpoint`, `role` (primary / secondary / fallback / settlement / observation),
  `max_data_age_seconds`, `required_by` (strategies), `fallback_to`.
- `kalshibot/sources.py`: loads + validates the registry; refuses Tier-4 as a
  trading input; exposes the fallback chain PRIMARY → AUTHORISED SECONDARY →
  AUTHORISED FALLBACK → NO DATA → NO TRADE.
- `weatherdata.py`, `livebooks.py`, `client.py`, `forecast_scoring.py` read
  endpoints from the registry instead of module constants (values unchanged).
- NWS User-Agent gains a contact address (operator to supply).
- Tests: registry validation, tier rules, fallback chain.

### Phase 2 — Provenance capture (no behaviour change)
- New journal table `source_observations`: `id, source_id, publisher, endpoint,
  data_type, value_json, data_timestamp, retrieved_at, data_vintage
  (model run / NWS updateTime / obs time), market, opportunity_id`.
- Every external fetch that feeds a forecast writes one row; opportunity
  `evidence` gains `source_observation_ids` + `data_age_seconds`.
- Forecast cache stores data timestamp + retrieval timestamp; staleness judged
  on **data** age.
- NWS gridpoint fetched alongside the ensemble every time (shadow) and stored,
  so Phase 4's conflict threshold can be fitted from real disagreement data.
- Read `settlement_sources` per series from Kalshi at startup; store; log
  WARNING if it differs from the last stored value.
- Journal migration is additive (new table + new JSON keys); old rows untouched.
- Tests: provenance row written per fetch; evidence links resolve; migration is
  idempotent on a copy of the live journal.

### Phase 3 — Source health (no behaviour change)
- Per source: last success, last failure, HTTP status, latency, error rate
  (rolling), consecutive failures, last valid data timestamp → state
  `HEALTHY` / `DEGRADED` / `DOWN`.
- `MARKET STATE`-style heartbeat line `SOURCES  NWS HEALTHY | OpenMeteo HEALTHY
  | Kalshi REST HEALTHY | Kalshi WS …`.
- Dashboard panel (Prometheus) + monitor section: source table; click-through
  from an opportunity to the `source_observations` rows behind it.
- Tests: state transitions, thresholds.

### Phase 4 — Gates (BEHAVIOUR CHANGE; thresholds from Phase 2–3 data)
- **G1 Required source unhealthy → block.** Strategy whose `required_by` source
  is DOWN/DEGRADED rejects with `SOURCE_UNAVAILABLE`. No substitution with an
  unlisted source.
- **G2 Data conflict → reject.** Ensemble mean vs NWS gridpoint differ by more
  than X °F (X fitted from Phase 2 shadow data, e.g. the 95th percentile of
  normal disagreement) → `DATA_CONFLICT`, recorded with both values, timestamps,
  difference. Never auto-trades.
- **G3 Stale data → reject.** Data age at decision > `max_data_age_seconds` →
  `STALE_SOURCE`.
- **G4 Live observation QC.** Spike filter (reuse `forecast_scoring` logic),
  NWS `qualityControl` flag honoured, observation older than N minutes not used
  as a floor.
- All four go through the existing risk gate as rejection reasons, so they show
  in the monitor's rejection counts and the dashboard.
- Tests per gate, including "primary down, no fallback → no trade".

### Phase 5 — Source calibration (after data threshold)
- Extend existing `station_bias` / `calibration_gate` to report error, MAE, bias
  **per source × station × lead-time bucket** (season once there is >1 season).
- Correction still governed by the existing rule in `station_bias.py`: 20+
  distinct target dates per station **and** walk-forward acceptance. Earliest
  ~2026-10-06/08 (stations currently at 7–9 days).

### Deferred (built only with the strategy that needs it)
- Economic/Fed (BLS, BEA, Treasury, FRED/ALFRED for vintages, GDPNow, NY Fed
  Nowcast, Cleveland Fed), crypto exchanges, sports, elections.
  Vintage control for economic data = ALFRED real-time periods; must be designed
  in from day one of any econ strategy.

---

## 5. Deployment procedure (on the droplet, when signalled)

1. `git switch -c package/14a` in `/opt/kalshi/trading/kalshi-bot`.
2. Implement phase by phase; after each: `python tests/run_tests.py` must end
   `0 failed`; commit.
3. Phases 1–3: merge to `main`, `sudo systemctl restart kalshibot`, confirm
   `self-test … 0 failed`, `SOURCES` heartbeat line, no new errors in
   `journalctl -u kalshibot`. Let it collect ≥24 h of shadow data.
4. Phase 4: operator reviews the fitted thresholds before merge. Deploy the same
   way. Watch rejection counts for a day.
5. Rollback at any point: `git revert <commit>` + restart. Journal changes are
   additive, so rollback never needs a DB restore (backup taken before the
   Phase 2 migration anyway: `sqlite3 journal.db ".backup …"`).

## 6. Sequencing with other staged work

- **WebSocket feed** (roadmap item 3) is mid-baseline; do it first or
  independently. 14A Phase 3 adds Kalshi WS to the health table either way.
- **False "unhedged" flag fix** (`execution_manager.py`, single-leg partial
  fills) is independent; small.

## 7. Open questions for the operator (answer at execution time)

- Contact e-mail for the NWS User-Agent.
- G2 conflict threshold: accept the data-fitted value, or set one by hand?
- Should `DATA_CONFLICT` only reject, or also alert (e.g. push notification)?
