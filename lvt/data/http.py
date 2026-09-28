"""Unauthenticated HTTP client for public market data.

This class cannot sign requests: it has no key, no signer and no code path
that adds Kalshi auth headers. Receiving market data therefore never implies
permission to trade (section 11). Order submission lives in
lvt/execution/kalshi_live.py and is only constructed in LIVE mode.

A token bucket enforces one request budget across every public call so
this bot cannot starve the other bot on the droplet or trip Kalshi's limits.
"""
from __future__ import annotations

import email.utils
import logging
import threading
import time
from dataclasses import dataclass, field

import requests

log = logging.getLogger("lvt.http")

USER_AGENT = "kalshi-live-volatility/0.1 (+paper research bot)"


class HttpError(Exception):
    def __init__(self, status: int, message: str, url: str):
        super().__init__(f"HTTP {status} {url}: {message[:200]}")
        self.status = status
        self.url = url


class TokenBucket:
    def __init__(self, rate: float, burst: float, clock=time.monotonic, sleep=time.sleep):
        self.rate = rate
        self.burst = burst
        self.tokens = burst
        self.clock = clock
        self.sleep = sleep
        self.last = clock()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                t = self.clock()
                self.tokens = min(self.burst, self.tokens + (t - self.last) * self.rate)
                self.last = t
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait = (1.0 - self.tokens) / self.rate
            self.sleep(wait)


@dataclass
class HttpStats:
    requests: int = 0
    errors: int = 0
    http_429: int = 0
    last_ok_ts: float = 0.0
    last_error: str = ""
    clock_skew_s: float = 0.0
    rtt_ms_ewma: float = 0.0
    window: list = field(default_factory=list)   # request timestamps (last 60 s)

    def req_per_s(self) -> float:
        t = time.time()
        self.window = [x for x in self.window if t - x < 60]
        return len(self.window) / 60.0


class PublicHttp:
    def __init__(self, base_url: str, rate: float, burst: float, timeout: float,
                 backoff_on_429_s: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.bucket = TokenBucket(rate, burst)
        self.backoff_on_429_s = backoff_on_429_s
        self.stats = HttpStats()
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json", "User-Agent": USER_AGENT})
        self._blocked_until = 0.0

    def get(self, path: str, params: dict | None = None, absolute: bool = False,
            retries: int = 2) -> dict:
        url = path if absolute else self.base_url + path
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            pause = self._blocked_until - time.time()
            if pause > 0:
                time.sleep(pause)
            self.bucket.acquire()
            t0 = time.time()
            self.stats.requests += 1
            self.stats.window.append(t0)
            try:
                resp = self._session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                self.stats.errors += 1
                self.stats.last_error = str(exc)[:200]
                last_exc = exc
                time.sleep(min(2.0, 0.3 * (2 ** attempt)))
                continue
            rtt = (time.time() - t0) * 1000.0
            self.stats.rtt_ms_ewma = rtt if not self.stats.rtt_ms_ewma else 0.9 * self.stats.rtt_ms_ewma + 0.1 * rtt
            self._note_date(resp.headers.get("Date"), t0, time.time())
            if resp.status_code == 429:
                self.stats.http_429 += 1
                self.stats.errors += 1
                self._blocked_until = time.time() + self.backoff_on_429_s * (attempt + 1)
                last_exc = HttpError(429, "rate limited", url)
                log.warning("HTTP 429 on %s; backing off %.0fs", path, self.backoff_on_429_s * (attempt + 1))
                continue
            if resp.status_code >= 500:
                self.stats.errors += 1
                last_exc = HttpError(resp.status_code, resp.text, url)
                time.sleep(min(2.0, 0.3 * (2 ** attempt)))
                continue
            if resp.status_code >= 400:
                self.stats.errors += 1
                self.stats.last_error = f"{resp.status_code} {path}"
                raise HttpError(resp.status_code, resp.text, url)
            self.stats.last_ok_ts = time.time()
            return resp.json() if resp.content else {}
        self.stats.last_error = str(last_exc)[:200]
        raise last_exc or HttpError(0, "request failed", url)

    def _note_date(self, date_hdr: str | None, t0: float, t1: float) -> None:
        if not date_hdr:
            return
        try:
            server = email.utils.parsedate_to_datetime(date_hdr).timestamp()
        except (TypeError, ValueError):
            return
        # Date has 1 s resolution: compare with the request midpoint and only
        # count skew beyond that resolution.
        mid = (t0 + t1) / 2.0
        diff = mid - server
        skew = 0.0 if -1.0 <= diff <= 1.0 else (diff - 1.0 if diff > 0 else diff + 1.0)
        self.stats.clock_skew_s = 0.8 * self.stats.clock_skew_s + 0.2 * skew
