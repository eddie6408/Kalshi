"""Maker orders, stage 1 (Package C2 M1 + M2): forecast entries REST on the
book instead of crossing the spread. DEMO only.

Why: the M0 shadow simulation (12,677 simulated orders on the production
tape, 28 Sep - 10 Oct) measured a resting order at our price at +1.58c per
filled contract against -2.31c for crossing at the ask - about 3.9c a
contract in favour of waiting, with no sign of adverse selection.

What rests, what does not (docs/packages/C2-maker-orders.md 3.1):
  * single-leg forecast BUYS rest here when `execution.maker.enabled`;
  * arbitrage legs never rest (a resting basket can half-fill unhedged);
  * exits are unchanged (stage 1 is entries only).

Price (buying side S, cents):
    min( best_bid_S + improve,                          join/improve the bid
         floor(fair_S - min_edge - maker_fee),          never pay away the edge
         best_ask_S - 1 )                               never cross
sent `post_only`, so the exchange itself refuses anything that would cross.

Lifecycle, polled every few seconds (get_order):
  fills are journalled as they arrive (an executions row and a leg_orders row
  per new fill, exactly what a taker fill writes - positions, P&L and the
  position manager need no change); the order is cancelled when it has
  rested `max_rest_seconds`, when its market closes within
  `close_within_minutes`, or when the engine may not open positions (breaker,
  operator pause). On start-up every order this bot owns is cancelled first,
  so a crash never leaves one working.

Exposure: a resting order counts at its full size against the per-market,
per-event and total limits from the moment it is placed - it can fill at
any time.
"""
from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .clients import price_in_leg_terms
from .execution_manager import (LegExecution, OrderState, TradeOutcome, TradeResult)
from .fees import maker_fee_usd

log = logging.getLogger(__name__)

COID_PREFIX = "kb-mk-"

SCHEMA = """
CREATE TABLE IF NOT EXISTS resting_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT UNIQUE, order_id TEXT, strategy TEXT, event_id TEXT,
    ticker TEXT, side TEXT, count INTEGER, price_cents INTEGER,
    fair_cents REAL, bid_cents INTEGER, ask_cents INTEGER, taker_price_cents INTEGER,
    state TEXT, filled INTEGER DEFAULT 0, avg_fill_cents REAL,
    placed_at TEXT, updated_at TEXT, closed_at TEXT, reason TEXT, opportunity_uuid TEXT
);
CREATE INDEX IF NOT EXISTS idx_resting_state ON resting_orders (state);
"""


@dataclass
class MakerConfig:
    enabled: bool = False
    improve_cents: int = 1
    min_edge_cents: float = 3.0
    max_rest_seconds: float = 900.0
    close_within_minutes: float = 30.0
    poll_seconds: float = 5.0
    max_resting: int = 5
    maker_fee_rate: float = 0.0025

    @classmethod
    def from_dict(cls, d: dict | None) -> "MakerConfig":
        d = d or {}
        return cls(enabled=bool(d.get("enabled", False)),
                   improve_cents=int(d.get("improve_cents", 1)),
                   min_edge_cents=float(d.get("min_edge_cents", 3.0)),
                   max_rest_seconds=float(d.get("max_rest_seconds", 900)),
                   close_within_minutes=float(d.get("close_within_minutes", 30)),
                   poll_seconds=float(d.get("poll_seconds", 5)),
                   max_resting=int(d.get("max_resting", 5)),
                   maker_fee_rate=float(d.get("maker_fee_rate", 0.0025)))


def maker_price(side: str, probability_yes: float, bid: int | None, ask: int | None,
                count: int, cfg: MakerConfig) -> tuple[int | None, str]:
    """(limit price in cents for BUYING `side`, why) - None when there is no
    price that rests without crossing and still keeps `min_edge_cents`."""
    if ask is None:
        return None, "no ask on this side: nothing to rest under"
    fair = 100.0 * (probability_yes if side == "yes" else 1.0 - probability_yes)
    ceiling = ask - 1
    base = bid + cfg.improve_cents if bid is not None else ceiling
    # the edge cap, maker fee included (iterate once: the fee depends on price)
    cap = math.floor(fair - cfg.min_edge_cents)
    fee_c = 100.0 * maker_fee_usd(cap, count, cfg.maker_fee_rate) / max(1, count)
    cap = math.floor(fair - cfg.min_edge_cents - fee_c)
    price = min(base, cap, ceiling)
    if price < 1:
        return None, f"no positive price leaves {cfg.min_edge_cents:g}c of edge (fair {fair:.1f}c)"
    if price >= ask:
        return None, "would cross the ask"
    return int(price), (f"bid {bid}c ask {ask}c fair {fair:.1f}c -> rest at {price}c "
                        f"({'improving the bid' if bid is not None and price > bid else 'at/behind the bid' if bid is not None else 'no bid'})")


