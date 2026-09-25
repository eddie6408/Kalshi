"""Thin, well-behaved REST client for the Kalshi trade API v2."""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Iterator
from urllib.parse import urlparse

import requests

from .auth import Signer
from .models import Market, Orderbook, Position

log = logging.getLogger(__name__)


class KalshiError(RuntimeError):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} from {url}: {body[:400]}")
        self.status = status
        self.body = body


class TokenBucket:
    """Client-side throttle so we never hammer Kalshi into a 429."""

    def __init__(self, tokens_per_sec: float, burst: float | None = None):
        self.rate = float(tokens_per_sec)
        self.capacity = float(burst if burst is not None else tokens_per_sec)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def take(self, cost: float = 10.0) -> None:
        """Thread-safe. Order book fetches run in parallel, so several
        threads hit this at once; without the lock the budget leaks and we
        would sail past Kalshi's rate limit."""
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity,
                                   self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                wait = max(0.01, (cost - self._tokens) / self.rate)
            time.sleep(wait)


class KalshiClient:
    """All calls are synchronous and retry-safe.

    Read budget defaults are deliberately well under the Basic tier
    (200 read tokens/sec, 100 write tokens/sec, 10 tokens per request).
    """

    def __init__(
        self,
        base_url: str,
        signer: Signer | None = None,
        read_tokens_per_sec: float = 60.0,
        write_tokens_per_sec: float = 20.0,
        timeout: float = 20.0,
        max_retries: int = 4,
    ):
        self.base_url = base_url.rstrip("/")
        self.base_path = urlparse(self.base_url).path.rstrip("/")  # e.g. /trade-api/v2
        self.signer = signer
        self.timeout = timeout
        self.max_retries = max_retries
        self._read_bucket = TokenBucket(read_tokens_per_sec)
        self._write_bucket = TokenBucket(write_tokens_per_sec)
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "kalshibot/1.0"})
        adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    # ---------------- low level ----------------
    def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_body: dict | None = None,
        authed: bool = True,
        public_ok: bool = False,
    ) -> dict[str, Any]:
        """public_ok: if the signed request is rejected, retry unsigned.

        Market data on Kalshi is public. This lets you point a DEMO key at the
        PRODUCTION exchange to read real prices in paper mode - which is the
        most useful configuration there is, because the demo exchange often has
        no order books at all while production does.
        """
        method = method.upper()
        url = f"{self.base_url}{path}"
        full_path = f"{self.base_path}{path}"       # what Kalshi wants signed
        bucket = self._read_bucket if method == "GET" else self._write_bucket

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            bucket.take(10.0)
            headers: dict[str, str] = {"Accept": "application/json"}
            if authed:
                if not self.signer:
                    raise KalshiError(401, "No API credentials configured", url)
                headers.update(self.signer.sign(method, full_path))
            try:
                resp = self._session.request(
                    method, url, params=params, json=json_body,
                    headers=headers, timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_exc = exc
                self._backoff(attempt)
                continue

            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                last_exc = KalshiError(resp.status_code, resp.text, url)
                self._backoff(attempt)
                continue
            # Kalshi rejects a signature whose timestamp arrived too late.
            # The signature is fine - it just aged in flight, or this machine's
            # clock drifted. Re-sign and try again rather than failing the
            # trade: "header timestamp expired" aborted a real order mid-flight.
            if (resp.status_code == 401
                    and "timestamp" in resp.text.lower()
                    and attempt < self.max_retries):
                log.warning("signature timestamp rejected on %s; re-signing "
                            "(attempt %d)", path, attempt + 1)
                continue
            if resp.status_code in (401, 403) and authed and public_ok:
                log.debug("signed request rejected on %s; retrying unauthenticated", path)
                authed = False
                continue
            if resp.status_code >= 400:
                raise KalshiError(resp.status_code, resp.text, url)
            if not resp.content:
                return {}
            return resp.json()

        raise last_exc or KalshiError(0, "request failed", url)

    @staticmethod
    def _backoff(attempt: int) -> None:
        delay = min(2.0, 0.25 * (2 ** attempt)) + random.uniform(0, 0.10)
        time.sleep(delay)

    def _paginate(self, path: str, params: dict, key: str, page_limit: int = 20) -> Iterator[dict]:
        params = dict(params)
        params.setdefault("limit", 200)
        cursor = None
        seen = 0
        started = time.time()
        for page in range(page_limit):
            if cursor:
                params["cursor"] = cursor
            data = self._request("GET", path, params=params,
                                 authed=self.signer is not None, public_ok=True)
            rows = data.get(key, []) or []
            seen += len(rows)
            for row in rows:
                yield row
            # Paging 80,000 markets takes several minutes with no sign of life,
            # which reads as a hung bot. Say something every 50 pages.
            if page and page % 50 == 0:
                log.info("PAGING  %s: %d %s so far (%d pages, %.0fs)",
                         path, seen, key, page + 1, time.time() - started)
            cursor = data.get("cursor") or None
            if not cursor:
                return
            time.sleep(0.25)

    # ---------------- public market data ----------------
    def exchange_status(self) -> dict:
        return self._request("GET", "/exchange/status", authed=False)

    def probe_market_data(self) -> tuple[bool, str]:
        """Can we read market data here at all? Used by diagnostics."""
        try:
            self._request("GET", "/markets", params={"limit": 1},
                          authed=self.signer is not None, public_ok=True)
            return True, "ok"
        except KalshiError as exc:
            return False, str(exc)

    def get_markets(
        self,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        status: str = "open",
        max_pages: int = 20,
    ) -> list[Market]:
        params: dict[str, Any] = {"status": status}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        return [Market.from_api(r)
                for r in self._paginate("/markets", params, "markets", max_pages)]

    def get_market(self, ticker: str) -> Market:
        d = self._request("GET", f"/markets/{ticker}",
                          authed=self.signer is not None, public_ok=True)
        return Market.from_api(d.get("market", d))

    def get_orderbook(self, ticker: str, depth: int = 20) -> Orderbook:
        d = self._request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth},
                          authed=self.signer is not None, public_ok=True)
        return Orderbook.from_api(ticker, d)

    def get_events(self, series_ticker: str | None = None, status: str = "open") -> list[dict]:
        params: dict[str, Any] = {"status": status}
        if series_ticker:
            params["series_ticker"] = series_ticker
        return list(self._paginate("/events", params, "events"))

    # ---------------- portfolio ----------------
    def get_balance_cents(self) -> int:
        return int(self._request("GET", "/portfolio/balance").get("balance", 0))

    def get_positions(self) -> list[Position]:
        data = self._request("GET", "/portfolio/positions", params={"limit": 200})
        rows = data.get("market_positions", []) or []
        return [p for p in (Position.from_api(r) for r in rows) if p.contracts or p.resting_orders]

    def get_orders(self, status: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"limit": 200}
        if status:
            params["status"] = status
        return self._request("GET", "/portfolio/orders", params=params).get("orders", []) or []

    def get_fills(self, limit: int = 200) -> list[dict]:
        return self._request("GET", "/portfolio/fills", params={"limit": limit}).get("fills", []) or []

    # ---------------- trading ----------------
    #: Kalshi retired POST /portfolio/orders on 2026-09-16 with
    #: HTTP 410 deprecated_v1_order_endpoint. The replacement is
    #: POST /portfolio/events/orders, and it is not a rename - the model
    #: changed underneath it.
    ORDER_PATH_V2 = "/portfolio/events/orders"

    @staticmethod
    def to_yes_book(action: str, side: str, limit_price_cents: int
                    ) -> tuple[str, int]:
        """Translate (buy|sell, yes|no) into the V2 single-book YES side.

        THIS IS THE DANGEROUS FUNCTION IN THIS FILE. V2 quotes ONE book - the
        YES book - so there is no "buy NO" any more. Buying NO at N cents is
        the same position as selling YES at (100 - N) cents:

            buy  YES @ 40c  ->  bid @ 40c
            sell YES @ 40c  ->  ask @ 40c
            buy  NO  @ 57c  ->  ask @ 43c      <- sells YES
            sell NO  @ 57c  ->  bid @ 43c      <- buys YES

        Get this backwards and the bot places the OPPOSITE trade at a price
        that still looks plausible in the log. Returns (side, price_cents)
        where side is "bid" or "ask" and the price is on the YES book.
        """
        if action not in ("buy", "sell"):
            raise ValueError(f"bad action {action!r}")
        if side not in ("yes", "no"):
            raise ValueError(f"bad side {side!r}")
        if not 1 <= int(limit_price_cents) <= 99:
            raise ValueError(f"price {limit_price_cents}c out of range 1-99")

        if side == "yes":
            yes_price = int(limit_price_cents)
            book_side = "bid" if action == "buy" else "ask"
        else:
            yes_price = 100 - int(limit_price_cents)
            book_side = "ask" if action == "buy" else "bid"
        return book_side, yes_price

    def create_order(
        self,
        ticker: str,
        action: str,              # "buy" | "sell"
        side: str,                # "yes" | "no"
        count: int,
        limit_price_cents: int,
        client_order_id: str,
        order_type: str = "limit",
        post_only: bool = False,
        time_in_force: str | None = None,
        expiration_ts: int | None = None,
    ) -> dict:
        """Create an order on the V2 endpoint.

        The caller still speaks yes/no and cents, because that is how the rest
        of this bot reasons about a market. The translation to the exchange's
        wire model happens here and nowhere else.
        """
        book_side, yes_price_cents = self.to_yes_book(
            action, side, limit_price_cents)

        body: dict[str, Any] = {
            "ticker": ticker,
            "side": book_side,
            # V2 wants fixed-point STRINGS, and the price is in DOLLARS:
            # 43 cents is "0.4300", not 43.
            "count": f"{int(count)}",
            "price": f"{yes_price_cents / 100.0:.4f}",
            # Both of these are REQUIRED by V2. good_till_canceled matches the
            # old default, and the execution manager cancels the remainder
            # itself after its fill timeout.
            "time_in_force": time_in_force or "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": client_order_id,
        }
        if post_only:
            body["post_only"] = True
        if expiration_ts is not None:
            body["expiration_time"] = int(expiration_ts)
        return self._request("POST", self.ORDER_PATH_V2, json_body=body)

    def cancel_order(self, order_id: str, market_ticker: str | None = None) -> dict:
        # V2 cancel lives under the same events path. market_ticker lets the
        # exchange auto-route without an explicit shard index.
        params = {"market_ticker": market_ticker} if market_ticker else None
        return self._request("DELETE", f"{self.ORDER_PATH_V2}/{order_id}",
                             params=params)
