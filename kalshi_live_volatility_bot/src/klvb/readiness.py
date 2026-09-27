"""Production-readiness gate. LIVE mode is unavailable unless EVERY check passes.

Evidence comes from this bot's own database (paper/shadow trades, validation
runs recorded by `klvb backtest`, `klvb walkforward`, `klvb selftest` and
`klvb validate-exec`), plus credential presence and an explicit, human-written
authorization in the environment. Nothing here can be satisfied automatically
by the trading loop itself.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from .execution.kalshi_auth import credentials_present
from .storage.db import Database

AUTH_PHRASE = "I AUTHORIZE LIVE TRADING WITH REAL MONEY"
VALIDATION_MAX_AGE_DAYS = 30


@dataclass
class ReadinessReport:
    checks: dict[str, tuple[bool, str]] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return bool(self.checks) and all(ok for ok, _ in self.checks.values())

    def lines(self) -> list[str]:
        return [f"[{'PASS' if ok else 'FAIL'}] {name}: {why}" for name, (ok, why) in self.checks.items()]


def _recent_validation(db: Database, kind: str, now: float) -> tuple[bool, str]:
    v = db.latest_validation(kind)
    if v is None:
        return False, "never run"
    age_days = (now - v["ts"]) / 86400
    if age_days > VALIDATION_MAX_AGE_DAYS:
        return False, f"last run {age_days:.0f} days ago (> {VALIDATION_MAX_AGE_DAYS})"
    return bool(v["passed"]), f"last run {age_days:.1f} days ago: {'passed' if v['passed'] else 'FAILED'}"


def evaluate(cfg, db: Database, env: dict[str, str] | None = None, now: float | None = None) -> ReadinessReport:
    env = dict(os.environ) if env is None else env
    now = now or time.time()
    r = cfg.readiness
    rep = ReadinessReport()

    n_paper = db.scalar("SELECT COUNT(*) FROM trades WHERE env='PAPER'") or 0
    first_paper = db.scalar("SELECT MIN(closed_ts) FROM trades WHERE env='PAPER'")
    paper_days = (now - first_paper) / 86400 if first_paper else 0
    n_events = db.scalar("SELECT COUNT(DISTINCT event_ticker) FROM trades WHERE env='PAPER'") or 0
    ok = n_paper >= r.min_paper_closed_trades and paper_days >= r.min_paper_days and n_events >= r.min_distinct_events
    rep.checks["PAPER DATA SUFFICIENT"] = (ok, f"{n_paper}/{r.min_paper_closed_trades} trades, "
                                               f"{paper_days:.1f}/{r.min_paper_days} days, "
                                               f"{n_events}/{r.min_distinct_events} events")
    rep.checks["BACKTEST COMPLETED"] = _recent_validation(db, "BACKTEST", now)
    oos = db.latest_validation("OUT_OF_SAMPLE")
    if oos is None:
        rep.checks["OUT-OF-SAMPLE TEST COMPLETED"] = (False, "never run")
    else:
        exp = (oos.get("summary") or {}).get("oos_expectancy_cents")
        ok = bool(oos["passed"]) and (not r.require_positive_oos_expectancy or (exp is not None and exp > 0))
        rep.checks["OUT-OF-SAMPLE TEST COMPLETED"] = (ok, f"passed={bool(oos['passed'])}, OOS expectancy {exp}c")

    n_shadow = db.scalar("SELECT COUNT(*) FROM trades WHERE env='SHADOW'") or 0
    first_shadow = db.scalar("SELECT MIN(closed_ts) FROM trades WHERE env='SHADOW'")
    shadow_days = (now - first_shadow) / 86400 if first_shadow else 0
    ok = n_shadow >= r.min_shadow_closed_trades and shadow_days >= r.min_shadow_days
    rep.checks["SHADOW MODE COMPLETED"] = (ok, f"{n_shadow}/{r.min_shadow_closed_trades} trades, "
                                               f"{shadow_days:.1f}/{r.min_shadow_days} days")
    for kind, label in [("RISK_SELFTEST", "RISK CONTROLS VALIDATED"),
                        ("RECONCILIATION_TEST", "RECONCILIATION VALIDATED"),
                        ("EXECUTION_TEST", "EXECUTION TESTED"),
                        ("DUPLICATE_ORDER_TEST", "DUPLICATE ORDER PROTECTION TESTED"),
                        ("PARTIAL_FILL_TEST", "PARTIAL FILL HANDLING TESTED"),
                        ("RESTART_RECOVERY_TEST", "RESTART RECOVERY TESTED")]:
        rep.checks[label] = _recent_validation(db, kind, now)

    creds = credentials_present("prod", env)
    rep.checks["PRODUCTION CREDENTIALS PRESENT"] = (creds, "configured" if creds else
                                                    "KALSHI_PROD_API_KEY_ID / KALSHI_PROD_PRIVATE_KEY_PATH not set")
    who = env.get("LIVE_TRADING_AUTHORIZED_BY", "").strip()
    ack = env.get("LIVE_TRADING_ACKNOWLEDGEMENT", "").strip()
    authorized = bool(who) and ack == AUTH_PHRASE
    rep.checks["HUMAN AUTHORIZATION"] = (authorized, f"authorized by {who}" if authorized else
                                         f"set LIVE_TRADING_AUTHORIZED_BY and LIVE_TRADING_ACKNOWLEDGEMENT=\"{AUTH_PHRASE}\"")
    return rep
