"""Simulated venue for PAPER and SHADOW.

The venue keeps its OWN ledger of positions, built only from its fills, so
reconciliation compares two independently maintained records (engine
position book vs venue), just as LIVE compares the book with Kalshi.
After a restart the ledger is rebuilt from the fills table - the paper
equivalent of asking the exchange.
"""
from __future__ import annotations

import json
import logging

from lvt.db import Database
from lvt.execution import fill_model
from lvt.execution.base import Venue
from lvt.execution.encoding import encode_order
from lvt.models import MarketInfo, OrderRequest, OrderUpdate, Snapshot

log = logging.getLogger("lvt.paper")


class PaperVenue(Venue):
    is_real = False

    def __init__(self, cfg, env: str, markets: dict[str, MarketInfo], shadow: bool = False):
        self.cfg = cfg
        self.xcfg = cfg.execution
        self.env = env
        self.name = "shadow" if shadow else "paper"
        self.shadow = shadow
        self.markets = markets
        self.pending: dict[str, tuple[OrderRequest, float]] = {}   # coid -> (req, arrival_ts)
        self.done: dict[str, OrderUpdate] = {}
        self.ledger: dict[str, tuple[str, int]] = {}
        self.payloads: dict[str, dict] = {}

    def restore(self, db: Database) -> None:
        rows = db.query("SELECT ticker, side, action, SUM(count) n FROM fills WHERE env=? "
                        "GROUP BY ticker, side, action", (self.env,))
        net: dict[tuple[str, str], int] = {}
        for r in rows:
            k = (r["ticker"], r["side"])
            net[k] = net.get(k, 0) + (r["n"] if r["action"] == "buy" else -r["n"])
        self.ledger = {t: (s, n) for (t, s), n in net.items() if n > 0}

    def submit(self, req: OrderRequest, now: float) -> OrderUpdate:
        if req.client_order_id in self.pending or req.client_order_id in self.done:
            return OrderUpdate(req.client_order_id, "REJECTED", now, reason="duplicate client_order_id")
        if self.shadow:
            # Exactly what LIVE would POST; encode_order validates it too.
            self.payloads[req.client_order_id] = encode_order(req)
        arrival = now + fill_model.latency_s(req.client_order_id, self.xcfg.latency_ms, self.xcfg.latency_jitter_ms)
        self.pending[req.client_order_id] = (req, arrival)
        return OrderUpdate(req.client_order_id, "ACKED", now, exchange_order_id=f"{self.name}-{req.client_order_id}")

    def on_market_data(self, snaps: dict[str, Snapshot], now: float) -> list[OrderUpdate]:
        out: list[OrderUpdate] = []
        for coid, (req, arrival) in list(self.pending.items()):
            snap = snaps.get(req.ticker)
            if snap is not None and snap.ts >= arrival and now >= arrival:
                info = self.markets.get(req.ticker) or MarketInfo(req.ticker)
                up = fill_model.resolve(req, snap, info, snap.ts, self.xcfg,
                                        self.cfg.discovery.depth_max_age_s)
            elif now - arrival > self.xcfg.order_ttl_s:
                up = OrderUpdate(coid, "NO_FILL", now, reason="no market data after order arrival")
            else:
                continue
            del self.pending[coid]
            self.done[coid] = up
            for f in up.fills:
                side, n = self.ledger.get(req.ticker, (req.side, 0))
                n = n + f.count if req.action == "buy" else n - f.count
                if n > 0:
                    self.ledger[req.ticker] = (req.side, n)
                else:
                    self.ledger.pop(req.ticker, None)
            out.append(up)
        return out

    def cancel(self, client_order_id: str, now: float) -> OrderUpdate:
        if client_order_id in self.pending:
            del self.pending[client_order_id]
            up = OrderUpdate(client_order_id, "CANCELLED", now, reason="cancelled")
            self.done[client_order_id] = up
            return up
        return self.done.get(client_order_id) or OrderUpdate(client_order_id, "REJECTED", now,
                                                             reason="unknown order")

    def lookup(self, client_order_id: str) -> OrderUpdate | None:
        if client_order_id in self.pending:
            return OrderUpdate(client_order_id, "ACKED", 0.0)
        return self.done.get(client_order_id)

    def positions(self) -> dict[str, tuple[str, int]]:
        return dict(self.ledger)

    def working_orders(self) -> list[str]:
        return list(self.pending)

    def payload_json(self, coid: str) -> str:
        p = self.payloads.get(coid)
        return json.dumps(p) if p else ""
