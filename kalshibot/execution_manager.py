"""Multi-leg execution.

A structural arbitrage is only an arbitrage if every leg fills. Fill three
legs of four and you are not holding a hedged basket - you are holding naked
directional exposure you never chose.

So this module never assumes a fill. Every leg moves through an explicit
state machine, the manager watches for partial fills, and when a basket
cannot be completed it tries to unwind what it did get rather than leaving it
on the book and hoping.

Order of operations for every trade:

    1. re-price against fresh books      (stale opportunities are dropped)
    2. re-check fees and profitability   (the gate runs again, not once)
    3. check balance and limits
    4. submit legs, thinnest book first  (fail early, fail cheap)
    5. poll fills
    6. on shortfall: cancel, then unwind what filled
    7. reconcile expected vs actual positions
    8. write everything down
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from .clients import price_in_leg_terms
from .depth import DepthBook
from .opportunity import ArbitrageOpportunity, Leg

log = logging.getLogger(__name__)


class OrderState(str, Enum):
    PENDING = "PENDING"
    SUBMITTED = "ORDER_SUBMITTED"
    ACKNOWLEDGED = "ORDER_ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class TradeOutcome(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL_RECOVERED = "PARTIAL_RECOVERED"
    PARTIAL_EXPOSED = "PARTIAL_EXPOSED"
    #: A single-leg order that filled in part. The remainder was cancelled and
    #: what filled is simply a smaller position: one leg has nothing to hedge.
    #: Until 26 September this was reported PARTIAL_EXPOSED, "unhedged -
    #: MANUAL REVIEW", for a forecast trade that was never a hedge.
    PARTIAL_FILLED = "PARTIAL_FILLED"
    ABORTED_PRE_TRADE = "ABORTED_PRE_TRADE"
    FAILED = "FAILED"


@dataclass
class LegExecution:
    leg: Leg
    state: OrderState = OrderState.PENDING
    order_id: str = ""
    client_order_id: str = ""
    requested: int = 0
    filled: int = 0
    avg_price_cents: float = 0.0
    fee_usd: float = 0.0
    error: str = ""
    submitted_at: datetime | None = None

    @property
    def shortfall(self) -> int:
        return max(0, self.requested - self.filled)

    @property
    def cost_usd(self) -> float:
        return self.filled * self.avg_price_cents / 100.0


@dataclass
class TradeResult:
    opportunity: ArbitrageOpportunity
    outcome: TradeOutcome
    legs: list[LegExecution] = field(default_factory=list)
    sets_completed: int = 0
    total_cost_usd: float = 0.0
    total_fees_usd: float = 0.0
    #: guaranteed edge at entry (arbitrage); None when not knowable at entry
    realised_edge_usd: float | None = 0.0
    unwound: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    started: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def fully_filled(self) -> bool:
        return bool(self.legs) and all(l.shortfall == 0 for l in self.legs)

    @property
    def exposed(self) -> bool:
        return self.outcome == TradeOutcome.PARTIAL_EXPOSED


class ExecutionManager:
    def __init__(self, execution_client, portfolio_client, market_data_client,
                 gate, fees, journal=None, cfg: dict | None = None):
        self.exec_client = execution_client
        self.portfolio = portfolio_client
        self.market = market_data_client
        self.gate = gate
        self.fees = fees
        self.journal = journal
        c = cfg or {}
        self.fill_timeout_s = float(c.get("max_partial_fill_time_seconds", 8))
        self.poll_interval_s = float(c.get("fill_poll_seconds", 0.6))
        self.attempt_unwind = bool(c.get("attempt_unwind_on_partial", True))
        self.max_unhedged_usd = float(c.get("max_unhedged_exposure_usd", 5.0))
        self.reprice_before_send = bool(c.get("reprice_before_send", True))
        #: The positions endpoint lags a fill; re-read before crying mismatch.
        self.reconcile_attempts = int(c.get("reconcile_attempts", 4))
        self.reconcile_backoff_s = float(c.get("reconcile_backoff_seconds", 1.5))

    # ==============================================================
    def execute(self, op: ArbitrageOpportunity) -> TradeResult:
        result = TradeResult(opportunity=op, outcome=TradeOutcome.ABORTED_PRE_TRADE)
        log.info("TRADE START   %s", op.summary())

        ok, why = self._pre_trade(op, result)
        if not ok:
            log.info("TRADE ABORTED (pre-trade) %s", why)
            result.notes.append(why)
            self._record(result)
            return result

        # thinnest book first: if the hard leg fails we have spent the least
        order = sorted(op.legs, key=lambda l: l.available)
        result.legs = [LegExecution(leg=l, requested=l.quantity) for l in order]

        for lx in result.legs:
            self._submit(lx)
            if lx.state is OrderState.FAILED:
                log.error("TRADE LEG FAILED  %s: %s", lx.leg.ticker, lx.error)
                break

        self._await_fills(result)

        shortfalls = [l for l in result.legs if l.shortfall > 0]
        if not shortfalls:
            result.outcome = TradeOutcome.COMPLETE
            result.sets_completed = op.quantity
            log.info("TRADE COMPLETE  all %d legs filled", len(result.legs))
        else:
            log.warning("TRADE PARTIAL  %d of %d legs short - basket is not hedged",
                        len(shortfalls), len(result.legs))
            self._recover(result)

        self._finalise(result)
        self._reconcile(result)
        self._record(result)
        return result

    # ==============================================================
    def _pre_trade(self, op: ArbitrageOpportunity, result: TradeResult
                   ) -> tuple[bool, str]:
        if op.quantity <= 0:
            return False, "zero quantity"

        # Worst-case naked exposure: every leg fills except the cheapest one.
        # A multi-leg structure is only risk-free once ALL legs are on. Until
        # then the operator is holding a directional position, and this is the
        # size they said they were willing to hold if the hedge never lands.
        if len(op.legs) > 1 and self.max_unhedged_usd > 0:
            leg_costs = [l.quantity * l.limit_price_cents / 100.0 for l in op.legs]
            worst_unhedged = sum(leg_costs) - min(leg_costs)
            if worst_unhedged > self.max_unhedged_usd:
                return False, (
                    f"a partial fill could leave ${worst_unhedged:.2f} unhedged, "
                    f"above the ${self.max_unhedged_usd:.2f} limit "
                    f"(execution.max_unhedged_exposure_usd)")
            result.notes.append(
                f"worst-case unhedged ${worst_unhedged:.2f} "
                f"within ${self.max_unhedged_usd:.2f}")

        if self.reprice_before_send and self.market is not None:
            fresh = self.market.get_depths([l.ticker for l in op.legs])
            ok, why = self._still_valid(op, fresh)
            if not ok:
                return False, f"opportunity went stale: {why}"
            result.notes.append("re-priced against fresh books")
            # An opportunity is found DURING the scan and traded AFTER it, and
            # a scan takes 10-15 minutes. Against a 20-second freshness limit
            # that made every single trade abort as STALE on the gate re-check
            # - the bot could never place an order at all.
            #
            # Re-pricing is what freshness actually means: the books were just
            # walked and the cost was confirmed. Stamping the clock here is
            # honest, and it happens ONLY on the repriced path - with
            # reprice_before_send off, the original timestamp stands and the
            # stale check still bites.
            op.timestamp = datetime.now(timezone.utc)

        decision = self.gate.evaluate(op)
        if not decision.accepted:
            return False, f"gate rejected on re-check: {decision}"
        result.notes.append("gate passed on re-check")

        if self.portfolio is not None:
            try:
                balance = self.portfolio.balance_usd()
            except Exception as exc:  # noqa: BLE001
                return False, f"could not read balance: {exc}"
            needed = op.gross_cost + op.estimated_fees
            if balance < needed:
                return False, (f"insufficient balance: ${balance:.2f} available, "
                               f"${needed:.2f} needed")
            result.notes.append(f"balance ${balance:.2f} covers ${needed:.2f}")
        return True, "ok"

    def _still_valid(self, op: ArbitrageOpportunity,
                     books: dict[str, DepthBook]) -> tuple[bool, str]:
        """Would this still cost what we priced it at, right now?"""
        total = 0
        for leg in op.legs:
            book = books.get(leg.ticker)
            if book is None:
                return False, f"no book for {leg.ticker}"
            walk = book.walk(leg.side, "buy", leg.quantity, leg.limit_price_cents)
            if not walk.complete:
                return False, (f"{leg.ticker} can only fill {walk.filled} of "
                               f"{leg.quantity} at {leg.limit_price_cents}c")
            total += walk.total_cost_cents
        new_cost = total / 100.0
        if new_cost > op.gross_cost + 1e-9:
            slip = new_cost - op.gross_cost
            if slip > max(0.0, op.net_edge):
                return False, (f"cost moved from ${op.gross_cost:.2f} to "
                               f"${new_cost:.2f}, which eats the ${op.net_edge:.2f} edge")
        return True, "ok"

    # ==============================================================
    def _submit(self, lx: LegExecution) -> None:
        leg = lx.leg
        lx.client_order_id = f"kb-{uuid.uuid4().hex[:16]}"
        lx.submitted_at = datetime.now(timezone.utc)
        lx.state = OrderState.SUBMITTED
        log.info("TRADE LEG SUBMITTED  %s %s %s x%d @ %dc",
                 leg.ticker, leg.action, leg.side, leg.quantity, leg.limit_price_cents)
        try:
            resp = self.exec_client.place_demo_order(
                ticker=leg.ticker, action=leg.action, side=leg.side,
                count=leg.quantity, limit_price_cents=leg.limit_price_cents,
                client_order_id=lx.client_order_id,
            )
        except Exception as exc:  # noqa: BLE001
            lx.state = OrderState.FAILED
            lx.error = str(exc)
            return

        order = resp.get("order", resp) or {}
        lx.order_id = str(order.get("order_id", ""))
        lx.state = OrderState.ACKNOWLEDGED
        filled = order.get("filled_count")
        if filled is not None:
            lx.filled = int(filled)
            lx.avg_price_cents = (
                price_in_leg_terms(leg.side, order.get("yes_price_cents"))
                or float(leg.limit_price_cents))
            lx.state = (OrderState.FILLED if lx.shortfall == 0
                        else OrderState.PARTIALLY_FILLED)
        log.info("TRADE LEG ACK        %s order_id=%s status=%s",
                 leg.ticker, lx.order_id or "?", order.get("status", "?"))

    def _await_fills(self, result: TradeResult) -> None:
        deadline = time.time() + self.fill_timeout_s
        pending = [l for l in result.legs
                   if l.state in (OrderState.ACKNOWLEDGED, OrderState.SUBMITTED,
                                  OrderState.PARTIALLY_FILLED)]
        while pending and time.time() < deadline:
            time.sleep(self.poll_interval_s)
            for lx in list(pending):
                self._refresh(lx)
                if lx.state in (OrderState.FILLED, OrderState.CANCELLED,
                                OrderState.FAILED):
                    pending.remove(lx)
                    log.info("TRADE LEG %s  %s %d/%d",
                             lx.state.value, lx.leg.ticker, lx.filled, lx.requested)
        for lx in pending:
            if lx.state is not OrderState.FILLED:
                log.warning("TRADE LEG TIMEOUT   %s filled %d/%d after %.0fs",
                            lx.leg.ticker, lx.filled, lx.requested, self.fill_timeout_s)

    def _refresh(self, lx: LegExecution) -> None:
        if not lx.order_id:
            lx.state = OrderState.UNKNOWN
            return
        try:
            data = self.exec_client.get_order(lx.order_id)
        except Exception as exc:  # noqa: BLE001
            lx.state = OrderState.UNKNOWN
            lx.error = str(exc)
            return
        order = data.get("order", data) or {}
        lx.filled = int(order.get("filled_count") or lx.filled or 0)
        price = price_in_leg_terms(lx.leg.side, order.get("yes_price_cents"))
        if price:
            lx.avg_price_cents = float(price)
        status = str(order.get("status", "")).lower()
        if status in ("executed", "filled") or lx.shortfall == 0:
            lx.state = OrderState.FILLED
        elif status in ("canceled", "cancelled"):
            lx.state = OrderState.CANCELLED
        elif lx.filled > 0:
            lx.state = OrderState.PARTIALLY_FILLED

    # ==============================================================
    def _recover(self, result: TradeResult) -> None:
        log.warning("TRADE RECOVERY START")
        for lx in result.legs:
            if lx.order_id and lx.state in (OrderState.ACKNOWLEDGED,
                                            OrderState.PARTIALLY_FILLED,
                                            OrderState.SUBMITTED):
                try:
                    self.exec_client.cancel_demo_order(lx.order_id)
                    if lx.filled == 0:
                        lx.state = OrderState.CANCELLED
                    result.notes.append(f"cancelled remainder on {lx.leg.ticker}")
                except Exception as exc:  # noqa: BLE001
                    log.error("could not cancel %s: %s", lx.order_id, exc)

        filled_legs = [l for l in result.legs if l.filled > 0]
        if not filled_legs:
            result.outcome = TradeOutcome.ABORTED_PRE_TRADE
            result.notes.append("nothing filled, no exposure taken")
            log.info("TRADE RECOVERY OK    nothing filled")
            return

        # One leg: nothing to hedge and nothing to unwind. Selling back what
        # filled would pay the spread to undo a trade the strategy wanted.
        if len(result.legs) == 1:
            lx = filled_legs[0]
            result.outcome = TradeOutcome.PARTIAL_FILLED
            result.notes.append(f"single leg: kept {lx.filled} of {lx.requested}; "
                                f"nothing to hedge")
            log.info("TRADE PARTIAL FILL   %s kept %d of %d - single leg, "
                     "nothing to hedge", lx.leg.ticker, lx.filled, lx.requested)
            return

        if not self.attempt_unwind:
            result.outcome = TradeOutcome.PARTIAL_EXPOSED
            log.error("TRADE EXPOSED        unwind disabled; %d legs held",
                      len(filled_legs))
            return

        exposed_usd = 0.0
        for lx in filled_legs:
            if self._unwind(lx, result):
                continue
            exposed_usd += lx.cost_usd

        if exposed_usd <= 0:
            result.outcome = TradeOutcome.PARTIAL_RECOVERED
            log.info("TRADE RECOVERY OK    all filled legs unwound")
        else:
            result.outcome = TradeOutcome.PARTIAL_EXPOSED
            log.error("TRADE EXPOSED        $%.2f of unhedged position remains. "
                      "MANUAL REVIEW REQUIRED.", exposed_usd)
            result.notes.append(f"unhedged exposure ${exposed_usd:.2f}")

    def _unwind(self, lx: LegExecution, result: TradeResult) -> bool:
        """Sell back what we were left holding."""
        try:
            book = self.market.get_depth(lx.leg.ticker) if self.market else None
            price = book.best_price(lx.leg.side, "sell") if book else None
            if not price:
                log.error("cannot unwind %s: no bid to sell into", lx.leg.ticker)
                return False
            self.exec_client.place_demo_order(
                ticker=lx.leg.ticker, action="sell", side=lx.leg.side,
                count=lx.filled, limit_price_cents=price,
            )
            result.unwound.append(f"{lx.leg.ticker} x{lx.filled} @ {price}c")
            log.info("TRADE UNWIND         sold %d %s %s @ %dc",
                     lx.filled, lx.leg.side, lx.leg.ticker, price)
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("unwind failed for %s: %s", lx.leg.ticker, exc)
            return False

    # ==============================================================
    def _finalise(self, result: TradeResult) -> None:
        result.total_cost_usd = sum(l.cost_usd for l in result.legs)
        result.total_fees_usd = sum(
            self.fees.entry_fee_usd(l.avg_price_cents or l.leg.limit_price_cents,
                                    l.filled)
            for l in result.legs if l.filled)
        for l in result.legs:
            l.fee_usd = self.fees.entry_fee_usd(
                l.avg_price_cents or l.leg.limit_price_cents, l.filled)
        # Only a guaranteed basket has an edge that is known at entry. A
        # forecast trade has no guaranteed payout; booking "0 payout - cost"
        # recorded every weather purchase as a loss of its whole price.
        # Its result arrives later, from the exit or the settlement.
        if (result.outcome is TradeOutcome.COMPLETE
                and getattr(result.opportunity, "guaranteed", True)):
            payout = result.opportunity.guaranteed_min_payout
            result.realised_edge_usd = payout - result.total_cost_usd - result.total_fees_usd
        else:
            result.realised_edge_usd = None

    def _reconcile(self, result: TradeResult) -> None:
        """Compare what we think we own against what the exchange says."""
        if self.portfolio is None:
            result.notes.append("reconciliation skipped: no portfolio client")
            return
        # The positions endpoint lags a fill by a second or two. Reading it
        # immediately reported "expected +25, exchange says +0" on a trade
        # that had in fact filled - a false alarm that looks exactly like the
        # real thing. Give it a few tries before believing it.
        expected_tickers = {lx.leg.ticker for lx in result.legs if lx.filled}
        actual: dict[str, int] = {}
        for attempt in range(self.reconcile_attempts):
            try:
                actual = {p.ticker: p.contracts
                          for p in self.portfolio.positions()}
            except Exception as exc:  # noqa: BLE001
                result.notes.append(f"reconciliation failed: {exc}")
                log.error("TRADE RECONCILE FAILED %s", exc)
                return
            if all(actual.get(t) for t in expected_tickers):
                break
            if attempt + 1 < self.reconcile_attempts:
                time.sleep(self.reconcile_backoff_s)

        # Aggregate per TICKER before comparing. A same-market arbitrage buys
        # YES and NO on one ticker; those two legs net to zero, so checking
        # each leg separately reports a mismatch that is not real.
        expected: dict[str, int] = {}
        for lx in result.legs:
            if lx.filled == 0:
                continue
            signed = lx.filled if lx.leg.side == "yes" else -lx.filled
            if lx.leg.action == "sell":
                signed = -signed
            expected[lx.leg.ticker] = expected.get(lx.leg.ticker, 0) + signed

        mismatches = []
        for ticker, want in expected.items():
            have = actual.get(ticker, 0)
            if have != want:
                mismatches.append(
                    f"{ticker}: expected {want:+d}, exchange says {have:+d}")
        if mismatches:
            for m in mismatches:
                log.error("TRADE RECONCILE MISMATCH  %s", m)
            result.notes.extend(mismatches)
        else:
            log.info("TRADE RECONCILED     positions match expectations")
            result.notes.append("reconciled")

    def _record(self, result: TradeResult) -> None:
        if self.journal is None:
            return
        try:
            self.journal.log_trade_result(result)
        except Exception as exc:  # noqa: BLE001
            log.error("journal write failed: %s", exc)
