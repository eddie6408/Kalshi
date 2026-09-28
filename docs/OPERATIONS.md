# Operations runbook — Kalshi Live Volatility Trader

All commands run on the droplet as `eddie` in `/opt/kalshi/live-volatility`
unless marked **root**. `lvt` below means `.venv/bin/python -m lvt`.

## 1. Deploy / update
```bash
ssh droplet-eddie
cd /opt/kalshi/live-volatility
git pull            # or edit + commit here (changes are made on the droplet)
.venv/bin/pip install -r requirements.txt
./deploy/run_tests.sh          # MUST print "N passed, 0 failed" before any restart
sudo systemctl restart kalshi-live-volatility     # root; never touches kalshibot
```
First install (**root**): `bash /opt/kalshi/live-volatility/deploy/install.sh`, then
`systemctl start kalshi-live-volatility`.

Service: `systemctl {start|stop|restart|status} kalshi-live-volatility`
Logs: `journalctl -u kalshi-live-volatility -f` (warnings+), `tail -f logs/lvt.log`,
structured `logs/lvt.jsonl`.

Resource isolation: CPUQuota 60 %, MemoryMax 900 MB, Nice 10, idle-priority IO,
6 req/s total to Kalshi, read-only filesystem except `data/` and `logs/`.

## 2. Environment (`.env`, chmod 600)
| var | default | meaning |
|---|---|---|
| `TRADING_MODE` | `PAPER` | PAPER, SHADOW or LIVE. Only a human edits this. |
| `KILL_SWITCH` | `false` | `true` blocks all new orders |
| `ALERT_WEBHOOK_URL` | empty | optional JSON POST per alert |
| `KALSHI_PROD_KEY_ID` | empty | LIVE only; never logged or displayed |
| `KALSHI_PROD_PRIVATE_KEY_PATH` | empty | LIVE only; keep it under `/opt/kalshi/live-volatility/secrets/` (the unit hides `/home`) |

## 0. !!! REVISIT RISK LIMITS BEFORE SHADOW OR LIVE !!!
Since 2026-09-27 PAPER runs in **research mode** (`risk.enforce_limits_in_paper =
false`), at the user's request, so the calibration dataset is complete:

* waived in PAPER/BACKTEST: daily loss, per-strategy loss, trades per hour,
  concurrent positions, total exposure, event exposure, drawdown size-halving;
  sizing uses the fixed $1,000 starting equity;
* still enforced everywhere: kill switch, fail-closed, spread, liquidity, fresh
  data, one position per market, time to close, per-trade size caps;
* every entry a live limit WOULD have blocked is recorded: `risk_events` rows
  whose detail starts `NOT ENFORCED`, and `signals.explanation.would_block_live`;
* `max_daily_loss_usd` / `max_strategy_daily_loss_usd` are **placeholders
  ($500)** from paper testing.

SHADOW and LIVE always enforce every limit (hard-coded, not configurable).
Before either: re-set every value in `[risk]`, commit, then
`lvt review-limits --by NAME`. LIVE is refused until that review exists for the
exact config (readiness item RISK_LIMITS_REVIEWED); SHADOW starts but raises a
RISK_LIMITS_REVIEW_NEEDED alert until it does.

Useful query - what live limits would have done to the paper sample:
```sql
SELECT check_name, COUNT(*) FROM risk_events
 WHERE env='PAPER' AND detail LIKE 'NOT ENFORCED%' GROUP BY check_name;
```

## 3. PAPER startup
`TRADING_MODE=PAPER` (or unset) → restart. The first ~75 s are the universe sweep;
then the hot tier fills and each market needs `signals.min_history_s` (300 s) of
history before it can be traded. Status: `lvt status`, or the dashboard.

## 4. Dashboard
```bash
ssh -N -L 8787:127.0.0.1:8787 droplet-eddie     # on the Mac, then open http://localhost:8787
```
Or double-click `DASHBOARD LIVE VOL (VPS).command` in ~/trading.
Shows mode/data/execution/key status, PAPER/SHADOW/LIVE performance (never
combined), positions, the hot-tier scanner, signals, trades, alerts, readiness.

