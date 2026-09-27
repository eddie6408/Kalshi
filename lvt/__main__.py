"""Command line: python -m lvt <command>

  run                     start the trading loop (mode from TRADING_MODE, default PAPER)
  status                  engine status snapshot
  report [--env PAPER]    performance report for ONE environment
  analyze [--env PAPER]   research answers (section 67)
  readiness               production-readiness checklist
  selftest                run the safety test groups and record readiness evidence
  backtest [--start ISO] [--end ISO]
  oos [--grid strategies.DIP.min_drop_cents=5,6,8 ...]
  label                   label signal outcomes now
  kill on "reason" | kill off
  authorize-live --by NAME | revoke-live
  live-probe [--order-test]   human-run checks with production credentials
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import unittest
from datetime import datetime, timezone

from lvt.config import ROOT, git_commit, load_config
from lvt.db import Database, open_state_db, open_tape_db


def _ts(s: str | None, default: float) -> float:
    if not s:
        return default
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() if "T" in s or "-" in s else float(s)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="lvt")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("status")
    for name in ("report", "analyze"):
        p = sub.add_parser(name)
        p.add_argument("--env", default="PAPER", choices=["PAPER", "SHADOW", "LIVE", "BACKTEST"])
        p.add_argument("--db", default="", help="backtest database (with --env BACKTEST)")
        p.add_argument("--json", action="store_true")
    sub.add_parser("readiness")
    sub.add_parser("selftest")
    p = sub.add_parser("backtest")
    p.add_argument("--start")
    p.add_argument("--end")
    p = sub.add_parser("oos")
    p.add_argument("--grid", nargs="*", default=[])
    sub.add_parser("label")
    p = sub.add_parser("kill")
    p.add_argument("state", choices=["on", "off"])
    p.add_argument("reason", nargs="?", default="manual")
    p = sub.add_parser("authorize-live")
    p.add_argument("--by", required=True)
    sub.add_parser("revoke-live")
    p = sub.add_parser("live-probe")
    p.add_argument("--order-test", action="store_true")
    a = ap.parse_args(argv)

    cfg = load_config()
    from lvt import logsetup
    if a.cmd == "selftest":
        # synthetic test trades must never appear in the production logs
        import logging
        logging.disable(logging.CRITICAL)
    else:
        logsetup.setup(cfg.path(cfg.app.log_dir), console_level="INFO" if a.cmd in ("backtest", "oos") else "WARNING")
    state_path = cfg.path(cfg.app.state_db)

    if a.cmd == "run":
        from lvt.runner import LiveRunner
        return LiveRunner(cfg).run()

    if a.cmd == "status":
        db = open_state_db(state_path)
        for r in db.query("SELECT * FROM status"):
            print(r["k"], time.strftime("%H:%M:%S", time.gmtime(r["ts"])), r["v"])
        return 0

    if a.cmd in ("report", "analyze"):
        from lvt import reports
        db = open_state_db(a.db) if a.db else open_state_db(state_path)
        if a.cmd == "report":
            rep = reports.full_report(db, a.env, cfg.risk.paper_starting_equity)
            print(json.dumps(rep, indent=1, default=str) if a.json else reports.format_report(rep))
        else:
            tape = Database(cfg.path(cfg.app.tape_db), readonly=True) if cfg.path(cfg.app.tape_db).exists() else None
            an = reports.analyze(db, tape, a.env)
            print(json.dumps(an, indent=1, default=str) if a.json else reports.format_analysis(an))
        return 0

    if a.cmd == "readiness":
        from lvt.live_gate import format_report, readiness
        print(f"commit {git_commit(cfg.root)}  config {cfg.config_hash()}  TRADING_MODE={cfg.mode}\n")
        print(format_report(readiness(cfg, open_state_db(state_path))))
        return 0

    if a.cmd == "selftest":
        from lvt.live_gate import SELFTEST_ITEMS
        db = open_state_db(state_path)
        commit = git_commit(cfg.root)
        sys.path.insert(0, str(ROOT))
        rc = 0
        for item, mods in SELFTEST_ITEMS.items():
            suite = unittest.defaultTestLoader.loadTestsFromNames(mods)
            res = unittest.TextTestRunner(verbosity=0, stream=open("/dev/null", "w")).run(suite)
            ok = res.wasSuccessful() and res.testsRun > 0
            rc |= 0 if ok else 1
            db.insert("readiness_evidence", {"item": item, "ts": time.time(), "git_commit": commit,
                                             "config_hash": cfg.config_hash(), "passed": ok,
                                             "details": {"modules": mods, "tests": res.testsRun,
                                                         "failures": len(res.failures) + len(res.errors)}})
            print(f"{'PASS' if ok else 'FAIL'}  {item:<36} {res.testsRun} tests")
        if commit.endswith("-dirty"):
            print("\nNOTE: working tree is dirty - evidence is recorded against a dirty commit and will not "
                  "satisfy the live gate.")
        return rc

    if a.cmd in ("backtest", "oos"):
        from lvt.replay import data_range, run_oos, run_replay
        tape = Database(cfg.path(cfg.app.tape_db), readonly=True)
        db = open_state_db(state_path)
        out = cfg.path(cfg.app.data_dir) / "backtests"
        if a.cmd == "backtest":
            lo, hi = data_range(tape)
            s = run_replay(cfg, tape, _ts(a.start, lo), _ts(a.end, hi + 1), out, "backtest", "all", db)
            print("BACKTEST (replayed live-recorded data, simulated execution)")
            print(json.dumps(s["metrics"], indent=1))
            print("trades database:", s["db_path"])
        else:
            res = run_oos(cfg.root, tape, db, out, a.grid)
            print(json.dumps(res, indent=1, default=str))
        return 0

    if a.cmd == "label":
        from lvt.labeler import label_signals
        n = label_signals(open_state_db(state_path), open_tape_db(cfg.path(cfg.app.tape_db)), cfg, time.time(),
                          limit=100_000)
        print(f"labeled {n} signals")
        return 0

    if a.cmd == "kill":
        f = cfg.path(cfg.kill_switch.file)
        if a.state == "on":
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(f"{a.reason} ({time.strftime('%Y-%m-%d %H:%M:%S')} UTC)\n")
            print(f"kill switch ON ({f}); the running bot stops new orders within one cycle")
        else:
            f.unlink(missing_ok=True)
            print("kill switch file removed" + (" - but KILL_SWITCH is still set in the environment"
                                                if cfg.kill_switch_env else ""))
        return 0

    if a.cmd == "authorize-live":
        from lvt.live_gate import authorize_interactive
        return authorize_interactive(cfg, open_state_db(state_path), a.by)

    if a.cmd == "revoke-live":
        from lvt.live_gate import revoke
        revoke(open_state_db(state_path))
        print("all live authorizations revoked")
        return 0

    if a.cmd == "live-probe":
        return live_probe(cfg, open_state_db(state_path), a.order_test)
    return 2


def live_probe(cfg, db, order_test: bool) -> int:
    """Human-run. Read-only unless --order-test, which places ONE 1-contract
    IOC buy at 1c (cannot fill unless someone sells at 1c) after typed
    confirmation, then records EXECUTION_TESTED evidence."""
    from lvt.execution.kalshi_live import AuthedHttp, CredentialError, KalshiLiveVenue, Signer
    try:
        signer = Signer(cfg.env.get("KALSHI_PROD_KEY_ID", ""), cfg.env.get("KALSHI_PROD_PRIVATE_KEY_PATH", ""))
    except CredentialError as exc:
        print(f"credentials: {exc}")
        return 1
    v = KalshiLiveVenue(AuthedHttp(cfg.kalshi.base_url, signer, cfg.kalshi.request_timeout_s), {})
    print("balance USD:", v.balance_usd())
    print("positions:", v.positions())
    print("resting orders:", len(v.resting_orders()))
    if not order_test:
        return 0
    if not sys.stdin.isatty():
        print("order test must be run by a human at a terminal")
        return 1
    ticker = input("Ticker for a 1-contract IOC BUY YES at 1c (will not fill unless the book is at 1c): ").strip()
    if input('Type "SEND TEST ORDER" to submit it: ').strip() != "SEND TEST ORDER":
        print("not sent")
        return 1
    from lvt.models import OrderRequest, new_id
    req = OrderRequest(new_id("lvt-probe"), ticker, "", "buy", "yes", 1, 1.0, "ENTRY", 1.0, time.time())
    up = v.submit(req, time.time())
    print("result:", up.status, up.exchange_order_id, up.reason, [f.__dict__ for f in up.fills])
    look = v.lookup(req.client_order_id)
    print("lookup by client_order_id:", look.status if look else None)
    ok = up.status in ("NO_FILL", "FILLED", "PARTIAL", "ACKED") and look is not None
    db.insert("readiness_evidence", {"item": "EXECUTION_TESTED", "ts": time.time(), "git_commit": git_commit(cfg.root),
                                     "config_hash": cfg.config_hash(), "passed": ok,
                                     "details": {"status": up.status, "order_id": up.exchange_order_id}})
    print("EXECUTION_TESTED evidence recorded:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
