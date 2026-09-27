"""Deterministic historical replay and backtesting (sections 41-43).

The replay feeds recorded snapshots from data/tape.db through the SAME
Engine, risk engine, exit engine and paper fill model the live loop uses,
on a replay clock. There is no look-ahead:
  * snapshots are yielded in time order, each containing only rows recorded
    at or before its timestamp;
  * an order is filled on the first snapshot AFTER its simulated arrival;
  * settlement results and market metadata that only exist later are never
    consulted (positions still open when the data ends are marked out at
    their last bid and flagged END_OF_DATA).

Each run writes its trades to its own database under data/backtests/, never
into the live state database, and records a summary row in
backtest_runs so readiness checks can see it.

Out-of-sample testing splits the recorded period chronologically into
TRAIN / VALIDATION / TEST (60/20/20 by default). A parameter grid is
searched on TRAIN only; the best few candidates are compared on VALIDATION;
exactly one winner is run on TEST, once.
"""
from __future__ import annotations

import itertools
import json
import logging
import time
import uuid
from pathlib import Path

from lvt.alerts import Alerter
from lvt.clock import ReplayClock
from lvt.config import Config, git_commit, load_config
from lvt.db import Database, open_state_db
from lvt.engine.trader import Engine
from lvt.execution.paper import PaperVenue
from lvt.labeler import label_signals
from lvt.models import FillEvent
from lvt.fees import fee_usd
from lvt.reports import closed, metrics
from lvt.tape import iter_snapshots, load_markets

log = logging.getLogger("lvt.replay")

MIN_TRAIN_TRADES = 20


def data_range(tape: Database) -> tuple[float, float]:
    r = tape.one("SELECT MIN(ts) a, MAX(ts) b FROM quotes")
    return (r["a"] or 0.0, r["b"] or 0.0) if r else (0.0, 0.0)


def run_replay(cfg: Config, tape: Database, start: float, end: float, out_dir: Path,
               kind: str = "backtest", segment: str = "all", state_db: Database | None = None,
               params: dict | None = None, label: bool = True) -> dict:
    run_id = f"bt-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / f"{run_id}.db"
    db = open_state_db(db_path)
    markets = load_markets(tape)
    clock = ReplayClock(start)
    venue = PaperVenue(cfg, "BACKTEST", markets)
    engine = Engine(cfg, "BACKTEST", run_id, db, venue, markets, Alerter(None, run_id, 1e9), clock,
                    record_scanner=False)
    t_wall = time.time()
    steps = 0
    last_snaps = {}
    for t, snaps in iter_snapshots(tape, start, end, step_s=cfg.discovery.quote_interval_s,
                                   keepalive_s=cfg.discovery.quote_interval_s * 3):
        clock.advance_to(t)
        engine.on_snapshots(snaps, t)
        last_snaps.update(snaps)
        steps += 1
    # positions still open at the end of the data: mark out at the last bid
    eod = 0
    for p in engine.book.open_positions():
        s = last_snaps.get(p.ticker)
        if p.qty > 0 and s is not None and s.bid(p.side) is not None:
            bid = s.bid(p.side)
            info = markets.get(p.ticker)
            fee = fee_usd(p.qty, bid, info.fee_type if info else "quadratic", info.fee_multiplier if info else 1.0)
            p.exit_reason = "END_OF_DATA"
            p.exit_mid, p.exit_ref_bid = s.mid(p.side) or bid, bid
            engine.book.apply_exit_fill(p, FillEvent("eod", clock.now(), bid, p.qty, fee))
            eod += 1
        elif p.status == "PENDING":
            engine.book.entry_finished(p, clock.now(), "END_OF_DATA")
    if label:
        label_signals(db, tape, cfg, end + 10_000, limit=1_000_000)
    rows = closed(db, "BACKTEST")
    m = metrics(rows, cfg.risk.paper_starting_equity)
    m.update({"end_of_data_closes": eod, "steps": steps, "wall_s": round(time.time() - t_wall, 1),
              "signals": engine.counters["signals"], "buy_signals": engine.counters["buy_signals"],
              "missed_entries": db.scalar("SELECT COUNT(*) FROM v_missed_trades", (), 0)})
    summary = {"run_id": run_id, "kind": kind, "segment": segment, "ts": time.time(), "data_start": start,
               "data_end": end, "params": params or {}, "metrics": m, "git_commit": git_commit(cfg.root),
               "config_hash": cfg.config_hash(),
               "strategy_versions": {s.name: s.version for s in engine.strategies}, "db_path": str(db_path)}
    if state_db is not None:
        state_db.insert("backtest_runs", summary)
    db.close()
    return summary


def parse_grid(specs: list[str]) -> list[dict]:
    """["strategies.DIP.min_drop_cents=5,6,8", ...] -> list of override dicts."""
    axes = []
    for spec in specs:
        path, vals = spec.split("=", 1)
        axes.append([(path, _num(v)) for v in vals.split(",")])
    combos = []
    for combo in itertools.product(*axes) if axes else [()]:
        o: dict = {}
        for path, v in combo:
            d = o
            parts = path.split(".")
            for p in parts[:-1]:
                d = d.setdefault(p, {})
            d[parts[-1]] = v
        combos.append(o)
    return combos


def _num(v: str):
    for f in (int, float):
        try:
            return f(v)
        except ValueError:
            pass
    return {"true": True, "false": False}.get(v.lower(), v)


def run_oos(root: Path, tape: Database, state_db: Database, out_dir: Path, grid: list[str],
            fractions=(0.6, 0.2, 0.2), top_k: int = 3) -> dict:
    a, b = data_range(tape)
    if b <= a:
        raise SystemExit("tape is empty - nothing to test")
    t1 = a + (b - a) * fractions[0]
    t2 = t1 + (b - a) * fractions[1]
    combos = parse_grid(grid)
    results = []
    for o in combos:
        cfg = load_config(root, overrides=o)
        s = run_replay(cfg, tape, a, t1, out_dir, "oos", "train", state_db, o, label=False)
        results.append((o, s))
        log.info("TRAIN %s -> %s", json.dumps(o), s["metrics"].get("expectancy_usd"))
    eligible = [(o, s) for o, s in results if s["metrics"].get("trades", 0) >= MIN_TRAIN_TRADES] or results
    eligible.sort(key=lambda x: -(x[1]["metrics"].get("expectancy_usd") or -1e9))
    val = []
    for o, _ in eligible[:top_k]:
        cfg = load_config(root, overrides=o)
        s = run_replay(cfg, tape, t1, t2, out_dir, "oos", "validation", state_db, o, label=False)
        val.append((o, s))
    val.sort(key=lambda x: -(x[1]["metrics"].get("expectancy_usd") or -1e9))
    best = val[0][0]
    cfg = load_config(root, overrides=best)
    test = run_replay(cfg, tape, t2, b, out_dir, "oos", "test", state_db, best)
    return {"split": {"train": [a, t1], "validation": [t1, t2], "test": [t2, b]},
            "chosen_params": best, "train_candidates": len(combos),
            "validation": [(o, s["metrics"]) for o, s in val], "test": test["metrics"]}
