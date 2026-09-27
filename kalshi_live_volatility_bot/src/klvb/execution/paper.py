"""PAPER and SHADOW execution: live data, simulated fills. No real orders.

Fill model (deliberately conservative; see docs/ARCHITECTURE.md):

TAKER orders (limit + immediate-or-cancel)
  * modeled latency = base +- jitter; the order is matched against the FIRST
    market observation at or after (submit + latency), never the one that
    produced the signal, so price movement between signal and execution is real.
  * walks the displayed book on the opposite side up to the limit price, taking
    at most `depth_participation` of each level (others compete for it).
  * if only top-of-book is known, only that level is available; unknown size
    uses `assumed_depth_when_unknown`.
  * outcomes: FULL FILL, PARTIAL FILL (remainder canceled), NO FILL (= MISSED
    trade, recorded with the reason), REJECTED (invalid/closed market or the
    configured random exchange-reject rate), DELAYED FILL (fill ts = match time).
  * every fill pays the Kalshi taker fee, computed per level and rounded up.

MAKER orders (post-only resting limit)
  * rejected if marketable at arrival (post-only would be refused).
  * fills ONLY from real trade prints after arrival: prints strictly through
    our price fill up to their size; prints AT our price credit only
    `maker_queue_fraction` of their size (we are not first in the queue).
  * canceled after `maker_order_ttl_seconds`.

SHADOW differs from PAPER in that, at submit time, it builds and validates the
exact Kalshi REST payload used by LIVE, fetches a *fresh* order book over the
network, and uses the measured round trip as the latency.
"""
from __future__ import annotations

import logging
import math
import random
import time

from ..engine.fees import FeeModel
from ..models import BookLevel, Fill, MarketMeta, MarketSnapshot, Order, OrderBook, OrderStatus, TradePrint
from .base import ExecutionClient, ExecutionEvent, build_order_payload, validate_payload

log = logging.getLogger("klvb.exec.paper")

BOOK_SNAPSHOT_TOLERANCE = 6.0  # seconds; a depth book older than the quote by more is not trusted


