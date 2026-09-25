"""Three separate clients, on purpose.

Market data is read-only and harmless. Portfolio reads your account. Only the
execution client can place an order, it is demo-only, and its method is called
place_demo_order() so a stray place_order() is an AttributeError instead of a
real trade.
"""
from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .client import KalshiClient, KalshiError
from .depth import DepthBook
from .env_guard import assert_demo_url, assert_trading_allowed
from .models import Market, Position

log = logging.getLogger(__name__)


class MarketDataClient:
    """Read-only. Markets, events, order books. Never places anything."""

    def __init__(self, base_url: str, signer=None, **kw):
        assert_demo_url(base_url, "market data client")
        self._c = KalshiClient(base_url, signer, **kw)
        self.base_url = base_url

    def exchange_status(self) -> dict:
        return self._c.exchange_status()

    def get_markets(self, status: str = "open", series_ticker: str | None = None,
                    max_pages: int = 60) -> list[Market]:
        return self._c.get_markets(series_ticker=series_ticker, status=status,
                                   max_pages=max_pages)

    def get_market(self, ticker: str) -> Market:
        return self._c.get_market(ticker)

    def get_events(self, status: str = "open") -> list[dict]:
        return self._c.get_events(status=status)

    def get_depth(self, ticker: str, depth: int = 50) -> DepthBook:
        raw = self._c._request("GET", f"/markets/{ticker}/orderbook",
                               params={"depth": depth},
                               authed=self._c.signer is not None, public_ok=True)
        return DepthBook.from_api(ticker, raw)

    def get_depths(self, tickers: list[str], depth: int = 50,
                   workers: int = 8) -> dict[str, DepthBook]:
        """Fetch many order books at once.

        Sequentially this is the slowest thing the bot does - 400 books at
        roughly half a second each is most of a four-minute scan. The shared
        token bucket still enforces the rate limit; the threads just stop us
        waiting on network latency one call at a time.
        """
        if not tickers:
            return {}
        out: dict[str, DepthBook] = {}

        def one(ticker: str):
            try:
                return ticker, self.get_depth(ticker, depth)
            except Exception as exc:   # noqa: BLE001
                log.debug("orderbook %s failed: %s", ticker, exc)
                return ticker, None

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for ticker, book in pool.map(one, tickers):
                if book is not None:
                    out[ticker] = book
        return out


class PortfolioClient:
    """Account state. Reads only."""

    def __init__(self, base_url: str, signer, **kw):
        assert_demo_url(base_url, "portfolio client")
        self._c = KalshiClient(base_url, signer, **kw)
        self.base_url = base_url

    def balance_usd(self) -> float:
        return self._c.get_balance_cents() / 100.0

    def positions(self) -> list[Position]:
        return self._c.get_positions()

    def position_for(self, ticker: str) -> Position | None:
        return next((p for p in self.positions() if p.ticker == ticker), None)

    def orders(self, status: str | None = None) -> list[dict]:
        return self._c.get_orders(status=status)

    def order(self, order_id: str) -> dict:
        return self._c._request("GET", f"/portfolio/orders/{order_id}")

    def fills(self, limit: int = 200) -> list[dict]:
        return self._c.get_fills(limit=limit)


