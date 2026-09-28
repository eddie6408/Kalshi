"""Simulated venue for PAPER and SHADOW.

The venue keeps its OWN ledger of positions, built only from its fills, so
reconciliation compares two independently maintained records (engine
position book vs venue), just as LIVE compares the book with Kalshi.
After a restart the ledger is rebuilt from the fills table - the paper
equivalent of asking the exchange.

Two order kinds:
  * taker IOC   - resolved once, on the first snapshot after latency
                  (fill_model.py);
  * maker GTC   - post-only, rests and fills from trade prints / touch-through
                  (maker_model.py) until filled, cancelled or expired.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict

from lvt.db import Database
from lvt.execution import fill_model, maker_model
from lvt.execution.base import Venue
from lvt.execution.encoding import encode_order
from lvt.models import MarketInfo, OrderRequest, OrderUpdate, Snapshot, Trade

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
        self.pending: dict[str, tuple[OrderRequest, float]] = {}   # taker: coid -> (req, arrival_ts)
        self.resting: dict[str, maker_model.RestingOrder] = {}     # maker orders
        self.done: dict[str, OrderUpdate] = {}
        self.ledger: dict[str, tuple[str, int]] = {}
        self.payloads: dict[str, dict] = {}
        self.trade_buf: dict[str, list[Trade]] = defaultdict(list)
        self.feed_tickers: set[str] = set()                        # markets covered by the trade feed

    @property
    def volume_attribution(self) -> float:
        m = self.cfg.data.get("maker", {})
        return float(m.get("volume_attribution", 0.5))

    def restore(self, db: Database) -> None:
        rows = db.query("SELECT ticker, side, action, SUM(count) n FROM fills WHERE env=? "
                        "GROUP BY ticker, side, action", (self.env,))
        net: dict[tuple[str, str], int] = {}
        for r in rows:
            k = (r["ticker"], r["side"])
            # 'sell' and 'settle' (market settled while held) both close contracts
            net[k] = net.get(k, 0) + (r["n"] if r["action"] == "buy" else -r["n"])
        self.ledger = {t: (s, n) for (t, s), n in net.items() if n > 0}

    # ---------------------------------------------------------------- feed
    def on_trades(self, trades: list[Trade], covered: set[str] | None = None) -> None:
        """Public trade prints since the last call. `covered` = tickers the
        feed is known to be complete for (all of them, when it is the
        exchange-wide feed)."""
        if covered is not None:
            self.feed_tickers = set(covered)
        for t in trades:
            self.trade_buf[t.ticker].append(t)

    # -------------------------------------------------------------- orders
    def submit(self, req: OrderRequest, now: float) -> OrderUpdate:
        coid = req.client_order_id
        if coid in self.pending or coid in self.resting or coid in self.done:
            return OrderUpdate(coid, "REJECTED", now, reason="duplicate client_order_id")
        if self.shadow:
            # Exactly what LIVE would POST; encode_order validates it too.
            self.payloads[coid] = encode_order(req)
        arrival = now + fill_model.latency_s(coid, self.xcfg.latency_ms, self.xcfg.latency_jitter_ms)
        if req.post_only:
            self.resting[coid] = maker_model.RestingOrder(req, arrival)
        else:
            self.pending[coid] = (req, arrival)
        return OrderUpdate(coid, "ACKED", now, exchange_order_id=f"{self.name}-{coid}")

    def on_market_data(self, snaps: dict[str, Snapshot], now: float) -> list[OrderUpdate]:
        out: list[OrderUpdate] = []
        depth_age = self.cfg.discovery.depth_max_age_s
        for coid, (req, arrival) in list(self.pending.items()):
            snap = snaps.get(req.ticker)
            if snap is not None and snap.ts >= arrival and now >= arrival:
                info = self.markets.get(req.ticker) or MarketInfo(req.ticker)
                up = fill_model.resolve(req, snap, info, snap.ts, self.xcfg, depth_age)
            elif now - arrival > self.xcfg.order_ttl_s:
                up = OrderUpdate(coid, "NO_FILL", now, reason="no market data after order arrival")
            else:
                continue
            del self.pending[coid]
            self._finish(req, up, out)

        for coid, ro in list(self.resting.items()):
            req = ro.req
            snap = snaps.get(req.ticker)
            if not ro.active:
                if snap is None or snap.ts < ro.arrival_ts:
                    if now - ro.arrival_ts > self.xcfg.order_ttl_s:
                        del self.resting[coid]
                        self._finish(req, OrderUpdate(coid, "NO_FILL", now, reason="no market data after arrival"), out)
                    continue
                rej = maker_model.activate(ro, snap, depth_age)
                if rej is not None:
                    del self.resting[coid]
                    self._finish(req, rej, out)
                    continue
            trades = [t for t in self.trade_buf.get(req.ticker, []) if t.ts >= ro.arrival_ts]
            info = self.markets.get(req.ticker) or MarketInfo(req.ticker)
            fills = maker_model.advance(ro, snap, trades, info, req.ticker in self.feed_tickers,
                                        self.volume_attribution)
            if ro.remaining <= 0:
                del self.resting[coid]
                self._finish(req, OrderUpdate(coid, "FILLED", now, fills=fills), out)
            elif req.expire_ts and now >= req.expire_ts:
                del self.resting[coid]
                status = "PARTIAL" if ro.filled else "NO_FILL"
                self._finish(req, OrderUpdate(coid, status, now, fills=fills,
                                              reason="maker order expired" + ("" if ro.filled else " unfilled")), out)
            elif fills:
                self._ledger(req, fills)
                out.append(OrderUpdate(coid, "PARTIALLY_FILLED", now, fills=fills))
        self.trade_buf.clear()
        return out

    def _ledger(self, req: OrderRequest, fills) -> None:
        for f in fills:
            side, n = self.ledger.get(req.ticker, (req.side, 0))
            n = n + f.count if req.action == "buy" else n - f.count
            if n > 0:
                self.ledger[req.ticker] = (req.side, n)
            else:
                self.ledger.pop(req.ticker, None)

    def _finish(self, req: OrderRequest, up: OrderUpdate, out: list) -> None:
        self.done[req.client_order_id] = up
        self._ledger(req, up.fills)
        out.append(up)

    def on_settlement(self, ticker: str) -> None:
        """The market settled: the exchange closes every position in it."""
        self.ledger.pop(ticker, None)

    def cancel(self, client_order_id: str, now: float) -> OrderUpdate:
        if client_order_id in self.pending:
            del self.pending[client_order_id]
        elif client_order_id in self.resting:
            ro = self.resting.pop(client_order_id)
            up = OrderUpdate(client_order_id, "CANCELLED", now,
                             reason=f"cancelled after {ro.filled} filled")
            self.done[client_order_id] = up
            return up
        else:
            return self.done.get(client_order_id) or OrderUpdate(client_order_id, "REJECTED", now,
                                                                 reason="unknown order")
        up = OrderUpdate(client_order_id, "CANCELLED", now, reason="cancelled")
        self.done[client_order_id] = up
        return up

    def lookup(self, client_order_id: str) -> OrderUpdate | None:
        if client_order_id in self.pending or client_order_id in self.resting:
            return OrderUpdate(client_order_id, "ACKED", 0.0)
        return self.done.get(client_order_id)

    def positions(self) -> dict[str, tuple[str, int]]:
        return dict(self.ledger)

    def working_orders(self) -> list[str]:
        return list(self.pending) + list(self.resting)

    def payload_json(self, coid: str) -> str:
        p = self.payloads.get(coid)
        return json.dumps(p) if p else ""
