# kalshibot

Structural arbitrage on the Kalshi **DEMO** exchange. Demo only - the
application refuses to start against production.

## Two double-clicks. That is the whole thing.

| Double-click | What happens |
|---|---|
| **START BOT.command** | Checks Python, environment, dependencies, runs the 566-test self-test, verifies the production lockout, authenticates against Kalshi demo, shows your balance - then scans continuously until you press Ctrl+C. |
| **WATCH BOT.command** | Live dashboard in a second window. Refreshes every few seconds. Ctrl+C stops watching; the bot keeps running. |

Anything else is optional:

| Double-click | What happens |
|---|---|
| **START BOT (DEMO LIVE).command** | Same as above but actually places orders on the demo exchange. |
| **DIAGNOSTICS.command** | Menu of checks: connection, market audit, liquidity, tests. |

## Folder layout

```
kalshi-bot/
  run_bot.sh            the one-command startup + continuous run
                        (replaces the old setup.sh / go-demo.sh)
  arb.py                entry point (SCAN_ONLY | PAPER | DEMO_LIVE)
  config.yaml           every threshold the bot obeys
  .env                  credentials (never shared, never committed)

  kalshibot/            the library
    env_guard.py        production lockout
    depth.py            order book depth and true executable cost
    strategies/         the three arbitrage detectors
    gate.py             the single profitability gate
    execution_manager.py  multi-leg execution, partial fills, recovery
    ...

  scripts/              things you run while trading
    monitor.py          the live dashboard
    report.py           P/L and calibration
    simulate.py         offline demo, no account needed
                        (drives kalshibot/sim_engine.py, the
                         retired loop - see the retirement log)

  diagnostics/          things you run when something looks wrong
    check_connection.py
    market_audit.py     why is a market not being scanned?
    compare_environments.py
    find_series.py
    diagnose.py
    settle.py

  tests/                356 tests, no network, nothing at risk
    run_tests.sh
```


## Modes

| Mode | Behaviour |
|---|---|
| `SCAN_ONLY` | finds and reports. Never contacts a trading endpoint. Default. |
| `PAPER` | simulates fills against the live demo book. No orders. |
| `DEMO_LIVE` | submits real orders to Kalshi demo. Play money. |

```bash
bash run_bot.sh                 # SCAN_ONLY, continuous
bash run_bot.sh DEMO_LIVE       # places demo orders, continuous
python arb.py --once --verbose  # one scan, full detail
python scripts/monitor.py       # the live dashboard
python tests/run_tests.py       # 356 tests, offline
```

## Production is blocked

Five independent layers, in `kalshibot/env_guard.py`. The order method is
`place_demo_order()`; a generic `place_order()` is an AttributeError. Both the
environment name and the URL are re-checked inside every order call, so
editing config after startup cannot open a path. Lookalike domains are
refused. `tests/test_safety.py` proves all of it, including that the
integration-test loopback hook can never unlock a production host.

## What to expect

Real structural arbitrage is rare. Per-leg fees round UP to the cent, so
`max_contracts_per_trade: 1` will reject almost everything. A scan that
reports **no qualifying arbitrage** is working correctly.

The demo exchange is mostly unquoted - a live audit found roughly 7% of
sampled markets with a real order book. The bot learns which series actually
have books (`data/liquid_series.json`) and spends its order-book budget there
first, so later scans are sharper than the first one.