## 5. SHADOW startup
**First revisit the `[risk]` limits (section 0) and run `lvt review-limits --by NAME`.**
Set `TRADING_MODE=SHADOW`, restart. Same live data; every order goes through
the LIVE order path (V2 payload built and validated by `encoding.py`, stored in
`orders.payload`, reconciliation every 60 s) against the simulated venue.
Results are stored under `env='SHADOW'`, separate from PAPER.

## 6. Performance reporting
```bash
lvt report --env PAPER            # or SHADOW / LIVE; --json for machine output
lvt analyze --env PAPER           # section-67 research questions
lvt backtest [--start 2026-10-01T00:00 --end ...]      # replay the recorded tape
lvt oos --grid strategies.DIP.min_drop_cents=6,8,10 strategies.DIP.min_bounce_cents=2,3
lvt report --env BACKTEST --db data/backtests/<run>.db
```
A report prints "INSUFFICIENT SAMPLE" below 100 trades and never states that a
strategy is profitable; it states whether net expectancy is more than 2
standard errors from zero in that sample.

### A/B: baseline vs EDGE_ENGINE_V1
```bash
lvt ab                          # BASELINE_V1 (PAPER) vs EDGE_ENGINE_V1 (PAPER_EDGE), 95% CIs
lvt ab --record                 # also store the result in strategy_experiments
lvt report --env PAPER_EDGE     # the challenger's own report
lvt backtest --edge             # replay the challenger on the recorded tape (scores included)
```
The challenger is never promoted automatically; it is only a candidate if
its net expectancy beats the tennis baseline with the 95% interval of the
difference excluding zero, over an adequate sample (brief section 66).

## 7. Emergency shutdown
Fastest (running bot reacts within one 2 s cycle, state preserved):
```bash
lvt kill on "reason"     # creates data/KILL_SWITCH
lvt kill off
```
Or `KILL_SWITCH=true` in `.env` + restart. On activation: new orders stop,
working orders are cancelled (`kill_switch.cancel_open_orders`), positions are
flattened only if `kill_switch.flatten_positions = true`, otherwise the exit
engine keeps managing them. An alert is raised. Nothing is deleted.
Full stop: `sudo systemctl stop kalshi-live-volatility`.

## 8. LIVE activation procedure (human only)
1. PAPER and SHADOW have run long enough for `lvt readiness` data items.
   **Revisit every `[risk]` limit (section 0) - they were loosened for paper testing -
   commit, and run `lvt review-limits --by NAME` (readiness item RISK_LIMITS_REVIEWED).**
2. `lvt backtest` and `lvt oos --grid ...` on the recorded tape.
3. Commit any change; run `lvt selftest` on the clean commit (records evidence
   for risk, reconciliation, duplicate orders, partial fills, restart).
4. Put the production key in `secrets/` (chmod 600) and set the two
   `KALSHI_PROD_*` variables.
5. `lvt live-probe` (read-only: balance, positions, orders), then
   `lvt live-probe --order-test` (one 1-contract IOC buy at 1¢, typed
   confirmation) → records EXECUTION_TESTED.
6. `lvt readiness` must show every item PASS except HUMAN_AUTHORIZATION.
7. `lvt authorize-live --by <name>` and type the phrase. The authorization is
   bound to this commit + config hash; any later change voids it.
8. Set `TRADING_MODE=LIVE`, restart. If anything fails the service exits with
   code 78 and systemd does not restart it. `lvt revoke-live` undoes step 7.

The bot can never enter LIVE by itself: the mode is read only from the
environment, credentials are only loaded behind the gate, and the market-data
client has no way to sign a request.

## 9. Database schema (data/lvt.db)
`runs` · `signals` (every BUY/WATCH, decision TAKEN/REJECTED/WATCH, reasons,
score components, explanation) · `signal_outcomes` (60/300/900 s forward mid
change, MFE, MAE, target-vs-stop first hit) · `orders` (every timestamp: event,
data, signal, created, submitted, ack, arrival, final; payload in SHADOW) ·
`fills` · `positions` (P&L decomposition, MFE/MAE, entry/exit reasons, regime,
version) · `decisions` (EXIT / periodic HOLD / SETTLED) · `risk_events` ·
`eligibility_log` · `market_states` (transitions) · `alerts` · `health` ·
`status` · `scanner_state` · `readiness_evidence` · `live_authorizations` ·
`backtest_runs` · views `v_missed_trades`, `v_rejected_signals`.
`data/tape.db`: `markets`, `quotes` (on change + 6 s keepalive), `probe_quotes`, `trades` (public prints for watched markets),
`depth`, `context`. Tape retention 30 days.

