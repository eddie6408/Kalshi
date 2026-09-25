"""Order routing.

One interface, three back ends:

    PaperBroker : never talks to Kalshi's trading endpoints. Simulates fills
                  against the live orderbook. This is where you live for weeks.
    LiveBroker  : sends real orders. Used for both the Kalshi demo environment
                  (fake money) and production (real money) - the difference is
                  which base URL the client was built with, plus an explicit
                  confirmation flag for production.

Both return the same Fill object, so nothing upstream knows or cares.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from .client import KalshiClient, KalshiError
from .fees import FeeModel
from .models import Orderbook, SizedOrder

log = logging.getLogger(__name__)


@dataclass
class Fill:
    ok: bool
    ticker: str
    side: str
    action: str
    contracts: int
    price_cents: int
    fee_usd: float
    order_id: str = ""
    client_order_id: str = ""
    status: str = ""
    error: str = ""


def _client_order_id(ticker: str) -> str:
    return f"kb-{ticker[:24]}-{uuid.uuid4().hex[:10]}"


class Broker:
    mode = "base"

    def place(self, order: SizedOrder, book: Orderbook | None) -> Fill:  # pragma: no cover
        raise NotImplementedError

    def cancel_all(self) -> int:
        return 0


class PaperBroker(Broker):
    mode = "paper"

    def __init__(self, fees: FeeModel, fill_model: str = "cross"):
        self.fees = fees
        self.fill_model = fill_model
        self.simulated_cash_used_usd = 0.0

    def place(self, order: SizedOrder, book: Orderbook | None) -> Fill:
        intent = order.intent
        price = intent.limit_price_cents
        if self.fill_model == "mid" and intent.action == "buy" and book is not None:
            best = book.best_yes_bid() if intent.side == "yes" else book.best_no_bid()
            if best:
                price = max(1, min(99, int(round((price + best.price_cents) / 2))))

        available = None
        if book is not None:
            available = book.depth_for(intent.side, intent.action, price)
        n = order.contracts if available is None else min(order.contracts, available)
        if n <= 0:
            return Fill(False, intent.ticker, intent.side, intent.action, 0, price, 0.0,
                        error=f"no simulated liquidity to {intent.action} "
                              f"{intent.side} at {price}c")

        fee = self.fees.entry_fee_usd(price, n)
        self.simulated_cash_used_usd += n * price / 100.0 + fee
        coid = _client_order_id(intent.ticker)
        log.info("[PAPER] %s %d %s @ %dc on %s (fee $%.2f)",
                 intent.action.upper(), n, intent.side.upper(), price, intent.ticker, fee)
        return Fill(True, intent.ticker, intent.side, intent.action, n, price, fee,
                    order_id=f"paper-{coid}", client_order_id=coid, status="simulated")


class LiveBroker(Broker):
    def __init__(self, client: KalshiClient, fees: FeeModel, mode: str = "demo",
                 post_only: bool = False):
        self.client = client
        self.fees = fees
        self.mode = mode
        self.post_only = post_only

    def place(self, order: SizedOrder, book: Orderbook | None) -> Fill:
        intent = order.intent
        coid = _client_order_id(intent.ticker)
        try:
            resp = self.client.create_order(
                ticker=intent.ticker,
                action=intent.action,
                side=intent.side,
                count=order.contracts,
                limit_price_cents=intent.limit_price_cents,
                client_order_id=coid,
                order_type="limit",
                post_only=self.post_only,
            )
        except KalshiError as exc:
            log.error("Order rejected by Kalshi: %s", exc)
            return Fill(False, intent.ticker, intent.side, intent.action, 0,
                        intent.limit_price_cents, 0.0, client_order_id=coid, error=str(exc))

        o = resp.get("order", resp) or {}
        filled = int(o.get("filled_count") or o.get("count") or order.contracts)
        price = int(o.get("yes_price") or o.get("no_price") or intent.limit_price_cents)
        fee = self.fees.entry_fee_usd(price, filled)
        log.info("[%s] order %s status=%s %d @ %dc on %s",
                 self.mode.upper(), o.get("order_id", "?"), o.get("status", "?"),
                 filled, price, intent.ticker)
        return Fill(True, intent.ticker, intent.side, intent.action, filled, price, fee,
                    order_id=str(o.get("order_id", "")), client_order_id=coid,
                    status=str(o.get("status", "")))

    def cancel_all(self) -> int:
        n = 0
        for o in self.client.get_orders(status="resting"):
            try:
                self.client.cancel_order(o["order_id"])
                n += 1
            except KalshiError as exc:
                log.warning("Could not cancel %s: %s", o.get("order_id"), exc)
        return n


def build_broker(settings, client: KalshiClient | None, fees: FeeModel) -> Broker:
    mode = settings.mode
    if mode == "paper":
        return PaperBroker(fees, str(settings.get("execution.paper_fill_model", "cross")))
    if client is None:
        raise RuntimeError(f"execution.mode={mode} requires API credentials")
    if mode == "live":
        log.warning("=" * 68)
        log.warning("LIVE MODE - THIS BOT IS ABOUT TO SPEND REAL MONEY")
        log.warning("=" * 68)
    return LiveBroker(client, fees, mode=mode)
