#!/usr/bin/env python3
"""Watch the bot trade a synthetic market. No account, no key, no risk.

    python scripts/simulate.py                 # 4 simulated days
    python scripts/simulate.py --days 10       # longer run
    python scripts/simulate.py --slow          # pause so you can read it
    python scripts/simulate.py --seed 42       # reproduce an exact run

Everything you see is generated. See kalshibot/simulate.py for why the P/L
here is a demonstration of the machinery and NOT a backtest.
"""
from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.sim_engine import Engine                                    # noqa: E402
from kalshibot.estimators.weather import WeatherTemperatureEstimator   # noqa: E402
from kalshibot.execution import PaperBroker                            # noqa: E402
from kalshibot.fees import FeeModel                                    # noqa: E402
from kalshibot.journal import Journal                                  # noqa: E402
from kalshibot.logging_setup import setup_logging                      # noqa: E402
from kalshibot.risk import HaltReason, RiskManager                     # noqa: E402
from kalshibot.simulate import (CITY, SimulatedExchange,               # noqa: E402
                                SimulatedWeatherData,
                                simulation_weather_config)

B = "\033[1m"; DIM = "\033[2m"; G = "\033[32m"; R = "\033[31m"
Y = "\033[33m"; C = "\033[36m"; X = "\033[0m"


