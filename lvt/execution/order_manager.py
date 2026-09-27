"""Order manager: the only code that talks to a venue.

Duplicate-order protection (section 38):
  1. every order gets a fresh client_order_id, written to the DB as
     PENDING_SUBMIT BEFORE the venue is called;
  2. at most one working order per market;
  3. a client_order_id is never re-sent;
  4. an exception during submit leaves the order UNKNOWN, which puts the
     whole engine into fail-closed until reconciliation resolves it.
"""
from __future__ import annotations

import logging
from typing import Callable

from lvt.db import Database
from lvt.execution.base import Venue
from lvt.models import TERMINAL_ORDER_STATES, OrderRequest, OrderUpdate, Snapshot

log = logging.getLogger("lvt.orders")


class OrderManager:
    def __init__(self, db: Database, venue: Venue, env: str, run_id: str,
                 on_update: Callable[[OrderRequest, OrderUpdate], None]):
        self.db = db
        self.venue = venue
        self.env = env
        self.run_id = run_id
        self.on_update = on_update
        self.working: dict[str, OrderRequest] = {}
        self.unknown: dict[str, OrderRequest] = {}

    def has_working(self, ticker: str) -> bool:
        return any(r.ticker == ticker for r in list(self.working.values()) + list(self.unknown.values()))

    def submit(self, req: OrderRequest, now: float) -> OrderUpdate:
        if self.has_working(req.ticker):
            return OrderUpdate(req.client_order_id, "REJECTED", now, reason="DUPLICATE_ORDER_RISK: market has a working order")
        if self.db.scalar("SELECT 1 FROM orders WHERE client_order_id=?", (req.client_order_id,)):
            return OrderUpdate(req.client_order_id, "REJECTED", now, reason="DUPLICATE_ORDER_RISK: client_order_id reused")
        self.db.insert("orders", {
            "client_order_id": req.client_order_id, "run_id": self.run_id, "env": self.env,
            "venue": self.venue.name, "ticker": req.ticker, "event_ticker": req.event_ticker,
            "strategy": req.strategy, "action": req.action, "side": req.side, "count": req.count,
            "limit_price": req.limit_price, "purpose": req.purpose, "position_id": req.position_id,
            "signal_id": req.signal_id, "exit_reason": req.exit_reason, "status": "PENDING_SUBMIT",
            "ref_price": req.ref_price, "event_ts": req.event_ts or None, "data_ts": req.data_ts,
            "signal_ts": req.signal_ts, "created_ts": req.created_ts,
        })
        try:
            up = self.venue.submit(req, now)
        except Exception as exc:                          # noqa: BLE001
            log.error("order %s state UNKNOWN after submit error: %s", req.client_order_id, exc)
            self.unknown[req.client_order_id] = req
            self.db.update("orders", "client_order_id", req.client_order_id,
                           {"status": "UNKNOWN", "submitted_ts": now, "reject_reason": str(exc)[:300]})
            return OrderUpdate(req.client_order_id, "UNKNOWN", now, reason=str(exc)[:300])
        payload = getattr(self.venue, "payload_json", lambda c: "")(req.client_order_id)
        self.db.update("orders", "client_order_id", req.client_order_id,
                       {"status": up.status if up.status != "ACKED" else "SUBMITTED", "submitted_ts": now,
                        "ack_ts": now if up.status == "ACKED" else None,
                        "exchange_order_id": up.exchange_order_id, "payload": payload or None,
                        "reject_reason": up.reason or None})
        if up.status in TERMINAL_ORDER_STATES:
            self._apply(req, up)
        else:
            self.working[req.client_order_id] = req
        return up

    def process(self, snaps: dict[str, Snapshot], now: float) -> list[OrderUpdate]:
        ups = self.venue.on_market_data(snaps, now)
        for up in ups:
            req = self.working.get(up.client_order_id)
            if req is None:
                continue
            if up.status == "UNKNOWN":
                self.unknown[up.client_order_id] = self.working.pop(up.client_order_id)
                self.db.update("orders", "client_order_id", up.client_order_id,
                               {"status": "UNKNOWN", "reject_reason": up.reason})
                continue
            if up.status in TERMINAL_ORDER_STATES:
                self.working.pop(up.client_order_id, None)
                self._apply(req, up)
        return ups

    def _apply(self, req: OrderRequest, up: OrderUpdate) -> None:
        for f in up.fills:
            self.db.insert("fills", {"client_order_id": req.client_order_id, "run_id": self.run_id,
                                     "env": self.env, "ts": f.ts, "ticker": req.ticker, "side": req.side,
                                     "action": req.action, "price": f.price, "count": f.count,
                                     "fee_usd": f.fee_usd, "exchange_fill_id": f.exchange_fill_id})
        n = sum(f.count for f in up.fills)
        avg = sum(f.price * f.count for f in up.fills) / n if n else None
        fees = round(sum(f.fee_usd for f in up.fills), 4)
        slip = None
        if avg is not None:
            slip = (avg - req.ref_price) if req.action == "buy" else (req.ref_price - avg)
        self.db.update("orders", "client_order_id", req.client_order_id, {
            "status": up.status, "filled_count": n, "avg_fill_price": avg, "fees_usd": fees,
            "slippage_cents": slip, "final_ts": up.ts,
            "arrival_ts": up.fills[0].ts if up.fills else up.ts,
            "reject_reason": up.reason or None,
        })
        self.on_update(req, up)

    def cancel_all(self, now: float) -> None:
        for coid, req in list(self.working.items()):
            try:
                up = self.venue.cancel(coid, now)
            except Exception as exc:                      # noqa: BLE001
                log.error("cancel %s failed: %s", coid, exc)
                continue
            self.working.pop(coid, None)
            self._apply(req, up)

    def resolve_unknown(self, now: float) -> list[str]:
        """Ask the venue about UNKNOWN orders. Returns ids still unresolved."""
        still = []
        for coid, req in list(self.unknown.items()):
            try:
                up = self.venue.lookup(coid)
            except Exception as exc:                      # noqa: BLE001
                log.warning("lookup %s failed: %s", coid, exc)
                still.append(coid)
                continue
            if up is None:
                # The venue never saw it: safe to treat as not placed.
                up = OrderUpdate(coid, "REJECTED", now, reason="not found at venue after UNKNOWN")
            if up.status in TERMINAL_ORDER_STATES:
                self.unknown.pop(coid)
                self._apply(req, up)
            else:
                self.unknown.pop(coid)
                self.working[coid] = req
        return still

    def restore(self) -> None:
        """After a restart: any order not terminal in the DB is UNKNOWN until
        the venue confirms it."""
        rows = self.db.query("SELECT * FROM orders WHERE env=? AND status IN "
                             "('PENDING_SUBMIT','SUBMITTED','UNKNOWN')", (self.env,))
        for r in rows:
            self.unknown[r["client_order_id"]] = OrderRequest(
                client_order_id=r["client_order_id"], ticker=r["ticker"], event_ticker=r["event_ticker"] or "",
                action=r["action"], side=r["side"], count=r["count"], limit_price=r["limit_price"],
                purpose=r["purpose"], ref_price=r["ref_price"] or 0.0, created_ts=r["created_ts"] or 0.0,
                position_id=r["position_id"] or "", signal_id=r["signal_id"] or "",
                exit_reason=r["exit_reason"] or "", strategy=r["strategy"] or "")
