"""WX-2: the API budget guard.

The system runs on one fixed-price droplet, so "cloud credits" do not apply;
what can run out is third-party quota. Open-Meteo's free tier allows about
10,000 calls a day, and the live strategy already spends part of it. This
counts every research call per provider per UTC day and REFUSES the call that
would cross the budget - stop and log, never exceed, never buy more.

For Open-Meteo the budget covers the whole process: the live strategy's own
calls (read from the source monitor, counted from the first check of the
day) are added to the research calls before deciding.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

DEFAULT_LIMITS = {
    # provider: calls per UTC day for the WHOLE process (open_meteo) or for
    # the research layer (the rest).
    "open_meteo": 8000,        # free tier ~10k/day; 2k headroom
    "twc_climate": 3000,
    "acis": 1500,
    "kalshi_public": 20000,
    "aviationweather": 3000,
}
#: Minimum seconds between two research calls to one provider.
DEFAULT_SPACING = {"open_meteo": 0.25, "twc_climate": 0.15, "acis": 0.3,
                   "kalshi_public": 0.5, "aviationweather": 0.3}


class QuotaExceeded(RuntimeError):
    pass


class QuotaGuard:
    def __init__(self, store, limits: dict | None = None, spacing: dict | None = None,
                 shared_usage=None, clock=time.monotonic, sleep=time.sleep):
        self.store = store
        self.limits = {**DEFAULT_LIMITS, **(limits or {})}
        self.spacing = {**DEFAULT_SPACING, **(spacing or {})}
        #: callable(provider) -> cumulative calls the REST of the process has
        #: made to this provider (source monitor), or None.
        self.shared_usage = shared_usage
        self._baseline: dict[tuple, int] = {}
        self._last: dict[str, float] = {}
        self._warned: set = set()
        self._lock = threading.Lock()
        self.clock, self.sleep = clock, sleep

    @staticmethod
    def today() -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def used(self, provider: str, day: str | None = None) -> int:
        day = day or self.today()
        row = self.store.one("SELECT calls FROM wx_quota WHERE provider=? AND day=?",
                             (provider, day))
        return int(row["calls"]) if row else 0

    def _shared_today(self, provider: str, day: str) -> int:
        if not self.shared_usage:
            return 0
        try:
            total = int(self.shared_usage(provider) or 0)
        except Exception:  # noqa: BLE001
            return 0
        base = self._baseline.setdefault((provider, day), total)
        return max(0, total - base)

    def remaining(self, provider: str) -> int:
        day = self.today()
        return (self.limits.get(provider, 1000) - self.used(provider, day)
                - self._shared_today(provider, day))

    def allow(self, provider: str, n: int = 1) -> bool:
        """Reserve n calls. False (and logged once a day) if that would
        cross the budget - the caller skips the work."""
        with self._lock:
            day = self.today()
            if provider == "open_meteo":
                from ..weatherdata import open_meteo_backing_off
                if open_meteo_backing_off():
                    # The live strategy has been rate-limited: research
                    # spends nothing on Open-Meteo until it recovers.
                    self.store.execute(
                        "INSERT INTO wx_quota (provider, day, calls, refused) VALUES (?,?,0,1) "
                        "ON CONFLICT(provider, day) DO UPDATE SET refused = refused + 1",
                        (provider, day))
                    return False
            if self.remaining(provider) < n:
                self.store.execute(
                    "INSERT INTO wx_quota (provider, day, calls, refused) VALUES (?,?,0,1) "
                    "ON CONFLICT(provider, day) DO UPDATE SET refused = refused + 1",
                    (provider, day))
                if (provider, day) not in self._warned:
                    self._warned.add((provider, day))
                    log.warning("WX QUOTA  %s: daily budget of %d reached - research calls "
                                "stop until 00:00 UTC (live strategy unaffected)",
                                provider, self.limits.get(provider, 1000))
                    self.store.alert("quota", provider, f"daily budget "
                                     f"{self.limits.get(provider, 1000)} reached")
                return False
            self.store.execute(
                "INSERT INTO wx_quota (provider, day, calls) VALUES (?,?,?) "
                "ON CONFLICT(provider, day) DO UPDATE SET calls = calls + ?",
                (provider, day, n, n))
            gap = self.spacing.get(provider, 0.0)
            wait = self._last.get(provider, -1e9) + gap - self.clock()
            if wait > 0:
                self.sleep(wait)
            self._last[provider] = self.clock()
            return True

    def require(self, provider: str, n: int = 1) -> None:
        if not self.allow(provider, n):
            raise QuotaExceeded(provider)

    def summary(self) -> dict:
        day = self.today()
        return {p: {"used": self.used(p, day), "shared": self._shared_today(p, day),
                    "limit": lim} for p, lim in self.limits.items()}


def monitor_usage(monitor):
    """shared_usage for QuotaGuard: the process's own Open-Meteo calls."""
    def usage(provider: str) -> int:
        if provider != "open_meteo":
            return 0
        return sum(int(r.get("calls") or 0) for r in monitor.snapshot()
                   if str(r.get("source_id", "")).startswith("open_meteo"))
    return usage
