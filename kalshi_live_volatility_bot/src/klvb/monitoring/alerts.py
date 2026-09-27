"""Alerting: every alert is logged and stored in the DB; optionally POSTed to a
Slack/Discord-compatible webhook (URL taken from an env var, never from config).
Identical alerts are de-duplicated for `dedupe_seconds`.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx

from ..storage.db import Database
from .logs import redact

log = logging.getLogger("klvb.alerts")

KINDS = {
    "BOT_STARTED", "BOT_STOPPED", "DATA_DISCONNECTED", "EXCHANGE_DISCONNECTED", "STALE_DATA",
    "RECONCILIATION_FAILURE", "UNEXPECTED_POSITION", "DAILY_LOSS_LIMIT", "EXCESSIVE_TRADE_FREQUENCY",
    "API_ERRORS", "DATABASE_FAILURE", "KILL_SWITCH", "LIVE_MODE_ACTIVATED", "PRODUCTION_CREDENTIALS_DETECTED",
    "ABNORMAL_SLIPPAGE", "UNKNOWN_ORDER_STATE", "RESOURCE_PRESSURE", "TRADE_CLOSED",
}


class Alerter:
    def __init__(self, db: Database | None, alerts_cfg, env: dict[str, str] | None = None):
        self.db = db
        self.cfg = alerts_cfg
        env = os.environ if env is None else env
        self.webhook = env.get(alerts_cfg.webhook_url_env, "").strip() or None
        self.last_sent: dict[str, float] = {}
        self.sent = 0

    def alert(self, kind: str, message: str, level: str = "WARNING", details: dict[str, Any] | None = None,
              dedupe_key: str | None = None) -> bool:
        now = time.time()
        key = dedupe_key or f"{kind}:{message}"
        if now - self.last_sent.get(key, 0.0) < self.cfg.dedupe_seconds:
            return False
        self.last_sent[key] = now
        message = redact(message)
        log.log(logging.CRITICAL if level == "CRITICAL" else logging.WARNING if level == "WARNING" else logging.INFO,
                f"ALERT {kind}: {message}", extra={"data": {"alert": kind, **(details or {})}})
        delivered = False
        if self.webhook and level in ("WARNING", "CRITICAL"):
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._post(kind, message, level))
                delivered = True
            except RuntimeError:
                pass
        if self.db is not None:
            try:
                self.db.insert("alerts", {"ts": now, "level": level, "kind": kind, "message": message,
                                          "details": details or {}, "delivered": delivered})
            except Exception:  # noqa: BLE001 - alerting must never crash the bot
                log.exception("could not store alert")
        self.sent += 1
        return True

    async def _post(self, kind: str, message: str, level: str) -> None:
        text = f"[kalshi_live_volatility_bot] {level} {kind}: {message}"
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                await c.post(self.webhook, json={"text": text, "content": text})
        except Exception as e:  # noqa: BLE001
            log.warning("webhook delivery failed: %s", type(e).__name__)
