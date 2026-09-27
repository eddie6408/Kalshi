"""The LIVE gate (sections 12-14, 52, 53, 72).

LIVE requires ALL of:
  1. TRADING_MODE=LIVE in the environment (a human edits .env);
  2. every production-readiness item below passing, computed from this
     bot's own database;
  3. a human authorization recorded with `lvt authorize-live`, bound to
     the exact git commit and config hash now running - any code or
     parameter change voids it;
  4. production credentials (KALSHI_PROD_KEY_ID and
     KALSHI_PROD_PRIVATE_KEY_PATH) that load successfully.

If any of these fails, the process refuses to start (exit code 78, which
systemd is told not to restart). It never falls back to another account,
never guesses credentials, and never drops silently to PAPER while the
operator believes it is LIVE.

Credentials are read only here and only in LIVE mode. PAPER and SHADOW
never load them, even when they are present in .env.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass

from lvt.config import Config, git_commit
from lvt.db import Database

EXIT_LIVE_REFUSED = 78

SELFTEST_ITEMS = {
    # readiness item -> test modules that prove it (run by `lvt selftest`)
    "RISK_CONTROLS_VALIDATED": ["tests.test_risk", "tests.test_kill_switch"],
    "RECONCILIATION_VALIDATED": ["tests.test_reconcile"],
    "DUPLICATE_ORDER_PROTECTION_TESTED": ["tests.test_order_manager"],
    "PARTIAL_FILL_HANDLING_TESTED": ["tests.test_fill_model", "tests.test_positions"],
    "RESTART_RECOVERY_TESTED": ["tests.test_restart"],
}

ITEMS = [
    "PAPER_DATA_SUFFICIENT", "BACKTEST_COMPLETED", "OUT_OF_SAMPLE_TEST_COMPLETED",
    "SHADOW_MODE_COMPLETED", "NET_EXPECTANCY_POSITIVE", "RISK_CONTROLS_VALIDATED",
    "RECONCILIATION_VALIDATED", "EXECUTION_TESTED", "DUPLICATE_ORDER_PROTECTION_TESTED",
    "PARTIAL_FILL_HANDLING_TESTED", "RESTART_RECOVERY_TESTED", "PRODUCTION_CREDENTIALS_PRESENT",
    "HUMAN_AUTHORIZATION",
]


@dataclass
class Item:
    name: str
    passed: bool
    detail: str


def _evidence(db: Database, item: str, commit: str) -> dict | None:
    return db.one("SELECT * FROM readiness_evidence WHERE item=? AND passed=1 AND git_commit=? "
                  "ORDER BY ts DESC LIMIT 1", (item, commit))


def credentials_present(cfg: Config) -> tuple[bool, str]:
    kid = cfg.env.get("KALSHI_PROD_KEY_ID", "")
    path = cfg.env.get("KALSHI_PROD_PRIVATE_KEY_PATH", "")
    if not kid or not path:
        return False, "KALSHI_PROD_KEY_ID / KALSHI_PROD_PRIVATE_KEY_PATH not set"
    from pathlib import Path
    if not Path(path).is_file():
        return False, "private key path does not exist"
    return True, "configured (values not shown)"


def readiness(cfg: Config, db: Database) -> list[Item]:
    rc = cfg.readiness
    commit = git_commit(cfg.root)
    out: list[Item] = []

    paper = db.one("SELECT COUNT(*) n, COUNT(DISTINCT event_ticker) ev, MIN(opened_ts) t0, MAX(closed_ts) t1, "
                   "SUM(realized_pnl_usd) pnl FROM positions WHERE env='PAPER' AND status='CLOSED'") or {}
    days = ((paper.get("t1") or 0) - (paper.get("t0") or 0)) / 86400.0
    ok = (paper.get("n") or 0) >= rc.min_paper_trades and (paper.get("ev") or 0) >= rc.min_paper_events \
        and days >= rc.min_paper_days
    out.append(Item("PAPER_DATA_SUFFICIENT", ok,
                    f"{paper.get('n') or 0}/{rc.min_paper_trades} trades, {paper.get('ev') or 0}/{rc.min_paper_events} "
                    f"events, {days:.1f}/{rc.min_paper_days} days"))

    bt = db.one("SELECT * FROM backtest_runs WHERE kind='backtest' ORDER BY ts DESC LIMIT 1")
    btn = json.loads(bt["metrics"]).get("trades", 0) if bt else 0
    out.append(Item("BACKTEST_COMPLETED", bool(bt) and btn >= rc.min_backtest_trades,
                    f"latest backtest {bt['run_id'] if bt else 'none'}: {btn}/{rc.min_backtest_trades} trades"))

    oos = db.one("SELECT * FROM backtest_runs WHERE kind='oos' AND segment='test' ORDER BY ts DESC LIMIT 1")
    om = json.loads(oos["metrics"]) if oos else {}
    out.append(Item("OUT_OF_SAMPLE_TEST_COMPLETED", bool(oos) and om.get("trades", 0) >= rc.min_oos_trades,
                    f"latest OOS test segment: {om.get('trades', 0)}/{rc.min_oos_trades} trades"))

    sh = db.one("SELECT COUNT(*) n, MIN(opened_ts) t0, MAX(closed_ts) t1 FROM positions "
                "WHERE env='SHADOW' AND status='CLOSED'") or {}
    sdays = ((sh.get("t1") or 0) - (sh.get("t0") or 0)) / 86400.0
    recon_fail = db.scalar("SELECT COUNT(*) FROM alerts WHERE code='RECONCILIATION_FAILURE' AND ts>=?",
                           (sh.get("t0") or time.time(),), 0)
    out.append(Item("SHADOW_MODE_COMPLETED",
                    (sh.get("n") or 0) >= rc.min_shadow_trades and sdays >= rc.min_shadow_days and recon_fail == 0,
                    f"{sh.get('n') or 0}/{rc.min_shadow_trades} shadow trades over {sdays:.1f}/{rc.min_shadow_days} days, "
                    f"{recon_fail} reconciliation failures"))

    exp_paper = (paper.get("pnl") or 0.0) / paper["n"] if paper.get("n") else 0.0
    exp_oos = om.get("expectancy_usd", 0.0)
    ok = (not rc.require_positive_expectancy) or (exp_paper > 0 and exp_oos > 0)
    out.append(Item("NET_EXPECTANCY_POSITIVE", ok,
                    f"paper net expectancy ${exp_paper:.3f}/trade, OOS ${exp_oos:.3f}/trade (both must be > 0)"))

    for item in ("RISK_CONTROLS_VALIDATED", "RECONCILIATION_VALIDATED", "DUPLICATE_ORDER_PROTECTION_TESTED",
                 "PARTIAL_FILL_HANDLING_TESTED", "RESTART_RECOVERY_TESTED", "EXECUTION_TESTED"):
        ev = _evidence(db, item, commit)
        how = "`lvt selftest`" if item != "EXECUTION_TESTED" else "`lvt live-probe --order-test` (human-run)"
        out.append(Item(item, ev is not None,
                        f"evidence at {time.strftime('%Y-%m-%d %H:%M', time.gmtime(ev['ts']))} UTC for {commit}"
                        if ev else f"no evidence for commit {commit}; run {how}"))

    ok, detail = credentials_present(cfg)
    out.append(Item("PRODUCTION_CREDENTIALS_PRESENT", ok, detail))

    auth = db.one("SELECT * FROM live_authorizations WHERE revoked_ts IS NULL ORDER BY ts DESC LIMIT 1")
    ok = bool(auth) and auth["git_commit"] == commit and auth["config_hash"] == cfg.config_hash() \
        and not commit.endswith("-dirty")
    out.append(Item("HUMAN_AUTHORIZATION", ok,
                    (f"authorized by {auth['authorized_by']} for {auth['git_commit']}/{auth['config_hash']}"
                     if auth else "no authorization recorded") +
                    ("" if ok or not auth else f" - running {commit}/{cfg.config_hash()}, re-authorize")))
    return out


def live_allowed(cfg: Config, db: Database) -> tuple[bool, list[Item]]:
    items = readiness(cfg, db)
    return all(i.passed for i in items), items


def format_report(items: list[Item]) -> str:
    lines = []
    for i in items:
        lines.append(f"  [{'PASS' if i.passed else 'FAIL'}] {i.name:<36} {i.detail}")
    verdict = "LIVE MODE AVAILABLE" if all(i.passed for i in items) else "LIVE MODE UNAVAILABLE"
    return "\n".join(lines + ["", verdict])


def build_live_venue(cfg: Config, markets):
    """Only reachable after live_allowed() returned True."""
    from lvt.execution.kalshi_live import AuthedHttp, KalshiLiveVenue, Signer
    signer = Signer(cfg.env.get("KALSHI_PROD_KEY_ID", ""), cfg.env.get("KALSHI_PROD_PRIVATE_KEY_PATH", ""))
    return KalshiLiveVenue(AuthedHttp(cfg.kalshi.base_url, signer, cfg.kalshi.request_timeout_s), markets)


PHRASE = "I AUTHORIZE REAL-MONEY TRADING"


def authorize_interactive(cfg: Config, db: Database, by: str) -> int:
    items = readiness(cfg, db)
    pending = [i for i in items if not i.passed and i.name != "HUMAN_AUTHORIZATION"]
    print(format_report(items))
    if pending:
        print("\nRefusing: readiness items above must pass before authorization.")
        return 1
    if not sys.stdin.isatty():
        print("Refusing: authorization must be typed by a human at a terminal.")
        return 1
    print(f"\nThis authorizes LIVE trading for commit {git_commit(cfg.root)} and config {cfg.config_hash()}.")
    typed = input(f'Type exactly "{PHRASE}": ').strip()
    if typed != PHRASE:
        print("Phrase did not match; nothing recorded.")
        return 1
    db.insert("live_authorizations", {"ts": time.time(), "authorized_by": by, "git_commit": git_commit(cfg.root),
                                      "config_hash": cfg.config_hash(),
                                      "readiness": [i.__dict__ for i in items]})
    print("Authorization recorded. LIVE still requires TRADING_MODE=LIVE in .env and a service restart.")
    return 0


def revoke(db: Database) -> None:
    db.execute("UPDATE live_authorizations SET revoked_ts=? WHERE revoked_ts IS NULL", (time.time(),))
