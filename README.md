# Kalshi Live Volatility Trader (`kalshi_live_volatility_bot`)

A **standalone** Kalshi bot that trades short-term price *movement* in live,
volatile markets (live tennis first, then other sports and anything else that
moves), not event outcomes. It is a separate application from `kalshi-bot`:

| | this bot | existing bot |
|---|---|---|
| code | `/opt/kalshi/live-volatility` (own git repo) | `/opt/kalshi/trading/kalshi-bot` |
| service | `kalshi-live-volatility.service` | `kalshibot.service` |
| database | `data/lvt.db`, `data/tape.db` | `data/journal.db` |
| env / config | own `.env`, `config/default.toml` | own |
| venv | own `.venv` | own |
| dashboard | `127.0.0.1:8787` | n/a |

Nothing is imported from, written to, or restarted in the existing bot.

**Default mode is PAPER:** live Kalshi production market data (public, no key),
simulated execution. No production key is needed to develop or run it.

## Architecture

```
            Kalshi public REST (production)            Kalshi live_data / MLB StatsAPI
   /series  /markets sweep  /markets?tickers  /orderbook          (sports context)
        │            │               │             │                     │
        └──── PublicHttp (token bucket 6 req/s, cannot sign) ────────────┘
                                   │
        ┌──── discovery thread ────┴──────── main thread (every 2 s) ──────────┐
        │ catalog (daily)                     hot-tier quotes + rotating depth │
        │ universe sweep (hourly, ~120 pages) sports context (budgeted)        │
        │ probe tier: top-400 every 30 s      tape recorder (data/tape.db)     │
        └────────────── Scanner: UNIVERSE → PROBE → HOT (60) ──────────────────┘
                                   │ snapshots
                                   ▼
   Engine  (same code in live loop and replay)
     VolatilityEngine → MarketState classifier → eligibility
     → strategies (DIP, REVERSAL, MOMENTUM, DOWNTREND) → quality score + costs
     → RiskEngine (checks + sizing) → OrderManager (dedupe, fail-closed)
     → Venue: PaperVenue | PaperVenue(shadow) | KalshiLiveVenue (LIVE only)
     → PositionBook → ExitEngine (every cycle) → P&L decomposition
   Labeler (5 min): forward outcome of EVERY signal, taken or not
   Reconciliation (60 s): book vs venue      Health/alerts (60 s)
   Dashboard thread (read-only DB connection)
```

### Strategies (section 46 subtypes)

Two detectors, each run on both sides of the one Kalshi book. Kalshi has no
short sale; downside exposure is **buying NO**, which is exactly what a live
account can do, so paper results for the "sell" subtypes match real mechanics.

| subtype | detector | side | idea |
|---|---|---|---|
| DIP | dip | YES | high → meaningful fast decline → no new low for N s → bounce → buy |
| REVERSAL | dip | NO | YES spikes and turns down (= NO dips and turns up): sell into the increase |
| MOMENTUM | momentum | YES | persistent, accelerating, volume-backed rise; never chase an exhausted move |
| DOWNTREND | momentum | NO | confirmed YES decline: sell into the decline |

Each signal is BUY only if every confirmation passes, including **net edge
after both legs' taker fees, spread and a slippage buffer**. Otherwise it is
recorded as WATCH with the reasons (falling knife, chasing, edge too thin, ...)
so the labeler can later show which refusals were right.

Every trade carries `strategy_version = <label>+<parameter hash>`, so a change
of parameters never pools with old results.

### Exits (re-evaluated every cycle)
EMERGENCY, SETTLEMENT (closing soon, ≥97¢, event final, market not active),
MAX_LOSS (stop), FIXED_PROFIT_TARGET, TRAILING_PROFIT_TARGET, REVERSAL,
MOMENTUM, LIQUIDITY, TIME. Stale data never triggers an exit (fail closed).

### Paper fills
IOC marketable limit orders, filled on the **first snapshot after the
simulated latency** (never the decision snapshot), walking the depth ladder
with a participation haircut, a deterministic touch-miss probability, taker
fees per level, and FULL / PARTIAL / NO_FILL / REJECTED outcomes. Missed
trades are recorded (`v_missed_trades`).

### P&L identity (per closed position)
`gross_mid − spread − slippage = gross; gross − fees = net (realized_pnl_usd)`.

## Directory layout
```
config/default.toml        every threshold (override in config/local.toml)
.env                        TRADING_MODE, KILL_SWITCH, ALERT_WEBHOOK_URL, prod key (LIVE only)
lvt/config.py models.py fees.py db.py tape.py clock.py alerts.py health.py logsetup.py
lvt/data/        http.py (public, unsigned) provider.py kalshi_public.py context.py
lvt/engine/      volatility.py states.py scanner.py strategies.py scoring.py risk.py
                 positions.py exits.py trader.py
lvt/execution/   base.py fill_model.py paper.py encoding.py order_manager.py
                 reconcile.py kalshi_live.py (imported only in LIVE)
lvt/live_gate.py readiness + authorization     lvt/runner.py live loop
lvt/replay.py    replay/backtest/OOS           lvt/labeler.py reports.py dashboard.py
deploy/          systemd unit, install.sh, run_tests.sh
tests/           109 tests (python tests/run_tests.py)
docs/OPERATIONS.md  runbook: startup, shadow, live activation, emergency, reports
```

See **docs/OPERATIONS.md** for procedures, database schema, known limitations
and risks, and the production-readiness checklist.
