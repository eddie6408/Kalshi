"""Alerting (section 58). Every alert goes to the log and the alerts table;
optionally also to ALERT_WEBHOOK_URL (JSON POST - works with ntfy, Slack
and Discord style webhooks). Repeats of the same code are suppressed for
`dedupe_window_s`."""
from __future__ import annotations

import json
import logging
import threading
import time

import requests

from lvt.db import Database

log = logging.getLogger("lvt.alerts")

CODES = {
    "BOT_STARTED", "BOT_STOPPED", "MARKET_DATA_DISCONNECTED", "MARKET_DATA_RESTORED",
    "EXCHANGE_DISCONNECTED", "STALE_DATA", "RECONCILIATION_FAILURE", "UNEXPECTED_POSITION",
    "DAILY_LOSS_LIMIT", "EXCESSIVE_TRADE_FREQUENCY", "API_ERRORS", "DATABASE_FAILURE",
    "KILL_SWITCH", "LIVE_MODE_ACTIVATION", "PRODUCTION_CREDENTIALS_DETECTED",
    "ABNORMAL_SLIPPAGE", "UNKNOWN_ORDER_STATE", "CLOCK_SKEW", "RESOURCE", "LIVE_GATE_REFUSED",
}


class Alerter:
    def __init__(self, db: Database | None, run_id: str, dedupe_window_s: float = 900,
                 webhook_url: str = "", clock=time.time):
        self.db = db
        self.run_id = run_id
        self.window = dedupe_window_s
        self.webhook = webhook_url
        self.clock = clock
        self.last: dict[str, float] = {}

    def fire(self, code: str, message: str, level: str = "WARNING", details: dict | None = None,
             key: str = "") -> bool:
        now = self.clock()
        k = f"{code}:{key}"
        if now - self.last.get(k, -1e18) < self.window:
            return False
        self.last[k] = now
        getattr(log, level.lower(), log.warning)("ALERT %s %s", code, message)
        if self.db is not None:
            try:
                self.db.insert("alerts", {"ts": now, "run_id": self.run_id, "level": level, "code": code,
                                          "message": message[:1000], "details": details or {}})
            except Exception as exc:                  # noqa: BLE001
                log.error("could not persist alert: %s", exc)
        if self.webhook:
            body = {"source": "kalshi-live-volatility", "level": level, "code": code,
                    "message": message, "details": details or {}, "ts": now}
            threading.Thread(target=self._post, args=(body,), daemon=True).start()
        return True

    def _post(self, body: dict) -> None:
        try:
            requests.post(self.webhook, data=json.dumps(body, default=str),
                          headers={"Content-Type": "application/json"}, timeout=5)
        except Exception as exc:                      # noqa: BLE001
            log.warning("alert webhook failed: %s", exc)