class PaperExecution(ExecutionClient):
    env = "PAPER"
    live = False

    def __init__(self, paper_cfg, fees: FeeModel, seed: int = 1337):
        self.cfg = paper_cfg
        self.fees = fees
        self.rng = random.Random(seed)
        self.pending: dict[str, tuple[Order, float, MarketMeta]] = {}   # taker: order, match_after_ts, meta
        self.resting: dict[str, tuple[Order, float, MarketMeta]] = {}   # maker: order, active_from_ts, meta
        self.last_snap: dict[str, MarketSnapshot] = {}

    # ---------------------------------------------------------------------
    def working_orders(self) -> list[Order]:
        return [o for o, _, _ in self.pending.values()] + [o for o, _, _ in self.resting.values()]

    def _latency(self) -> float:
        j = self.cfg.latency_jitter_ms
        ms = max(20.0, self.cfg.base_latency_ms + self.rng.uniform(-j, j))
        return ms / 1000.0

    def _reject(self, order: Order, now: float, why: str) -> list[ExecutionEvent]:
        order.status = OrderStatus.REJECTED
        order.reject_reason = why
        order.final_ts = now
        return [ExecutionEvent(order, note=why)]

    async def submit(self, order: Order, meta: MarketMeta, now: float) -> list[ExecutionEvent]:
        order.submitted_ts = now
        order.payload = build_order_payload(order, meta.tick_size)
        errs = validate_payload(order.payload)
        if errs:
            return self._reject(order, now, "invalid order: " + "; ".join(errs))
        snap = self.last_snap.get(order.ticker)
        if snap is not None and snap.status not in ("active", "open", ""):
            return self._reject(order, now, f"market not open ({snap.status})")
        if self.rng.random() < self.cfg.reject_probability:
            return self._reject(order, now, "simulated exchange reject")
        lat = self._latency()
        order.ack_ts = now + lat
        if order.style == "maker":
            ask = snap.ask_for(order.side) if snap else None
            bid = snap.bid_for(order.side) if snap else None
            if order.action == "buy" and ask is not None and order.limit_price >= ask:
                return self._reject(order, now, "post-only order would cross the spread")
            if order.action == "sell" and bid is not None and order.limit_price <= bid:
                return self._reject(order, now, "post-only order would cross the spread")
            order.status = OrderStatus.RESTING
            order.expires_ts = order.expires_ts or now + self.cfg.maker_order_ttl_seconds
            self.resting[order.id] = (order, now + lat, meta)
        else:
            order.status = OrderStatus.SUBMITTED
            self.pending[order.id] = (order, now + lat, meta)
        return [ExecutionEvent(order)]

    async def cancel(self, order: Order, now: float) -> ExecutionEvent:
        self.pending.pop(order.id, None)
        self.resting.pop(order.id, None)
        if not order.status.terminal:
            order.status = OrderStatus.CANCELED
            order.final_ts = now
        return ExecutionEvent(order, note="canceled")

    # ---------------------------------------------------------------------
    def _levels(self, order: Order, snap: MarketSnapshot, book: OrderBook | None) -> list[BookLevel]:
        side, buy = order.side, order.action == "buy"
        top_price = snap.ask_for(side) if buy else snap.bid_for(side)
        if top_price is None:
            return []
        if book is not None and abs(book.ts - snap.ts) <= BOOK_SNAPSHOT_TOLERANCE:
            levels = book.asks_for(side) if buy else book.bids_for(side)
            if levels and abs(levels[0].price - top_price) < 1e-6:
                return levels
        size = snap.ask_size_for(side) if buy else snap.bid_size_for(side)
        if size is None or size <= 0:
            size = self.cfg.assumed_depth_when_unknown
        return [BookLevel(top_price, size)]

    def match_taker(self, order: Order, snap: MarketSnapshot, book: OrderBook | None, match_ts: float,
                    meta: MarketMeta) -> ExecutionEvent:
        buy = order.action == "buy"
        levels = self._levels(order, snap, book)
        fills: list[Fill] = []
        remaining = order.remaining
        for lv in levels:
            if remaining <= 0:
                break
            if (buy and lv.price > order.limit_price + 1e-9) or (not buy and lv.price < order.limit_price - 1e-9):
                break
            avail = int(math.floor(lv.size * self.cfg.depth_participation))
            take = min(remaining, avail)
            if take <= 0:
                continue
            fee = self.fees.fee(lv.price, take, True, meta, match_ts)
            fills.append(Fill(order_id=order.id, env=self.env, ticker=order.ticker, side=order.side,
                              action=order.action, count=take, price=lv.price, fee=fee, is_taker=True,
                              ts=match_ts, ref_price=order.ref_price))
            remaining -= take
        self._apply(order, fills, match_ts)
        if order.filled == 0:
            best = levels[0].price if levels else None
            order.status = OrderStatus.MISSED
            if best is None:
                order.reject_reason = "no liquidity on the opposite side"
            elif (buy and best > order.limit_price) or (not buy and best < order.limit_price):
                order.reject_reason = f"price moved to {best:.1f}c beyond limit {order.limit_price:.1f}c before arrival"
            else:
                order.reject_reason = "insufficient displayed depth within limit"
            order.final_ts = match_ts
            return ExecutionEvent(order, [], order.reject_reason)
        order.status = OrderStatus.FILLED if order.remaining == 0 else OrderStatus.CANCELED
        if order.remaining:
            order.reject_reason = f"partial fill {order.filled}/{order.count}; IOC remainder canceled"
        order.final_ts = match_ts
        return ExecutionEvent(order, fills, order.reject_reason)

    def _apply(self, order: Order, fills: list[Fill], ts: float) -> None:
        if not fills:
            return
        prev_cost = (order.avg_fill_price or 0.0) * order.filled
        n = sum(f.count for f in fills)
        order.filled += n
        order.avg_fill_price = (prev_cost + sum(f.price * f.count for f in fills)) / order.filled
        order.fees += sum(f.fee for f in fills)
        order.first_fill_ts = order.first_fill_ts or ts

    def _match_maker(self, order: Order, active_from: float, trades: list[TradePrint], now: float,
                     meta: MarketMeta, snap: MarketSnapshot | None = None) -> ExecutionEvent | None:
        fills: list[Fill] = []
        # Rule 1: the displayed book crossed our resting price after we arrived. A bid at/above our
        # resting offer (or an ask at/below our resting bid) cannot coexist with it, so the counterparty
        # would have traded with us: fill up to the size displayed at that price.
        if snap is not None and snap.ts >= active_from and order.remaining > 0:
            if order.action == "sell":
                px, size = snap.bid_for(order.side), snap.bid_size_for(order.side)
                crossed = px is not None and px >= order.limit_price - 1e-9
            else:
                px, size = snap.ask_for(order.side), snap.ask_size_for(order.side)
                crossed = px is not None and px <= order.limit_price + 1e-9
            if crossed:
                size = size if size and size > 0 else self.cfg.assumed_depth_when_unknown
                take = min(order.remaining, int(math.floor(size * self.cfg.depth_participation)))
                if take > 0:
                    fee = self.fees.fee(order.limit_price, take, False, meta, snap.ts)
                    fl = Fill(order_id=order.id, env=self.env, ticker=order.ticker, side=order.side,
                              action=order.action, count=take, price=order.limit_price, fee=fee,
                              is_taker=False, ts=snap.ts, ref_price=order.ref_price)
                    fills.append(fl)
                    self._apply(order, [fl], snap.ts)
        # Rule 2: trade prints through (full size) or at (queue share) our price.
        for t in trades:
            if t.ts < active_from or order.remaining <= 0:
                continue
            px = t.yes_price if order.side == "yes" else 100 - t.yes_price
            through = px < order.limit_price - 1e-9 if order.action == "buy" else px > order.limit_price + 1e-9
            at = abs(px - order.limit_price) < 1e-9
            if not (through or at):
                continue
            credit = t.count if through else t.count * self.cfg.maker_queue_fraction
            take = min(order.remaining, int(math.floor(credit)))
            if take <= 0:
                continue
            fee = self.fees.fee(order.limit_price, take, False, meta, t.ts)
            fills.append(Fill(order_id=order.id, env=self.env, ticker=order.ticker, side=order.side,
                              action=order.action, count=take, price=order.limit_price, fee=fee,
                              is_taker=False, ts=t.ts, ref_price=order.ref_price))
            self._apply(order, [fills[-1]], t.ts)
        if order.remaining == 0:
            order.status = OrderStatus.FILLED
            order.final_ts = now
        elif order.filled > 0:
            order.status = OrderStatus.PARTIALLY_FILLED
        if order.expires_ts and now >= order.expires_ts and not order.status.terminal:
            order.status = OrderStatus.CANCELED if order.filled else OrderStatus.MISSED
            order.reject_reason = "maker order expired" + (f" after partial fill {order.filled}/{order.count}"
                                                           if order.filled else " unfilled")
            order.final_ts = now
        if fills or order.status.terminal:
            return ExecutionEvent(order, fills, order.reject_reason)
        return None

    async def on_market(self, ticker, snap, book, trades, now, meta=None) -> list[ExecutionEvent]:
        if snap is not None:
            self.last_snap[ticker] = snap
        events: list[ExecutionEvent] = []
        for oid, (order, match_after, m) in list(self.pending.items()):
            if order.ticker != ticker or snap is None:
                continue
            obs_ts = max(snap.ts, book.ts if book is not None else 0.0)
            if obs_ts < match_after:
                continue
            if snap.status not in ("active", "open", ""):
                self.pending.pop(oid)
                events += self._reject(order, now, f"market not open ({snap.status})")
                continue
            self.pending.pop(oid)
            events.append(self.match_taker(order, snap, book, max(match_after, snap.ts), m))
        for oid, (order, active_from, m) in list(self.resting.items()):
            if order.ticker != ticker:
                continue
            ev = self._match_maker(order, active_from, trades, now, m, snap)
            if ev is not None:
                events.append(ev)
            if order.status.terminal:
                self.resting.pop(oid, None)
        return events