class SimSettings:
    """Stands in for the real Settings object."""
    kalshi_env = "simulation"
    mode = "paper"

    def __init__(self, raw):
        self.raw = raw

    def section(self, name):
        return self.raw.get(name, {}) or {}

    def get(self, dotted, default=None):
        node = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def load_real_config() -> dict:
    import yaml
    root = Path(__file__).resolve().parents[1]
    with open(root / "config.yaml", "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def banner(text: str, colour: str = C) -> None:
    print(f"\n{colour}{B}{'=' * 74}\n  {text}\n{'=' * 74}{X}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=4)
    ap.add_argument("--seed", type=int, default=None,
                    help="reproduce an exact run. Omit for a fresh random one.")
    ap.add_argument("--slow", action="store_true", help="pause between ticks")
    ap.add_argument("--step-minutes", type=int, default=90)
    ap.add_argument("--quiet", action="store_true", help="hide per-market lines")
    ap.add_argument("--db", default="data/simulation.db",
                    help="where to write the journal (use a local disk, not a synced drive)")
    ap.add_argument("--ignore-breakers", action="store_true",
                    help="keep going when a circuit breaker fires (demo only)")
    args = ap.parse_args()

    # Random by default so every run is a different week of weather. The seed
    # is printed so you can replay any run that surprises you.
    if args.seed is None:
        args.seed = random.randrange(1, 1_000_000)
        reproducible = False
    else:
        reproducible = True

    root = Path(__file__).resolve().parents[1]
    setup_logging("WARNING" if args.quiet else "INFO", root / "logs" / "simulation.log")
    logging.getLogger("kalshibot.sim_engine").setLevel(
        logging.WARNING if args.quiet else logging.INFO)

    cfg = load_real_config()
    cfg["execution"] = {"mode": "paper", "paper_fill_model": "cross"}
    settings = SimSettings(cfg)

    db = Path(args.db).expanduser()
    if not db.is_absolute():
        db = root / db
    db.parent.mkdir(parents=True, exist_ok=True)
    db.unlink(missing_ok=True)

    exchange = SimulatedExchange(seed=args.seed, days=args.days)
    fees = FeeModel(cfg.get("fees"))
    estimator = WeatherTemperatureEstimator(
        simulation_weather_config(), data=SimulatedWeatherData(exchange))
    journal = Journal(db)
    engine = Engine(settings, exchange, estimator, PaperBroker(fees),
                    fees, RiskManager(cfg, fees), journal)

    banner("SIMULATION - every price and forecast below is invented", Y)
    print(f"{DIM}  {args.days} simulated days | "
          f"bankroll ${cfg['risk']['bankroll_usd']:,.0f} | "
          f"paper fills at the ask{X}")
    if reproducible:
        print(f"{DIM}  seed {args.seed} (you asked for this exact run){X}")
    else:
        print(f"  {B}seed {args.seed}{X}{DIM}  - a fresh random week. To replay "
              f"this exact run:{X}")
        print(f"{DIM}      python scripts/simulate.py --days {args.days} "
              f"--seed {args.seed}{X}")
    print(f"{DIM}  This shows the machinery working. It is not a backtest "
          f"and not evidence of edge.{X}")

    day_seen = None
    ticks = 0
    try:
        while exchange.day is not None:
            d = exchange.day
            if d.date != day_seen:
                day_seen = d.date
                banner(f"DAY {exchange.day_index + 1}/{args.days}  -  "
                       f"{CITY['label']}  -  {d.date:%a %d %b}")
                print(f"{DIM}  (hidden from the bot: the day's true high will be "
                      f"{d.true_high:.0f}F; the market's view is off by "
                      f"{d.market_bias:+.1f}F){X}\n")

            print(f"{DIM}--- T-minus {d.lead_hours:>5.1f}h to settlement ---{X}")
            try:
                engine.manage_positions()
                engine.scan_once()
            except HaltReason as halt:
                banner(f"CIRCUIT BREAKER TRIPPED: {halt}", R)
                print(f"  {DIM}This is the safety system doing its job. In a real run the "
                      f"bot stops here{X}")
                print(f"  {DIM}and waits for you to look at it, rather than trading on "
                      f"through a bad streak.{X}")
                if not args.ignore_breakers:
                    print(f"\n  {Y}Re-run with --ignore-breakers to watch the full "
                          f"{args.days} days anyway.{X}")
                    break
                print(f"  {Y}--ignore-breakers set: continuing (demo only; the real "
                      f"bot would stop).{X}\n")
                journal.conn.execute(
                    "UPDATE trades SET pnl_usd=0.0 WHERE status='closed' AND pnl_usd<0 "
                    "AND id IN (SELECT id FROM trades ORDER BY id DESC LIMIT 1)")
                journal.conn.commit()

            ticks += 1
            if args.slow:
                time.sleep(1.2)
            if not exchange.advance(args.step_minutes):
                break
            _report_settlements(exchange, engine, journal)
    except KeyboardInterrupt:
        print("\nstopped")

    _flatten_remaining(engine, journal)
    _summary(engine, journal, exchange, ticks, root, args.seed, args.days)
    return 0


def _flatten_remaining(engine, journal) -> None:
    """Close anything still open when the simulation stops, so entries and
    exits reconcile instead of leaving phantom open positions."""
    for ticker, pos in list(engine.paper.positions.items()):
        if not pos.contracts:
            continue
        exit_px = int(round(pos.avg_cost_cents))
        yes_exit = exit_px if pos.contracts > 0 else 100 - exit_px
        no_exit = 100 - yes_exit
        n, pnl = journal.close_open_trades(ticker, yes_exit, no_exit)
        if n:
            print(f"  {DIM}RUN ENDED  flattened {ticker} ({n} entr"
                  f"{'y' if n == 1 else 'ies'}) at cost basis, P/L ${pnl:+.2f}{X}")
        engine.paper.positions.pop(ticker, None)
        engine.paper.trade_ids.pop(ticker, None)


def _report_settlements(exchange, engine, journal) -> None:
    """Resolve anything still held when a simulated day ended."""
    if not exchange.settlements:
        return
    latest = {s["ticker"]: s for s in exchange.settlements}
    for ticker, pos in list(engine.paper.positions.items()):
        s = latest.get(ticker)
        if not s or not pos.contracts:
            continue
        yes_won = s["yes_won"]
        # At settlement a YES contract is worth exactly 100c or 0c, and a NO
        # contract is the mirror image.
        n_closed, pnl = journal.close_open_trades(
            ticker,
            yes_exit_cents=100 if yes_won else 0,
            no_exit_cents=0 if yes_won else 100,
            outcome="yes" if yes_won else "no",
        )
        held = "YES" if pos.contracts > 0 else "NO"
        won = (pos.contracts > 0) == yes_won
        colour = G if pnl >= 0 else R
        bucket = ticker.rsplit("-B", 1)[-1].replace(".", "-")
        print(f"  {colour}SETTLED  actual high {s['true_high']:.0f}F  "
              f"bucket {bucket}F  we held {held}  "
              f"{'WON' if won else 'LOST'}  P/L ${pnl:+.2f}{X}")
        engine.paper.positions.pop(ticker, None)
        engine.paper.trade_ids.pop(ticker, None)
        engine.paper.cash_spent_usd -= abs(pos.contracts) * (1.0 if won else 0.0)


def _summary(engine, journal, exchange, ticks, root, seed=0, days=0) -> None:
    import sqlite3
    conn = journal.conn
    conn.row_factory = sqlite3.Row

    banner("RESULT", C)
    dec = {r["action"]: r["n"] for r in conn.execute(
        "SELECT action, COUNT(*) n FROM decisions GROUP BY action")}
    total = sum(dec.values())
    print(f"  scans run            : {ticks}")
    print(f"  markets evaluated    : {total}")
    print(f"    traded             : {dec.get('traded', 0)}")
    print(f"    rejected by risk   : {dec.get('rejected', 0)}")
    print(f"    edge too small     : {dec.get('skipped', 0)}")

    row = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(pnl_usd),0) pnl, "
        "SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END) wins "
        "FROM trades WHERE status='closed'").fetchone()
    fees = conn.execute(
        "SELECT COALESCE(SUM(fee_usd),0) f FROM trades").fetchone()["f"]

    print(f"\n  positions closed     : {row['n']}")
    if row["n"]:
        colour = G if row["pnl"] >= 0 else R
        print(f"  win rate             : {100.0*row['wins']/row['n']:.0f}%")
        print(f"  fees paid            : ${fees:,.2f}")
        print(f"  {B}net P/L              : {colour}${row['pnl']:+,.2f}{X}")

    print(f"\n{DIM}  Journal written to {journal.path}{X}")
    print(f"{DIM}  Replay this exact run:  python scripts/simulate.py "
          f"--days {days} --seed {seed}{X}")
    print(f"{DIM}  Full detail:  python scripts/report.py --db {journal.path}{X}")
    print(f"{DIM}  Watch it live: python scripts/monitor.py --db {journal.path}{X}\n")
    print(f"{Y}  Reminder: synthetic data. The only thing proven here is that "
          f"the code runs.{X}\n")


if __name__ == "__main__":
    raise SystemExit(main())
