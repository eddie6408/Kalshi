"""Execution interface + the one Kalshi order-payload builder shared by SHADOW and LIVE.

DATA and EXECUTION are separate connections. Receiving live data grants no
permission to trade; only KalshiLiveExecution can send an order, and it can
only be constructed through `execution.factory.build_execution` in LIVE mode
after the readiness gate passes.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

from ..models import Fill, MarketMeta, MarketSnapshot, Order, OrderBook, OrderStatus, TradePrint


@dataclass
class ExecutionEvent:
    order: Order
    fills: list[Fill] = field(default_factory=list)
    note: str = ""


class ExecutionClient(abc.ABC):
    env: str = "BASE"
    live: bool = False

    @abc.abstractmethod
    async def submit(self, order: Order, meta: MarketMeta, now: float) -> list[ExecutionEvent]:
        """Send (or simulate sending) an order. Caller has already persisted it as PENDING_SUBMIT."""

    @abc.abstractmethod
    async def on_market(self, ticker: str, snap: MarketSnapshot | None, book: OrderBook | None,
                        trades: list[TradePrint], now: float, meta: MarketMeta | None = None) -> list[ExecutionEvent]:
        """Advance working orders with new market data (paper/shadow fills, live polling)."""

    @abc.abstractmethod
    async def cancel(self, order: Order, now: float) -> ExecutionEvent:
        ...

    @abc.abstractmethod
    def working_orders(self) -> list[Order]:
        ...

    async def close(self) -> None:  # pragma: no cover
        pass


def round_to_tick(price: float, tick: float = 1.0) -> float:
    t = tick if tick and tick > 0 else 1.0
    return round(round(price / t) * t, 4)


def clamp_price(price: float) -> float:
    return min(99.0, max(1.0, price))


def build_order_payload(order: Order, tick: float = 1.0, live_cfg=None) -> dict[str, Any]:
    """Exact JSON body for POST /portfolio/orders. Used verbatim by LIVE and logged by SHADOW."""
    price = clamp_price(round_to_tick(order.limit_price, tick))
    body: dict[str, Any] = {
        "ticker": order.ticker,
        "client_order_id": order.client_order_id,
        "side": order.side,
        "action": order.action,
        "count": int(order.count),
        "type": "limit",
        f"{order.side}_price_dollars": f"{price / 100:.4f}",
    }
    if order.style == "maker":
        body["post_only"] = True
        if order.expires_ts:
            body["expiration_ts"] = int(order.expires_ts)
    else:
        body["time_in_force"] = getattr(live_cfg, "time_in_force", "immediate_or_cancel") if live_cfg else "immediate_or_cancel"
    if order.action == "sell":
        body["reduce_only"] = True   # an exit can never open or flip exposure
    return body


def validate_payload(body: dict[str, Any]) -> list[str]:
    errs = []
    for k in ("ticker", "client_order_id", "side", "action", "count", "type"):
        if k not in body:
            errs.append(f"missing {k}")
    if body.get("side") not in ("yes", "no"):
        errs.append("side must be yes/no")
    if body.get("action") not in ("buy", "sell"):
        errs.append("action must be buy/sell")
    if not isinstance(body.get("count"), int) or body.get("count", 0) < 1:
        errs.append("count must be a positive integer")
    key = f"{body.get('side')}_price_dollars"
    try:
        p = float(body.get(key, "nan"))
        if not (0.01 <= p <= 0.99):
            errs.append(f"{key} out of range")
    except ValueError:
        errs.append(f"{key} not numeric")
    return errs


def terminal(order: Order) -> bool:
    return order.status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.MISSED)
