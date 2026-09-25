"""Multi-leg execution. A basket that half-fills is not an arbitrage - it is
naked exposure. These tests prove the manager notices and reacts.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.depth import DepthBook, Level
from kalshibot.execution_manager import (ExecutionManager, OrderState, TradeOutcome)
from kalshibot.fees import FeeModel
from kalshibot.gate import ProfitabilityGate
from kalshibot.models import Position
from kalshibot.opportunity import ArbitrageOpportunity, ExecutionRisk, Leg

FEES = FeeModel({"safety_multiplier": 1.0})


# ------------------------- fakes -------------------------
class FakeExec:
    """fill_plan maps ticker -> contracts that will actually fill."""
    def __init__(self, fill_plan=None, raise_on=None):
        self.fill_plan = fill_plan or {}
        self.raise_on = raise_on or set()
        self.orders = {}
        self.sent = []
        self.cancelled = []
        self._n = 0

    def place_demo_order(self, ticker, action, side, count, limit_price_cents, **kw):
        self.sent.append((ticker, action, side, count, limit_price_cents))
        if ticker in self.raise_on:
            raise RuntimeError(f"exchange rejected {ticker}")
        self._n += 1
        oid = f"o{self._n}"
        filled = self.fill_plan.get(ticker, count)
        self.orders[oid] = {"order_id": oid, "status": "executed" if filled >= count
                            else "resting", "filled_count": filled,
                            "yes_price": limit_price_cents}
        return {"order": self.orders[oid]}

    def cancel_demo_order(self, order_id):
        self.cancelled.append(order_id)
        return {}

    def get_order(self, order_id):
        return {"order": self.orders[order_id]}


class FakePortfolio:
    def __init__(self, balance=1000.0, positions=None):
        self._balance = balance
        self._positions = positions if positions is not None else []

    def balance_usd(self):
        return self._balance

    def positions(self):
        return self._positions


class FakeMarket:
    def __init__(self, books):
        self.books = books

    def get_depths(self, tickers, depth=50):
        return {t: self.books[t] for t in tickers if t in self.books}

    def get_depth(self, ticker, depth=50):
        return self.books[ticker]


def deep(ticker, yes_ask=40, no_ask=52, qty=1000):
    return DepthBook(ticker,
                     yes_bids=[Level(100 - no_ask, qty)],
                     no_bids=[Level(100 - yes_ask, qty)])


def opportunity(tickers=("T",), sets=10, sides=("yes", "no")):
    legs = [Leg(t, s, "buy", sets, 40 if s == "yes" else 52,
                40.0 if s == "yes" else 52.0, 1000)
            for t in tickers for s in sides]
    return ArbitrageOpportunity(
        strategy_type="same_market", event_id="EV", market_tickers=list(tickers),
        legs=legs, quantity=sets, gross_cost=sets * 0.92,
        guaranteed_min_payout=sets * 1.00, max_payout=sets * 1.00,
        gross_edge=sets * 0.08, estimated_fees=sets * 0.034,
        net_edge=sets * 0.046, net_edge_per_contract=0.046,
        expected_return_pct=5.0, execution_risk=ExecutionRisk.LOW,
        guaranteed=True, fee_drag_pct=42.0, max_executable_quantity=1000,
        min_profitable_quantity=2, optimal_quantity=sets)


def manager(ex, pf=None, mk=None, **cfg):
    # The unhedged-exposure limit has its own tests below; the rest of this
    # file is about fills and recovery, so it is left non-binding here.
    base = {"max_partial_fill_time_seconds": 0.2, "fill_poll_seconds": 0.01,
            "max_unhedged_exposure_usd": 1000.0}
    base.update(cfg)
    return ExecutionManager(ex, pf or FakePortfolio(), mk or FakeMarket({"T": deep("T")}),
                            ProfitabilityGate({}), FEES, None, base)


# ------------------------- happy path -------------------------
def test_complete_fill_is_reported_complete():
    ex = FakeExec()
    r = manager(ex).execute(opportunity())
    assert r.outcome is TradeOutcome.COMPLETE
    assert r.fully_filled and not r.exposed
    assert all(l.state is OrderState.FILLED for l in r.legs)
    assert r.sets_completed == 10


def test_realised_edge_is_computed_from_actual_fills():
    ex = FakeExec()
    r = manager(ex).execute(opportunity())
    assert abs(r.realised_edge_usd -
               (r.opportunity.guaranteed_min_payout - r.total_cost_usd
                - r.total_fees_usd)) < 1e-9
    assert r.realised_edge_usd > 0


def test_thinnest_leg_is_submitted_first():
    ex = FakeExec()
    op = opportunity()
    op.legs[0].available = 5000
    op.legs[1].available = 20
    manager(ex).execute(op)
    assert ex.sent[0][2] == op.legs[1].side, "thinnest book should go first"


# ------------------------- partial fills -------------------------
def test_partial_fill():
    """Second leg only half fills. The basket is not hedged."""
    ex = FakeExec(fill_plan={"B": 4})
    mk = FakeMarket({"A": deep("A"), "B": deep("B")})
    op = opportunity(tickers=("A", "B"), sides=("yes",), sets=10)
    r = manager(ex, mk=mk).execute(op)
    assert r.outcome in (TradeOutcome.PARTIAL_RECOVERED, TradeOutcome.PARTIAL_EXPOSED)
    assert not r.fully_filled
    short = [l for l in r.legs if l.shortfall > 0]
    assert short and short[0].leg.ticker == "B"


def test_partial_fill_cancels_the_remainder():
    ex = FakeExec(fill_plan={"B": 4})
    mk = FakeMarket({"A": deep("A"), "B": deep("B")})
    r = manager(ex, mk=mk).execute(opportunity(("A", "B"), sides=("yes",)))
    assert ex.cancelled, "the unfilled remainder must be cancelled"


def test_partial_fill_unwinds_what_did_fill():
    ex = FakeExec(fill_plan={"B": 0})
    mk = FakeMarket({"A": deep("A"), "B": deep("B")})
    r = manager(ex, mk=mk).execute(opportunity(("A", "B"), sides=("yes",)))
    sells = [s for s in ex.sent if s[1] == "sell"]
    assert sells, "a filled leg with no partner must be sold back"
    assert r.outcome is TradeOutcome.PARTIAL_RECOVERED
    assert r.unwound


def test_exposure_flagged_when_unwind_is_disabled():
    ex = FakeExec(fill_plan={"B": 0})
    mk = FakeMarket({"A": deep("A"), "B": deep("B")})
    r = manager(ex, mk=mk, attempt_unwind_on_partial=False).execute(
        opportunity(("A", "B"), sides=("yes",)))
    assert r.outcome is TradeOutcome.PARTIAL_EXPOSED
    assert r.exposed


def test_a_leg_rejected_by_the_exchange_stops_the_basket():
    ex = FakeExec(raise_on={"B"})
    mk = FakeMarket({"A": deep("A"), "B": deep("B")})
    r = manager(ex, mk=mk).execute(opportunity(("A", "B"), sides=("yes",)))
    failed = [l for l in r.legs if l.state is OrderState.FAILED]
    assert failed and "rejected" in failed[0].error
    assert r.outcome is not TradeOutcome.COMPLETE


# ------------------------- pre-trade guards -------------------------
def test_insufficient_balance():
    ex = FakeExec()
    r = manager(ex, pf=FakePortfolio(balance=0.50)).execute(opportunity())
    assert r.outcome is TradeOutcome.ABORTED_PRE_TRADE
    assert any("insufficient balance" in n for n in r.notes)
    assert ex.sent == [], "no order may be sent when the balance is short"


def test_zero_quantity_never_reaches_the_exchange():
    ex = FakeExec()
    r = manager(ex).execute(opportunity(sets=0))
    assert r.outcome is TradeOutcome.ABORTED_PRE_TRADE
    assert ex.sent == []


def test_stale_opportunity_is_not_traded_on_an_unverified_price():
    """Age alone only blocks a trade when the price has NOT been re-checked.
    Re-pricing is what makes an old opportunity current again - without it,
    an old price is just an old price."""
    ex = FakeExec()
    op = opportunity()
    op.timestamp = datetime.now(timezone.utc) - timedelta(seconds=600)
    r = manager(ex, reprice_before_send=False).execute(op)
    assert r.outcome is TradeOutcome.ABORTED_PRE_TRADE
    assert ex.sent == []


def test_book_moving_against_us_aborts_the_trade():
    """Re-pricing before sending must catch a book that got worse."""
    ex = FakeExec()
    moved = FakeMarket({"T": deep("T", yes_ask=60, no_ask=55)})   # now $1.15
    r = manager(ex, mk=moved).execute(opportunity())
    assert r.outcome is TradeOutcome.ABORTED_PRE_TRADE
    assert ex.sent == []
    assert any("stale" in n or "cost moved" in n for n in r.notes)


def test_vanished_liquidity_aborts_the_trade():
    ex = FakeExec()
    empty = FakeMarket({"T": DepthBook.empty("T")})
    r = manager(ex, mk=empty).execute(opportunity())
    assert r.outcome is TradeOutcome.ABORTED_PRE_TRADE
    assert ex.sent == []


# ------------------------- reconciliation -------------------------
def test_multi_leg_reconciliation():
    ex = FakeExec()
    pf = FakePortfolio(positions=[Position(ticker="T", contracts=10),
                                  Position(ticker="T", contracts=-10)])
    r = manager(ex, pf=pf).execute(opportunity())
    assert "reconciled" in r.notes or any("expected" in n for n in r.notes)


def test_reconciliation_reports_a_mismatch():
    """A single-sided fill that the exchange does not show must be caught.

    (An offsetting YES+NO pair on one ticker nets to zero at the exchange,
    so empty positions there is correct - see the regression test below.)"""
    ex = FakeExec()
    mk = FakeMarket({"A": deep("A"), "B": deep("B")})
    pf = FakePortfolio(positions=[])      # exchange says we hold nothing
    r = manager(ex, pf=pf, mk=mk).execute(opportunity(("A", "B"), sides=("yes",)))
    assert any("expected" in n and "exchange says" in n for n in r.notes), r.notes


def test_reconciliation_failure_is_recorded_not_swallowed():
    class Broken(FakePortfolio):
        def positions(self):
            raise RuntimeError("portfolio endpoint down")
    r = manager(FakeExec(), pf=Broken()).execute(opportunity())
    assert any("reconciliation failed" in n for n in r.notes)


def test_offsetting_legs_on_one_ticker_reconcile_to_zero():
    """Regression. A same-market arbitrage buys YES and NO on the SAME
    ticker. Those net to zero at the exchange, so per-leg comparison
    reported a mismatch that was not real."""
    ex = FakeExec()
    pf = FakePortfolio(positions=[])          # net zero, which is correct here
    r = manager(ex, pf=pf).execute(opportunity())     # yes + no on "T"
    assert r.outcome is TradeOutcome.COMPLETE
    assert "reconciled" in r.notes, r.notes
    assert not any("expected" in n and "exchange says" in n for n in r.notes)


def test_a_genuine_mismatch_is_still_reported():
    ex = FakeExec()
    pf = FakePortfolio(positions=[Position(ticker="T", contracts=999)])
    r = manager(ex, pf=pf).execute(opportunity())
    assert any("expected" in n and "exchange says" in n for n in r.notes), r.notes


def test_sell_legs_flip_the_expected_sign():
    ex = FakeExec()
    op = opportunity(sides=("yes",))
    op.legs[0].action = "sell"
    pf = FakePortfolio(positions=[Position(ticker="T", contracts=-10)])
    r = manager(ex, pf=pf).execute(op)
    assert "reconciled" in r.notes, r.notes


# ------------------------- unhedged exposure limit -------------------------
def test_worst_case_unhedged_exposure_blocks_the_trade():
    """A 2-leg structure is only risk-free once BOTH legs are on. If the
    expensive leg fills and the cheap one never does, the operator is holding
    a naked directional position. execution.max_unhedged_exposure_usd is the
    size they said they would accept, and it must be enforced BEFORE the first
    order goes out - afterwards is too late."""
    ex = FakeExec()
    op = opportunity(sets=25)          # legs cost $10.00 and $13.00
    r = manager(ex, max_unhedged_exposure_usd=5.0).execute(op)
    assert r.outcome is TradeOutcome.ABORTED_PRE_TRADE
    assert not ex.sent, "no order may be sent when the limit would be breached"
    assert any("unhedged" in n for n in r.notes), r.notes


def test_unhedged_limit_allows_a_structure_that_fits():
    ex = FakeExec()
    r = manager(ex, max_unhedged_exposure_usd=25.0).execute(opportunity(sets=25))
    assert r.outcome is TradeOutcome.COMPLETE
    assert any("worst-case unhedged" in n for n in r.notes), r.notes


def test_unhedged_limit_can_be_disabled_with_zero():
    ex = FakeExec()
    r = manager(ex, max_unhedged_exposure_usd=0).execute(opportunity(sets=25))
    assert r.outcome is TradeOutcome.COMPLETE


def test_unhedged_limit_ignores_single_leg_trades():
    """One leg cannot be half-hedged - there is nothing to hedge against."""
    ex = FakeExec()
    op = opportunity(sets=25, sides=("yes",))
    op.guaranteed_min_payout = op.quantity * 1.00
    r = manager(ex, max_unhedged_exposure_usd=1.0).execute(op)
    assert r.outcome is not TradeOutcome.ABORTED_PRE_TRADE


# ------------------------- stale on arrival -------------------------
def test_an_opportunity_found_early_in_a_long_scan_can_still_trade():
    """A scan takes 10-15 minutes. Opportunities are priced DURING it and
    traded AFTER it, against a 20-second freshness limit - so every trade
    aborted on the gate's stale check and the bot could never place an order.

    Re-pricing against fresh books is what freshness means; once that passes,
    the clock restarts."""
    ex = FakeExec()
    op = opportunity()
    op.timestamp = datetime.now(timezone.utc) - timedelta(minutes=14)
    r = manager(ex, reprice_before_send=True).execute(op)
    assert r.outcome is TradeOutcome.COMPLETE, r.notes
    assert ex.sent, "an order must actually have been sent"
    assert any("re-priced" in n for n in r.notes)


def test_without_repricing_a_stale_opportunity_is_still_refused():
    """The clock only restarts because the books were just walked. Turn that
    off and the protection has to come back."""
    ex = FakeExec()
    op = opportunity()
    op.timestamp = datetime.now(timezone.utc) - timedelta(minutes=14)
    r = manager(ex, reprice_before_send=False).execute(op)
    assert r.outcome is TradeOutcome.ABORTED_PRE_TRADE
    assert not ex.sent, "no order may be sent on an unverified stale price"


def test_repricing_does_not_rescue_an_opportunity_whose_price_moved():
    """Restarting the clock must not paper over a book that actually moved."""
    ex = FakeExec()
    op = opportunity()
    op.timestamp = datetime.now(timezone.utc) - timedelta(minutes=14)
    # the book is now far worse than the price we planned on
    worse = FakeMarket({"T": deep("T", yes_ask=70, no_ask=70)})
    r = manager(ex, mk=worse, reprice_before_send=True).execute(op)
    assert r.outcome is TradeOutcome.ABORTED_PRE_TRADE
    assert not ex.sent
