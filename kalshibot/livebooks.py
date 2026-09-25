"""Read the REAL order book. Read only, unauthenticated, cannot trade.

Why this exists: demo books are synthetic. Every fill-quality number this bot
has measured - spread, depth, slippage, whether 25 contracts move the price -
was measured against a fiction. The forecast edge is real and measured; the
execution cost on real books is completely unknown, and it is the difference
between a good forecast and a profitable trade.

Kalshi's market data is public. Their docs: "No authentication headers are
required for the endpoints in this guide." So this reads live books with **no
API key, no signature, and no account**. There is no credential to leak,
because there is no credential.

What makes it unable to trade, structurally rather than by promise:

  1. No signer. It cannot authenticate, and an unauthenticated request cannot
     act on an account even if one were somehow addressed.
  2. GET only. `_get` is the single request method and it hard-codes GET.
  3. A path allowlist in env_guard, checked on every call, that refuses
     anything containing portfolio, orders, fills or balance.
  4. No place_order, no place_demo_order, no cancel. Calling one is an
     AttributeError, not a trade.

`assert_demo_url` is untouched and every trading path still calls it, so the
execution client still refuses production unconditionally. This class and that
one cannot be confused: this one has no method that sends an order.
"""
from __future__ import annotations

import logging

import requests

from . import sources as _sources
from .depth import DepthBook, Level
from .env_guard import assert_public_market_data

log = logging.getLogger(__name__)

#: Kalshi production, market data only.
LIVE_MARKET_DATA_BASE = "https://external-api.kalshi.com/trade-api/v2"


class LiveBookReader:
    """Public market data from the live exchange. Reads books; sends nothing."""

    def __init__(self, base_url: str = LIVE_MARKET_DATA_BASE,
                 timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        # Deliberately no auth headers are ever set on this session.
        self._session.headers.update({"Accept": "application/json"})

    # ------------------------------------------------------------------
    def _get(self, path: str, params: dict | None = None) -> dict:
        """The only request this class can make. GET, allowlisted, no auth."""
        assert_public_market_data(self.base_url, path, "GET")
        with _sources.MONITOR.track("kalshi_live_market_data") as call:
            resp = self._session.get(f"{self.base_url}{path}", params=params,
                                     timeout=self.timeout)
            call.status = getattr(resp, "status_code", None)
            # A 404 is a market that does not exist on production, not the
            # exchange being down.
            if call.status == 404:
                call.ok = True
            resp.raise_for_status()
            return resp.json() or {}

    # ------------------------------------------------------------------
    def orderbook(self, ticker: str, depth: int = 50) -> DepthBook | None:
        """The live book for one market, in the same shape as the demo one."""
        try:
            raw = self._get(f"/markets/{ticker}/orderbook",
                            {"depth": depth}).get("orderbook") or {}
        except Exception as exc:  # noqa: BLE001
            log.debug("LIVE  no book for %s: %s", ticker, exc)
            return None
        return self._to_book(ticker, raw)

    @staticmethod
    def _to_book(ticker: str, raw: dict) -> DepthBook | None:
        """Kalshi returns resting BIDS on each side, in cents, as [price, qty].

        The YES asks are the NO bids mirrored: someone bidding 40c for NO is
        offering YES at 60c. Getting that mirror wrong would invent liquidity
        that is not there, which for the one purpose of this module - finding
        out what the real book would have done to us - is the worst possible
        error.
        """
        yes = raw.get("yes") or []
        no = raw.get("no") or []
        if not yes and not no:
            return None

        def levels(rows):
            out = []
            for row in rows or []:
                try:
                    price, qty = int(row[0]), int(row[1])
                except (TypeError, ValueError, IndexError):
                    continue
                if 1 <= price <= 99 and qty > 0:
                    out.append(Level(price, qty))
            return out

        # DepthBook takes the two BID ladders and derives the ask ladders
        # itself. Handing it a pre-mirrored ask list would mirror twice and
        # produce a book that does not exist.
        return DepthBook(ticker, levels(yes), levels(no))

    def market(self, ticker: str) -> dict:
        return (self._get(f"/markets/{ticker}").get("market") or {})

    def ping(self) -> bool:
        """Is the live market data reachable? Used at startup, not per pass."""
        try:
            self._get("/markets", {"limit": 1})
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("LIVE  public market data unreachable: %s", exc)
            return False


class LiveBookMarketClient:
    """The demo market client, with the ORDER BOOK taken from production.

    Discovery, market details and settlement stay on demo: those agree between
    the two exchanges and the demo client is already authenticated for them.
    Only the book is swapped, because the book is the only part that is
    synthetic and the only part that decides what a fill costs.

    When the live exchange has no book for a ticker this returns **None**
    rather than falling back to the demo book. A fallback would quietly put
    the fiction back into the measurement, which is the one thing this whole
    exercise exists to remove. The misses are counted and reported instead.
    """

    def __init__(self, demo_client, reader: LiveBookReader | None = None):
        self._demo = demo_client
        self.reader = reader or LiveBookReader()
        self.hits = 0
        self.misses = 0

    def __getattr__(self, name):
        # Everything not overridden below goes to the demo client unchanged.
        return getattr(self._demo, name)

    def get_depth(self, ticker: str, depth: int = 50):
        book = self.reader.orderbook(ticker, depth=depth)
        if book is None:
            self.misses += 1
            return None
        self.hits += 1
        return book

    def summary(self) -> str:
        total = self.hits + self.misses
        if not total:
            return "live books: nothing read yet"
        return (f"live books: {self.hits} read, {self.misses} not listed on "
                f"production ({self.misses / total:.0%})")