@dataclass
class Resting:
    op: object
    leg: object
    client_order_id: str
    order_id: str
    price_cents: int
    count: int
    placed_mono: float
    close_time: datetime | None
    filled: int = 0
    journalled: int = 0
    avg_fill_cents: float = 0.0
    row_id: int | None = None
    notes: list = field(default_factory=list)


class OrderManager:
    def __init__(self, exec_client, portfolio, journal, cfg: MakerConfig,
                 may_open=None, clock=time.monotonic):
        self.exec_client = exec_client
        self.portfolio = portfolio
        self.journal = journal
        self.cfg = cfg
        #: callable(op) -> (ok, why): may the engine still open this position?
        self.may_open = may_open
        self.clock = clock
        self._lock = threading.RLock()
        self._resting: dict[str, Resting] = {}
        self.stats = {"posted": 0, "filled_orders": 0, "contracts_filled": 0,
                      "cancelled": 0, "declined": 0, "errors": 0}
        if journal is not None:
            with journal.lock:
                journal.conn.executescript(SCHEMA)
                journal.conn.commit()

    # ------------------------------------------------------------ queries
    def handles(self, op) -> bool:
        legs = getattr(op, "legs", []) or []
        return (self.cfg.enabled and len(legs) == 1 and not getattr(op, "guaranteed", True)
                and legs[0].action == "buy"
                and str(getattr(op, "strategy_type", "")).startswith("forecast"))

    def resting_cost(self, ticker: str, side: str) -> float:
        with self._lock:
            return sum((r.count - r.filled) * r.price_cents / 100.0 for r in self._resting.values()
                       if r.leg.ticker == ticker and r.leg.side == side)

    def total_resting_cost(self) -> float:
        with self._lock:
            return sum((r.count - r.filled) * r.price_cents / 100.0 for r in self._resting.values())

    def has_resting(self, ticker: str) -> bool:
        with self._lock:
            return any(r.leg.ticker == ticker for r in self._resting.values())

    # ------------------------------------------------------------- post
    def post(self, op) -> TradeResult:
        """Rest a maker order for this single-leg forecast entry. Returns a
        TradeResult: RESTING when posted, ABORTED_PRE_TRADE when not."""
        leg = op.legs[0]
        result = TradeResult(opportunity=op, outcome=TradeOutcome.ABORTED_PRE_TRADE)
        with self._lock:
            n_resting = len(self._resting)
        if n_resting >= self.cfg.max_resting:
            return self._decline(result, f"{n_resting} maker orders already resting "
                                         f"(max {self.cfg.max_resting})")
        if self.has_resting(leg.ticker):
            return self._decline(result, f"a maker order is already resting in {leg.ticker}")
        price, why = maker_price(leg.side, float(op.probability_yes), leg.best_bid_cents,
                                 leg.best_ask_cents or leg.limit_price_cents, leg.quantity, self.cfg)
        if price is None:
            return self._decline(result, why)
        coid = f"{COID_PREFIX}{uuid.uuid4().hex[:14]}"
        try:
            resp = self.exec_client.place_demo_order(
                ticker=leg.ticker, action="buy", side=leg.side, count=leg.quantity,
                limit_price_cents=price, client_order_id=coid, post_only=True)
        except Exception as exc:  # noqa: BLE001
            self.stats["errors"] += 1
            result.outcome = TradeOutcome.FAILED
            result.notes.append(f"maker post failed: {exc}")
            log.warning("MAKER  post failed %s: %s", leg.ticker, exc)
            return result
        order = (resp or {}).get("order") or {}
        r = Resting(op=op, leg=leg, client_order_id=coid, order_id=str(order.get("order_id", "")),
                    price_cents=price, count=leg.quantity, placed_mono=self.clock(),
                    close_time=getattr(op, "settlement_time", None))
        r.row_id = self._row_insert(r, op, why)
        with self._lock:
            self._resting[coid] = r
        self.stats["posted"] += 1
        result.outcome = TradeOutcome.RESTING
        lx = LegExecution(leg=leg, state=OrderState.ACKNOWLEDGED, order_id=r.order_id,
                          client_order_id=coid, requested=leg.quantity)
        result.legs = [lx]
        result.notes.append(f"maker order resting: {why}")
        log.info("MAKER  RESTING  %s buy %s x%d @ %dc (taker would pay %sc) - %s",
                 leg.ticker, leg.side, leg.quantity, price, leg.limit_price_cents, why)
        # An immediate (partial) fill is possible even for post_only at the
        # touch; account for it on the first poll.
        return result

    def _decline(self, result, why):
        self.stats["declined"] += 1
        result.notes.append(f"maker: {why}")
        log.info("MAKER  not posted: %s", why)
        return result

    # ------------------------------------------------------------- poll
    def run_pass(self) -> dict:
        with self._lock:
            items = list(self._resting.values())
        now = self.clock()
        for r in items:
            try:
                self._poll(r)
            except Exception as exc:  # noqa: BLE001
                self.stats["errors"] += 1
                log.warning("MAKER  poll failed for %s: %s", r.leg.ticker, exc)
                continue
            if r.filled >= r.count:
                self._close(r, "FILLED", "fully filled")
                continue
            why = None
            if now - r.placed_mono >= self.cfg.max_rest_seconds:
                why = f"rested {self.cfg.max_rest_seconds:.0f}s unfilled"
            elif r.close_time is not None and datetime.now(timezone.utc) >= (
                    r.close_time - timedelta(minutes=self.cfg.close_within_minutes)):
                why = f"market closes within {self.cfg.close_within_minutes:.0f} min"
            elif callable(self.may_open):
                ok, reason = self.may_open(r.op)
                if not ok:
                    why = f"may no longer open: {reason}"
            if why:
                self._cancel(r, why)
        return {"resting": len(self._resting), **self.stats}

    def _poll(self, r: Resting) -> None:
        if not r.order_id:
            return
        data = self.exec_client.get_order(r.order_id)
        order = (data or {}).get("order", data) or {}
        filled = int(order.get("filled_count") or 0)
        price = price_in_leg_terms(r.leg.side, order.get("yes_price_cents"))
        if price:
            r.avg_fill_cents = float(price)
        if filled > r.filled:
            r.filled = min(filled, r.count)
            self._journal_fill(r)
        status = str(order.get("status", "")).lower()
        if status in ("canceled", "cancelled") and r.filled < r.count:
            self._close(r, "CANCELLED_BY_EXCHANGE", "exchange reports the order cancelled")

    def _journal_fill(self, r: Resting) -> None:
        """New fills since the last journal write, as a regular execution."""
        new = r.filled - r.journalled
        if new <= 0:
            return
        r.journalled = r.filled
        self.stats["contracts_filled"] += new
        price = r.avg_fill_cents or float(r.price_cents)
        lx = LegExecution(leg=r.leg, order_id=r.order_id, client_order_id=r.client_order_id,
                          requested=new, filled=new, avg_price_cents=price,
                          state=OrderState.FILLED)
        lx.fee_usd = maker_fee_usd(price, new, self.cfg.maker_fee_rate)
        res = TradeResult(opportunity=r.op, legs=[lx],
                          outcome=(TradeOutcome.COMPLETE if r.filled >= r.count
                                   else TradeOutcome.PARTIAL_FILLED),
                          total_cost_usd=lx.cost_usd, total_fees_usd=lx.fee_usd,
                          realised_edge_usd=None,
                          notes=[f"maker fill {new} @ {price:.0f}c "
                                 f"({r.filled}/{r.count} of the resting order)"])
        res.mode = "demo"
        log.info("MAKER  FILL  %s %d @ %.0fc (%d/%d)", r.leg.ticker, new, price, r.filled, r.count)
        if self.journal is not None:
            try:
                with self.journal.lock:
                    self.journal.log_trade_result(res)
            except Exception as exc:  # noqa: BLE001
                log.error("MAKER  could not journal a fill on %s: %s", r.leg.ticker, exc)
        self._row_update(r, "PARTIALLY_FILLED" if r.filled < r.count else "FILLED")

    def _cancel(self, r: Resting, why: str) -> None:
        try:
            self.exec_client.cancel_demo_order(r.order_id, market_ticker=r.leg.ticker)
        except Exception as exc:  # noqa: BLE001
            log.warning("MAKER  cancel failed for %s: %s", r.leg.ticker, exc)
        # one last look: a fill can land between the poll and the cancel
        try:
            self._poll(r)
        except Exception:  # noqa: BLE001
            pass
        self.stats["cancelled"] += 1
        self._close(r, "CANCELLED", why)

    def _close(self, r: Resting, state: str, why: str) -> None:
        with self._lock:
            self._resting.pop(r.client_order_id, None)
        if r.filled > 0:
            self.stats["filled_orders"] += 1
        self._row_update(r, state, why, closed=True)
        log.info("MAKER  %s  %s (%d/%d filled) - %s", state, r.leg.ticker, r.filled, r.count, why)

    # ---------------------------------------------------------- startup
    def cancel_orphans(self) -> int:
        """Cancel every resting order this bot posted (client_order_id prefix),
        before any trading. Returns how many."""
        n = 0
        try:
            orders = self.portfolio.orders(status="resting") if self.portfolio else []
        except Exception as exc:  # noqa: BLE001
            log.error("MAKER  could not list resting orders at start-up: %s", exc)
            orders = []
        for o in orders or []:
            coid = str(o.get("client_order_id") or "")
            if coid.startswith(COID_PREFIX):
                try:
                    self.exec_client.cancel_demo_order(str(o.get("order_id")),
                                                       market_ticker=o.get("ticker"))
                    n += 1
                except Exception as exc:  # noqa: BLE001
                    log.error("MAKER  could not cancel orphan %s: %s", o.get("order_id"), exc)
        if self.journal is not None:
            with self.journal.lock:
                self.journal.conn.execute(
                    "UPDATE resting_orders SET state='CANCELLED_ON_RESTART', closed_at=?, "
                    "reason='bot restarted' WHERE closed_at IS NULL", (_now(),))
                self.journal.conn.commit()
        if n:
            log.warning("MAKER  cancelled %d orphaned maker order(s) at start-up", n)
        return n

    # ---------------------------------------------------------- journal
    def _row_insert(self, r: Resting, op, why: str) -> int | None:
        if self.journal is None:
            return None
        fair = 100.0 * (op.probability_yes if r.leg.side == "yes" else 1 - op.probability_yes)
        with self.journal.lock:
            cur = self.journal.conn.execute(
                "INSERT INTO resting_orders (client_order_id, order_id, strategy, event_id, ticker, "
                "side, count, price_cents, fair_cents, bid_cents, ask_cents, taker_price_cents, "
                "state, placed_at, updated_at, reason, opportunity_uuid) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r.client_order_id, r.order_id, op.strategy_type, op.event_id, r.leg.ticker,
                 r.leg.side, r.count, r.price_cents, round(fair, 2), r.leg.best_bid_cents,
                 r.leg.best_ask_cents, r.leg.limit_price_cents, "RESTING", _now(), _now(), why,
                 getattr(op, "opportunity_id", None)))
            self.journal.conn.commit()
            return cur.lastrowid

    def _row_update(self, r: Resting, state: str, why: str = "", closed: bool = False) -> None:
        if self.journal is None or r.row_id is None:
            return
        with self.journal.lock:
            self.journal.conn.execute(
                "UPDATE resting_orders SET state=?, filled=?, avg_fill_cents=?, updated_at=?, "
                "closed_at=COALESCE(?, closed_at), reason=CASE WHEN ?='' THEN reason ELSE ? END "
                "WHERE id=?",
                (state, r.filled, r.avg_fill_cents or None, _now(), _now() if closed else None,
                 why, why, r.row_id))
            self.journal.conn.commit()

    def summary(self) -> str:
        return (f"{len(self._resting)} resting, {self.stats['posted']} posted, "
                f"{self.stats['filled_orders']} filled ({self.stats['contracts_filled']} contracts), "
                f"{self.stats['cancelled']} cancelled, {self.stats['declined']} declined")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