class ShadowExecution(PaperExecution):
    """Production-like simulation: exact LIVE payload, fresh book, measured network latency."""
    env = "SHADOW"

    def __init__(self, paper_cfg, fees: FeeModel, data_provider, seed: int = 1337, live_cfg=None):
        super().__init__(paper_cfg, fees, seed)
        self.data = data_provider
        self.live_cfg = live_cfg

    async def submit(self, order: Order, meta: MarketMeta, now: float) -> list[ExecutionEvent]:
        order.submitted_ts = now
        order.payload = build_order_payload(order, meta.tick_size, self.live_cfg)
        errs = validate_payload(order.payload)
        if errs:
            return self._reject(order, now, "invalid order (would be rejected by Kalshi): " + "; ".join(errs))
        if order.style == "maker":
            return await super().submit(order, meta, now)
        snap = self.last_snap.get(order.ticker)
        t0 = time.time()
        try:
            book = await self.data.orderbook(order.ticker)
        except Exception as e:  # noqa: BLE001 - an unreachable exchange means the order would have failed
            return self._reject(order, now, f"exchange unreachable at submit: {e}")
        rtt = time.time() - t0
        order.ack_ts = now + rtt
        if book is None or snap is None:
            return self._reject(order, now, "no book at submit")
        # rebuild the quote from the fresh book so the match uses what the exchange would have had
        yb, ya = book.best_yes_bid(), book.best_yes_ask()
        fresh = MarketSnapshot(ticker=order.ticker, ts=book.ts, yes_bid=yb.price if yb else None,
                               yes_ask=ya.price if ya else None, yes_bid_size=yb.size if yb else None,
                               yes_ask_size=ya.size if ya else None, status=snap.status, source="shadow_book")
        order.status = OrderStatus.SUBMITTED
        return [self.match_taker(order, fresh, book, now + rtt, meta)]
