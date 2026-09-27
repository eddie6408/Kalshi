"""Command line interface:  klvb <command>

  run             start the bot (mode from config / TRADING_MODE; PAPER by default)
  status          quick status from the database
  report          performance report for ONE environment (PAPER / SHADOW / LIVE)
  backtest        deterministic replay of recorded data
  walkforward     train / validation / out-of-sample evaluation
  fetch-history   build a replay dataset from Kalshi public history (no credentials)
  readiness       production-readiness checklist (LIVE is refused unless all pass)
  selftest        offline risk / restart-recovery validation
  validate-exec   execution validation on Kalshi DEMO (dedicated demo account only)
  kill / unkill   emergency stop switch (file based, picked up within one loop)
  prune           delete raw snapshots older than the retention window
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime

from .config import TradingMode, load_config


def _ts(s: str | None) -> float | None:
    return datetime.fromisoformat(s).timestamp() if s else None


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_run(cfg, args) -> int:
    from .app import Application
    from .execution.factory import LiveModeUnavailable

    if cfg.mode is TradingMode.LIVE:
        print("LIVE mode requested - checking production readiness...", file=sys.stderr)
    try:
        asyncio.run(Application(cfg).run())
    except LiveModeUnavailable as e:
        print(str(e), file=sys.stderr)
        return 2
    return 0


def cmd_status(cfg, args) -> int:
    from .storage.db import Database
    if not cfg.db_path.exists():
        print("no database yet - the bot has not run")
        return 1
    db = Database(cfg.db_path, read_only=True)
    run = db.query("SELECT * FROM runs ORDER BY started_ts DESC LIMIT 1")
    health = db.query("SELECT * FROM system_health ORDER BY ts DESC LIMIT 1")
    pos = db.query("SELECT env, ticker, side, qty, avg_entry, strategy, status FROM positions WHERE status!='CLOSED'")
    alerts = db.query("SELECT ts, level, kind, message FROM alerts ORDER BY ts DESC LIMIT 5")
    _print({"last_run": run[0] if run else None, "health": health[0] if health else None, "open_positions": pos,
            "recent_alerts": alerts, "kill_switch_file": cfg.kill_switch_file.exists()})
    return 0


def cmd_report(cfg, args) -> int:
    from .backtest.metrics import evidence_note, full_report
    from .storage.db import Database
    db = Database(args.db or cfg.db_path, read_only=True)
    since = time.time() - args.days * 86400 if args.days else None
    rep = full_report(db, args.env.upper(), since)
    rep["evidence"] = evidence_note(rep["summary"])
    if args.json:
        _print(rep)
        return 0
    s = rep["summary"]
    print(f"=== {rep['label']} ===")
    print(rep["evidence"])
    if not s.get("total_trades"):
        return 0
    for k in ("total_trades", "win_rate", "avg_win", "avg_loss", "expectancy", "expectancy_cents_per_contract",
              "profit_factor", "gross_pnl", "fees", "slippage", "net_pnl", "max_drawdown", "avg_hold_seconds",
              "trades_per_day", "longest_win_streak", "longest_loss_streak"):
        print(f"  {k:32s} {s.get(k)}")
    print(f"  {'capital_utilization':32s} {rep['capital_utilization']}")
    print(f"  {'entry_fill_rate':32s} {rep['entry_fill_rate']}")
    for group in ("by_strategy_version", "by_sport", "by_regime", "by_exit_reason"):
        print(f"--- {group}")
        for k, v in rep[group].items():
            print(f"  {k:32s} trades={v.get('total_trades')} net=${v.get('net_pnl')} "
                  f"exp=${v.get('expectancy')} win={v.get('win_rate')}")
    return 0


def cmd_backtest(cfg, args) -> int:
    from .backtest.replay import ReplayEngine, cfg_with, load_recorded
    from .backtest.metrics import evidence_note
    from .storage.db import Database
    src = args.db or str(cfg.db_path)
    c = cfg_with(cfg, json.loads(args.overrides) if args.overrides else None)
    metas, events = load_recorded(src, _ts(args.start), _ts(args.end), args.tickers.split(",") if args.tickers else None,
                                  allow_synthetic=args.allow_synthetic)
    out_db = Database(args.out) if args.out else None
    res = asyncio.run(ReplayEngine(c, out_db).run(metas, events))
    rep = {"environment": "BACKTEST (simulated, not real money)", "config_hash": res.config_hash,
           "summary": res.summary, "counters": res.counters, "evidence": evidence_note(res.summary),
           "by_strategy_version": res.report["by_strategy_version"], "by_sport": res.report["by_sport"],
           "by_exit_reason": res.report["by_exit_reason"], "entry_fill_rate": res.report["entry_fill_rate"]}
    _print(rep)
    if args.record and not args.allow_synthetic:
        Database(cfg.db_path).record_validation("BACKTEST", res.summary.get("total_trades", 0) > 0,
                                                {"source": src, **rep})
    return 0


def cmd_walkforward(cfg, args) -> int:
    from .backtest.walkforward import walk_forward
    from .storage.db import Database
    rec = Database(cfg.db_path) if args.record else None
    res = asyncio.run(walk_forward(cfg, args.db or str(cfg.db_path), min_trades=args.min_trades, record_db=rec,
                                   allow_synthetic=args.allow_synthetic))
    _print(res)
    return 0


def cmd_fetch_history(cfg, args) -> int:
    from .backtest.history import fetch_history
    from .data.kalshi_rest import KalshiPublicRestProvider
    from .storage.db import Database

    async def go():
        prov = KalshiPublicRestProvider(cfg)
        try:
            return await fetch_history(prov, Database(args.out), args.series.split(","), args.days,
                                       args.max_markets, args.assumed_depth, args.min_volume)
        finally:
            await prov.stop()
    _print(asyncio.run(go()))
    return 0


def cmd_readiness(cfg, args) -> int:
    from .readiness import evaluate
    from .storage.db import Database
    rep = evaluate(cfg, Database(cfg.db_path))
    print("\n".join(rep.lines()))
    print("\nLIVE MODE " + ("AVAILABLE (still requires TRADING_MODE=LIVE)" if rep.ready else "UNAVAILABLE"))
    return 0 if rep.ready else 1


def cmd_selftest(cfg, args) -> int:
    from .storage.db import Database
    from .validation import run_selftest
    res = run_selftest(cfg, Database(cfg.db_path) if args.record else None)
    _print(res)
    return 0 if res["passed"] else 1


def cmd_validate_exec(cfg, args) -> int:
    from .storage.db import Database
    from .validation import run_demo_execution_validation
    if not args.dedicated_demo_account:
        print("Refusing: pass --dedicated-demo-account to confirm the KALSHI_DEMO_* key belongs to a demo account "
              "used ONLY by this bot (orders on a shared account would disturb the other bot).", file=sys.stderr)
        return 2
    _print(asyncio.run(run_demo_execution_validation(cfg, Database(cfg.db_path))))
    return 0


def cmd_kill(cfg, args) -> int:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.kill_switch_file.write_text(args.reason or "manual kill switch")
    print(f"kill switch file written: {cfg.kill_switch_file} (no new orders; positions keep being managed)")
    return 0


def cmd_unkill(cfg, args) -> int:
    if cfg.kill_switch_file.exists():
        cfg.kill_switch_file.unlink()
    print("kill switch file removed (KILL_SWITCH env var, if set, still applies)")
    return 0


def cmd_prune(cfg, args) -> int:
    from .storage.db import Database
    n = Database(cfg.db_path).prune_snapshots(time.time() - cfg.data.retention_days * 86400)
    print(f"deleted {n} snapshot rows")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="klvb", description="Kalshi Live Volatility Trader")
    ap.add_argument("--config", help="override config file (default config/local.toml if present)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("status")
    p = sub.add_parser("report")
    p.add_argument("--env", default="PAPER")
    p.add_argument("--days", type=float)
    p.add_argument("--db")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("backtest")
    p.add_argument("--db", help="recorded data (default: the bot's own database)")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--tickers")
    p.add_argument("--overrides", help='JSON of dotted config overrides, e.g. {"costs.min_net_edge_cents": 2}')
    p.add_argument("--out", help="write simulated orders/trades to this database")
    p.add_argument("--record", action="store_true", help="record as BACKTEST readiness evidence")
    p.add_argument("--allow-synthetic", action="store_true")
    p = sub.add_parser("walkforward")
    p.add_argument("--db")
    p.add_argument("--min-trades", type=int, default=30)
    p.add_argument("--record", action="store_true")
    p.add_argument("--allow-synthetic", action="store_true")
    p = sub.add_parser("fetch-history")
    p.add_argument("--series", required=True, help="comma separated series tickers, e.g. KXNBAGAME,KXATPMATCH")
    p.add_argument("--days", type=float, default=7)
    p.add_argument("--max-markets", type=int, default=200)
    p.add_argument("--assumed-depth", type=float, default=200)
    p.add_argument("--min-volume", type=float, default=1000)
    p.add_argument("--out", default="data/history.sqlite3")
    sub.add_parser("readiness")
    p = sub.add_parser("selftest")
    p.add_argument("--record", action="store_true")
    p = sub.add_parser("validate-exec")
    p.add_argument("--dedicated-demo-account", action="store_true")
    p = sub.add_parser("kill")
    p.add_argument("--reason")
    sub.add_parser("unkill")
    sub.add_parser("prune")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    handler = {"run": cmd_run, "status": cmd_status, "report": cmd_report, "backtest": cmd_backtest,
               "walkforward": cmd_walkforward, "fetch-history": cmd_fetch_history, "readiness": cmd_readiness,
               "selftest": cmd_selftest, "validate-exec": cmd_validate_exec, "kill": cmd_kill, "unkill": cmd_unkill,
               "prune": cmd_prune}[args.cmd]
    return handler(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
