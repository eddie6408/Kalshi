"""LIVE execution against the Kalshi REST API (production, or demo for validation).

Safety properties:
  * constructed only by execution.factory after the LIVE readiness gate passes
    (or by the explicit demo execution validator with DEMO credentials);
  * order POSTs are NEVER retried automatically. A timeout / network error
    leaves the order UNKNOWN, which halts new trading until the reconciler
    finds the order by client_order_id (duplicate-order protection);
  * exits are sent with reduce_only so they cannot open or flip exposure;
  * Kalshi is the source of truth for orders, fills and positions.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from ..data.base import DataUnavailable, RateLimiter
from ..data.kalshi_rest import ExchangeHTTPError, KalshiRestClient
from ..data.parsing import count_field, dollars_to_cents, iso_ts, num, price_field
from ..engine.fees import FeeModel
from ..models import Fill, MarketMeta, Order, OrderStatus
from .base import ExecutionClient, ExecutionEvent, build_order_payload, validate_payload

log = logging.getLogger("klvb.exec.live")

UNKNOWN_GRACE_SECONDS = 30


class KalshiLiveExecution(ExecutionClient):
    live = True

    def __init__(self, live_cfg, fees: FeeModel, signer, base_url: str, env_name: str = "LIVE",
                 transport=None, max_rps: float = 5.0):
        self.env = env_name
        self.cfg = live_cfg
        self.fees = fees
        self.client = KalshiRestClient(base_url, RateLimiter(max_rps), timeout=10.0, max_retries=0,
                                       transport=transport, auth=signer)
        self.working: dict[str, tuple[Order, MarketMeta]] = {}
        self.seen_fill_ids: set[str] = set()
        self.last_poll: dict[str, float] = {}

    async def close(self) -> None:
        await self.client.close()

    def working_orders(self) -> list[Order]:
        return [o for o, _ in self.working.values()]

    # ---- account reads (reconciliation) ------------------------------------
    async def balance(self) -> dict[str, Any]:
        return await self.client.get("/portfolio/balance")

    async def positions(self) -> list[dict[str, Any]]:
        out, cursor = [], None
        for _ in range(20):
            params: dict[str, Any] = {"limit": 1000, "count_filter": "position"}
            if cursor:
                params["cursor"] = cursor
            data = await self.client.get("/portfolio/positions", params)
            out += data.get("market_positions") or []
            cursor = data.get("cursor")
            if not cursor:
                break
        return out

    async def orders(self, status: str | None = None, ticker: str | None = None,
                     min_ts: float | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": 1000}
        if status:
            params["status"] = status
        if ticker:
            params["ticker"] = ticker
        if min_ts:
            params["min_ts"] = int(min_ts)
        data = await self.client.get("/portfolio/orders", params)
        return data.get("orders") or []

    async def fills(self, order_id: str | None = None, min_ts: float | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": 1000}
        if order_id:
            params["order_id"] = order_id
        if min_ts:
            params["min_ts"] = int(min_ts)
        data = await self.client.get("/portfolio/fills", params)
        return data.get("fills") or []

    # ---- order lifecycle ----------------------------------------------------
    def _fill_from(self, f: dict[str, Any], order: Order, meta: MarketMeta | None) -> Fill | None:
        fid = f.get("fill_id") or f.get("trade_id")
        if not fid or fid in self.seen_fill_ids:
            return None
        side = f.get("side", order.side)
        px = dollars_to_cents(f.get(f"{side}_price_fixed")) or dollars_to_cents(f.get(f"{side}_price_dollars"))
        if px is None:
            px = num(f.get(f"{side}_price"))
        cnt = count_field(f, "count") or 0
        if px is None or cnt <= 0:
            return None
        is_taker = bool(f.get("is_taker", True))
        ts = iso_ts(f.get("created_time")) or time.time()
        fee = dollars_to_cents(f.get("fee_cost")) if f.get("fee_cost") is not None else \
            self.fees.fee(px, int(cnt), is_taker, meta, ts)
        self.seen_fill_ids.add(fid)
        return Fill(order_id=order.id, env=self.env, ticker=order.ticker, side=side,
                    action=f.get("action", order.action), count=int(cnt), price=px, fee=fee,
                    is_taker=is_taker, ts=ts, ref_price=order.ref_price, exchange_fill_id=fid)

    def _apply_exchange_order(self, order: Order, o: dict[str, Any], now: float) -> None:
        order.exchange_order_id = o.get("order_id", order.exchange_order_id)
        status = str(o.get("status", "")).lower()
        filled = int(count_field(o, "fill_count") or 0)
        count = int(count_field(o, "initial_count") or order.count)
        exch_fees = (dollars_to_cents(o.get("taker_fees_dollars")) or 0.0) + \
                    (dollars_to_cents(o.get("maker_fees_dollars")) or 0.0)
        if exch_fees:
            order.fees = exch_fees
        if status == "executed" or (filled >= count and count > 0):
            order.status = OrderStatus.FILLED
        elif status == "canceled":
            order.status = OrderStatus.CANCELED if filled else OrderStatus.MISSED
            order.reject_reason = order.reject_reason or ("IOC remainder canceled" if filled else "no fill (IOC)")
        elif status == "resting":
            order.status = OrderStatus.PARTIALLY_FILLED if filled else OrderStatus.RESTING
        elif status == "pending":
            order.status = OrderStatus.SUBMITTED
        if order.status.terminal:
            order.final_ts = now

    async def _collect_fills(self, order: Order, meta: MarketMeta | None) -> list[Fill]:
        if not order.exchange_order_id:
            return []
        out = []
        for f in await self.fills(order_id=order.exchange_order_id):
            fl = self._fill_from(f, order, meta)
            if fl:
                out.append(fl)
        if out:
            prev_cost = (order.avg_fill_price or 0.0) * order.filled
            order.filled += sum(f.count for f in out)
            order.avg_fill_price = (prev_cost + sum(f.price * f.count for f in out)) / order.filled
            order.first_fill_ts = order.first_fill_ts or min(f.ts for f in out)
            if not order.fees:
                order.fees = sum(f.fee for f in out)
        return out

    async def submit(self, order: Order, meta: MarketMeta, now: float) -> list[ExecutionEvent]:
        body = build_order_payload(order, meta.tick_size, self.cfg)
        order.payload = body
        errs = validate_payload(body)
        if errs:
            order.status, order.reject_reason, order.final_ts = OrderStatus.REJECTED, "; ".join(errs), now
            return [ExecutionEvent(order, note=order.reject_reason)]
        order.status = OrderStatus.SUBMITTED
        order.submitted_ts = time.time()
        try:
            resp = await self.client.request("POST", "/portfolio/orders", json=body)
        except ExchangeHTTPError as e:
            if e.status == 409:  # duplicate client_order_id: an order with this id exists -> resolve it
                order.status = OrderStatus.UNKNOWN
                order.reject_reason = "duplicate client_order_id reported by exchange; resolving"
            else:
                order.status = OrderStatus.REJECTED
                order.reject_reason = f"exchange rejected: HTTP {e.status} {e.body[:200]}"
                order.final_ts = time.time()
            return [ExecutionEvent(order, note=order.reject_reason)]
        except (DataUnavailable, Exception) as e:  # noqa: BLE001 - outcome unknown: FAIL CLOSED
            order.status = OrderStatus.UNKNOWN
            order.reject_reason = f"submit outcome unknown: {e}"
            self.working[order.id] = (order, meta)
            return [ExecutionEvent(order, note=order.reject_reason)]
        order.ack_ts = time.time()
        o = resp.get("order") or {}
        self._apply_exchange_order(order, o, order.ack_ts)
        fills = await self._collect_fills(order, meta)
        if not order.status.terminal:
            self.working[order.id] = (order, meta)
        return [ExecutionEvent(order, fills)]

    async def resolve_unknown(self, order: Order, meta: MarketMeta | None, now: float) -> ExecutionEvent:
        """Find an order whose submit outcome is unknown by its client_order_id."""
        since = (order.submitted_ts or order.created_ts) - 60
        found = None
        for o in await self.orders(ticker=order.ticker, min_ts=since):
            if o.get("client_order_id") == order.client_order_id:
                found = o
                break
        if found is None:
            if now - (order.submitted_ts or order.created_ts) > UNKNOWN_GRACE_SECONDS:
                order.status = OrderStatus.CANCELED
                order.reject_reason = "order never reached the exchange (not found by client_order_id)"
                order.final_ts = now
                self.working.pop(order.id, None)
            return ExecutionEvent(order, note=order.reject_reason)
        self._apply_exchange_order(order, found, now)
        fills = await self._collect_fills(order, meta)
        if order.status.terminal:
            self.working.pop(order.id, None)
        return ExecutionEvent(order, fills, "resolved")

    async def on_market(self, ticker, snap, book, trades, now, meta=None) -> list[ExecutionEvent]:
        events = []
        for oid, (order, m) in list(self.working.items()):
            if order.ticker != ticker:
                continue
            if order.status is OrderStatus.UNKNOWN:
                events.append(await self.resolve_unknown(order, m, now))
                continue
            if now - self.last_poll.get(oid, 0) < 2.0:
                continue
            self.last_poll[oid] = now
            if order.expires_ts and now >= order.expires_ts:
                events.append(await self.cancel(order, now))
                continue
            data = await self.client.get(f"/portfolio/orders/{order.exchange_order_id}")
            self._apply_exchange_order(order, data.get("order") or {}, now)
            fills = await self._collect_fills(order, m)
            if fills or order.status.terminal:
                events.append(ExecutionEvent(order, fills))
            if order.status.terminal:
                self.working.pop(oid, None)
        return events

    async def cancel(self, order: Order, now: float) -> ExecutionEvent:
        if not order.exchange_order_id:
            return ExecutionEvent(order, note="no exchange id")
        try:
            data = await self.client.request("DELETE", f"/portfolio/orders/{order.exchange_order_id}")
            self._apply_exchange_order(order, data.get("order") or {"status": "canceled"}, now)
        except ExchangeHTTPError as e:
            log.warning("cancel failed for %s: %s", order.id, e)
        fills = await self._collect_fills(order, self.working.get(order.id, (None, None))[1])
        if order.status.terminal:
            self.working.pop(order.id, None)
        return ExecutionEvent(order, fills, "cancel requested")