class DemoExecutionClient:
    """The ONLY thing in this codebase that can place an order.

    Demo-only by construction. Both the environment name and the URL are
    re-checked on every single call, not just at startup, so a later config
    edit cannot open a path to production.
    """

    def __init__(self, base_url: str, signer, env: str, **kw):
        assert_trading_allowed(env, base_url, "execution client construction")
        self.env = env
        self.base_url = base_url
        self._c = KalshiClient(base_url, signer, **kw)

    # -- deliberately NOT named place_order --------------------------
    def place_demo_order(
        self,
        ticker: str,
        action: str,
        side: str,
        count: int,
        limit_price_cents: int,
        client_order_id: str | None = None,
        post_only: bool = False,
        time_in_force: str | None = None,
    ) -> dict[str, Any]:
        assert_trading_allowed(self.env, self.base_url, f"order on {ticker}")
        if count <= 0:
            raise ValueError(f"refusing to send an order for {count} contracts")
        if not 1 <= limit_price_cents <= 99:
            raise ValueError(f"limit price {limit_price_cents}c out of range 1-99")
        if action not in ("buy", "sell") or side not in ("yes", "no"):
            raise ValueError(f"bad order: action={action!r} side={side!r}")

        coid = client_order_id or f"kb-{uuid.uuid4().hex[:16]}"
        book_side, yes_price = self._c.to_yes_book(action, side, limit_price_cents)
        log.info("ORDER SUBMIT  %s %s %s x%d @ %dc  (V2 %s @ %dc on the YES "
                 "book)  coid=%s", ticker, action, side, count,
                 limit_price_cents, book_side, yes_price, coid)
        raw = self._c.create_order(
            ticker=ticker, action=action, side=side, count=count,
            limit_price_cents=limit_price_cents, client_order_id=coid,
            order_type="limit", post_only=post_only, time_in_force=time_in_force,
        )
        return {"order": normalise_order(raw)}

    def cancel_demo_order(self, order_id: str, market_ticker: str | None = None) -> dict:
        assert_trading_allowed(self.env, self.base_url, f"cancel {order_id}")
        log.info("ORDER CANCEL  %s", order_id)
        return self._c.cancel_order(order_id, market_ticker)

    def get_order(self, order_id: str) -> dict:
        raw = self._c._request("GET", f"/portfolio/orders/{order_id}")
        return {"order": normalise_order(raw)}


def _num(order: dict, *keys) -> float | None:
    for k in keys:
        v = order.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def normalise_order(payload: dict) -> dict:
    """One order shape for the rest of the bot, whatever the wire sent.

    Kalshi's V2 order endpoints changed the field names AND the units under
    them, and the two endpoints do not even agree with each other:

        create  ->  fill_count, remaining_count, average_fill_price (DOLLARS)
        get     ->  fill_count_fp, remaining_count_fp, status
        legacy  ->  filled_count, yes_price / no_price (CENTS)

    Everything downstream reads the normalised keys, so a future rename is one
    edit here rather than a hunt through the execution manager.
    """
    o = payload.get("order", payload) or {}

    filled = _num(o, "fill_count", "fill_count_fp", "filled_count")
    remaining = _num(o, "remaining_count", "remaining_count_fp")
    initial = _num(o, "initial_count_fp", "count")

    # average_fill_price is fixed-point DOLLARS on the YES book; the legacy
    # yes_price/no_price were integer CENTS. Do not mix them up.
    yes_price_cents = None
    dollars = _num(o, "average_fill_price", "avg_fill_price")
    if dollars is not None and dollars > 0:
        yes_price_cents = dollars * 100.0
    elif _num(o, "yes_price") is not None:
        yes_price_cents = _num(o, "yes_price")
    elif _num(o, "no_price") is not None:
        yes_price_cents = 100.0 - _num(o, "no_price")

    status = (o.get("status") or "").lower()
    if not status:
        # V2 create returns no status. Derive it from what is left resting.
        if remaining is not None:
            status = "resting" if remaining > 0 else "executed"
        elif filled is not None and initial is not None:
            status = "executed" if filled >= initial else "resting"
    return {
        "order_id": str(o.get("order_id", "")),
        "client_order_id": o.get("client_order_id", ""),
        "status": status or "unknown",
        "filled_count": int(filled) if filled is not None else None,
        "remaining_count": int(remaining) if remaining is not None else None,
        "initial_count": int(initial) if initial is not None else None,
        "yes_price_cents": yes_price_cents,
        "raw": o,
    }


def price_in_leg_terms(side: str, yes_price_cents: float | None) -> float | None:
    """Convert a YES-book price back into the frame the caller thinks in."""
    if yes_price_cents is None:
        return None
    return yes_price_cents if side == "yes" else 100.0 - yes_price_cents


def build_clients(settings, signer):
    """One place that decides which URLs the three clients get."""
    base = settings.rest_base
    assert_demo_url(base, "client construction")
    market = MarketDataClient(base, signer)
    portfolio = PortfolioClient(base, signer) if signer else None
    return market, portfolio