## 10. Alerts
BOT_STARTED/STOPPED, MARKET_DATA_DISCONNECTED/RESTORED, STALE_DATA,
RECONCILIATION_FAILURE, UNEXPECTED_POSITION, DAILY_LOSS_LIMIT,
EXCESSIVE_TRADE_FREQUENCY, API_ERRORS, DATABASE_FAILURE, KILL_SWITCH,
LIVE_MODE_ACTIVATION, PRODUCTION_CREDENTIALS_DETECTED, ABNORMAL_SLIPPAGE,
UNKNOWN_ORDER_STATE, CLOCK_SKEW, RESOURCE, LIVE_GATE_REFUSED. Stored in
`alerts`, logged, optionally POSTed to `ALERT_WEBHOOK_URL`; 15-min dedupe.

## 11. Known limitations
* **Maker fills are modelled, not observed.** Queue position is estimated from
  displayed size and public trade prints; hidden orders, cancellations ahead
  of us and the true matching sequence within a 2 s poll are unknown. Treat
  paper maker results as an estimate until SHADOW/LIVE fills calibrate it.
  `post_only`, `good_till_canceled` and resting-order fill reporting on the V2
  endpoint are unverified against production.
* **REST polling, not a stream.** The production WebSocket needs a key, so
  prices arrive every ~2 s (plus up to ~1 s CloudFront caching). Moves that
  happen and reverse within a poll are invisible, and paper fills resolve on
  the next poll, so paper latency is ~2 s and roughly matches what this bot could
  do live over REST.
* **Taker-only.** Kalshi's taker fee (~1.75¢/contract per side at 50¢) plus the
  spread means a round trip costs ~4-5¢. Only large moves clear it; most
  setups are correctly refused. Maker orders are not implemented.
* The fill model cannot see hidden liquidity, queue position or other bots'
  reactions; the participation haircut and touch-miss probability are
  assumptions to be calibrated against SHADOW/LIVE fills.
* **LIVE execution is untested against production.** The V2
  `/portfolio/events/orders` payload and response parsing follow Kalshi's
  2026-09-16 change as used by the other bot, but `time_in_force`,
  `reduce_only` and response field names must be confirmed by
  `lvt live-probe --order-test` before LIVE.
* Sports context (Kalshi live_data, MLB StatsAPI) is recorded for analysis and
  used only for the event-final exit; ESPN and NBA feeds are blocked from the
  droplet's IP.
* Replay only covers markets that were on the hot tier when recorded.
* Warm-up: after a restart markets need 5 min of history before new entries.
* Position sizing uses paper equity ($1,000 notional) in PAPER/SHADOW.

## 12. Known risks
* Overfitting: parameters are hand-set; any tuning must use `lvt oos`.
* Correlated positions within one match are capped by event exposure only.
* A market can close or settle within a poll interval (SETTLEMENT_EXIT is best
  effort; settlement at 0/100 is handled if it happens).
* Rate limits are shared with anything else calling Kalshi from this IP (the
  other bot mostly calls the demo host).
* Clock skew is checked against HTTP Date headers (±5 s tolerance).

## 13. Production-readiness checklist (enforced by `lvt readiness`)
PAPER_DATA_SUFFICIENT (≥200 trades, ≥20 events, ≥14 days) ·
BACKTEST_COMPLETED (≥200 trades) · OUT_OF_SAMPLE_TEST_COMPLETED (≥50 test-segment
trades) · SHADOW_MODE_COMPLETED (≥50 trades, ≥7 days, 0 reconciliation failures)
· NET_EXPECTANCY_POSITIVE (paper and OOS) · RISK_CONTROLS_VALIDATED ·
RECONCILIATION_VALIDATED · EXECUTION_TESTED · DUPLICATE_ORDER_PROTECTION_TESTED
· PARTIAL_FILL_HANDLING_TESTED · RESTART_RECOVERY_TESTED · RISK_LIMITS_REVIEWED ·
PRODUCTION_CREDENTIALS_PRESENT · HUMAN_AUTHORIZATION.
Any FAIL → LIVE MODE UNAVAILABLE.
