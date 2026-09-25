"""Push alerts to the operator's phone, for the few things that need a person.

Package 14A phase 4. Delivery is ntfy (https://ntfy.sh): the bot POSTs a
short message to a topic, the ntfy app on the phone shows it. No account, no
credential on the server - the topic name is the only secret, which is why it
lives in .env (NTFY_TOPIC), not in config.yaml or git.

What alerts, and how often:

    data conflict         first enforced rejection per city per day
    required source DOWN  once per source per day
    settlement changed    once per series per day
    circuit breaker       once per day
    worker suspended      once per worker per day
    strategy suspended    once per strategy per day (calibration gate)

Everything is de-duplicated by key, because the bot scans every two minutes
and one broken source would otherwise be hundreds of notifications. Sending
never raises and never blocks for long: an alert that cannot be delivered is
logged, and trading carries on.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger(__name__)

PRIORITIES = {"min", "low", "default", "high", "urgent"}


def today_key(*parts: str) -> str:
    """A de-duplication key that expires at UTC midnight."""
    return ":".join([*parts, datetime.now(timezone.utc).strftime("%Y-%m-%d")])


class Notifier:
    def __init__(self, server: str = "https://ntfy.sh", topic: str = "",
                 enabled: bool = True, timeout: float = 5.0,
                 post: Callable | None = None):
        self.server = server.rstrip("/")
        self.topic = topic.strip()
        self.enabled = bool(enabled)
        self.timeout = timeout
        self._post = post
        self._sent: set[str] = set()
        self._lock = threading.Lock()
        self.delivered = 0
        self.failed = 0

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.topic)

    def configure(self, *, server: str | None = None, topic: str | None = None,
                  enabled: bool | None = None) -> None:
        if server is not None:
            self.server = server.rstrip("/")
        if topic is not None:
            self.topic = topic.strip()
        if enabled is not None:
            self.enabled = bool(enabled)

    def send(self, key: str, title: str, message: str,
             priority: str = "default", tags: str = "") -> bool:
        """Deliver once per `key`. Returns True only if this call delivered."""
        with self._lock:
            if key in self._sent:
                return False
            self._sent.add(key)
        log.warning("ALERT  %s - %s", title, message)
        if not self.active:
            return False
        headers = {"Title": title.encode("ascii", "replace").decode(),
                   "Priority": priority if priority in PRIORITIES else "default"}
        if tags:
            headers["Tags"] = tags
        try:
            post = self._post
            if post is None:
                import requests
                post = requests.post
            resp = post(f"{self.server}/{self.topic}",
                        data=message.encode("utf-8"), headers=headers,
                        timeout=self.timeout)
            status = getattr(resp, "status_code", 200)
            if status >= 400:
                raise RuntimeError(f"HTTP {status}")
            self.delivered += 1
            return True
        except Exception as exc:  # noqa: BLE001
            self.failed += 1
            log.warning("ALERT  could not deliver %r: %s", title, exc)
            return False


#: The process-wide notifier, configured by arb.py at startup.
ALERTS = Notifier(enabled=False)


def configure_from_settings(settings, notifier: Notifier | None = None) -> Notifier:
    n = notifier or ALERTS
    cfg = (settings.section("alerts") if settings is not None else {}) or {}
    topic_env = str(cfg.get("topic_env", "NTFY_TOPIC"))
    n.configure(server=str(cfg.get("ntfy_server", "https://ntfy.sh")),
                topic=os.getenv(topic_env, ""),
                enabled=bool(cfg.get("enabled", True)))
    return n
