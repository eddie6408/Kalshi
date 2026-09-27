"""Kalshi V2 order encoding - a pure function, no network, no credentials.

Kalshi retired POST /portfolio/orders on 2026-09-16. The replacement,
POST /portfolio/events/orders, quotes ONE book (the YES book). There is no
"buy NO" on the wire; buying NO at N cents is selling YES at 100-N:

    buy  YES @ 40c  ->  side "bid", price 0.4000
    sell YES @ 40c  ->  side "ask", price 0.4000
    buy  NO  @ 57c  ->  side "ask", price 0.4300   (sells YES)
    sell NO  @ 57c  ->  side "bid", price 0.4300   (buys YES)

Getting this backwards places the OPPOSITE trade at a plausible-looking
price, so it is isolated here and covered by tests. SHADOW mode runs every
order through this function so the encoding is exercised long before LIVE.
"""
from __future__ import annotations

from lvt.models import NO, YES, OrderRequest

ORDER_PATH = "/portfolio/events/orders"
TIME_IN_FORCE = "immediate_or_cancel"


class EncodingError(ValueError):
    pass


def to_yes_book(action: str, side: str, price_cents: int) -> tuple[str, int]:
    if action not in ("buy", "sell"):
        raise EncodingError(f"bad action {action!r}")
    if side not in (YES, NO):
        raise EncodingError(f"bad side {side!r}")
    if not 1 <= int(price_cents) <= 99 or int(price_cents) != price_cents:
        raise EncodingError(f"price {price_cents} must be a whole cent in 1-99")
    if side == YES:
        return ("bid" if action == "buy" else "ask"), int(price_cents)
    return ("ask" if action == "buy" else "bid"), 100 - int(price_cents)


def encode_order(req: OrderRequest) -> dict:
    if int(req.count) != req.count or req.count < 1:
        raise EncodingError(f"count {req.count} must be a positive integer")
    if not req.client_order_id:
        raise EncodingError("client_order_id is required (duplicate-order protection)")
    book_side, yes_price = to_yes_book(req.action, req.side, req.limit_price)
    body = {
        "ticker": req.ticker,
        "side": book_side,
        "count": f"{int(req.count)}",
        "price": f"{yes_price / 100.0:.4f}",
        "time_in_force": TIME_IN_FORCE,
        "self_trade_prevention_type": "taker_at_cross",
        "client_order_id": req.client_order_id,
    }
    if req.action == "sell" or req.purpose == "EXIT":
        body["reduce_only"] = True
    return body
