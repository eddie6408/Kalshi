"""A position in a market that has stopped trading.

This came straight from a live log. Three holdings in markets whose day was
over printed

    POSITIONS  STUCK KXHIGHNY-26SEP17-T80 x25 - no bid at or above 1c

every thirty seconds, forever. That is wrong twice over. It is wrong as a
DIAGNOSIS - the market is closed, not illiquid, and no order could be placed
on it whatever the book said. And it is wrong as a LOG - a warning printed 120
times an hour is not a warning, it is wallpaper, and the one time it matters
is the one time nobody reads it.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.depth import DepthBook, Level
from kalshibot.models import Market, Position
from kalshibot.position_manager import (ExitReason, PositionConfig,
                                        PositionView, PositionWorker)


class FakeExec:
    def __init__(self):
        self.sent = []

    def place_demo_order(self, ticker, action, side, count, limit_price_cents, **kw):
        self.sent.append((ticker, action, side, count, limit_price_cents))
        return {"order": {"order_id": "o1", "filled_count": count}}


class FakePortfolio:
    def __init__(self, positions=()):
        self._positions = list(positions)

    def positions(self):
        return self._positions


class ClosedMarket:
    """An exchange whose markets have stopped trading and have no book."""

    def __init__(self, status="closed", result=None, hours_to_close=-3.0):
        self.status, self.result, self.hours = status, result, hours_to_close

    def get_depth(self, ticker, depth=50):
        return DepthBook.empty(ticker)          # closed markets have no book

    def get_market(self, ticker, **kw):
        raw = {"status": self.status}
        if self.result is not None:
            raw["result"] = self.result
        m = Market(ticker=ticker, event_ticker="EV", series_ticker="KXFAKE",
                   status=self.status,
                   close_time=datetime.now(timezone.utc)
                   + timedelta(hours=self.hours))
        m.raw = raw
        return m


class FakeEstimator:
    name = "fake"

    def candidate_series(self):
        return ["KXFAKE"]

    def estimate(self, market):
        raise AssertionError("a closed market must never be re-estimated")


class FakeStrategy:
    def __init__(self):
        self.estimator = FakeEstimator()

    def candidate_series(self):
        return self.estimator.candidate_series()


class FakeJournal:
    def __init__(self):
        self.closed = []

    def entry_for(self, ticker, side):
        return 25, 20.0

    def close_open_trades(self, ticker, yes_px, no_px, outcome=None):
        self.closed.append((ticker, yes_px, no_px, outcome))
        return 1, 5.00


def _worker(market, positions, journal=None, ex=None, **cfg):
    return PositionWorker(market, FakePortfolio(positions), ex or FakeExec(),
                          [FakeStrategy()], journal,
                          PositionConfig.from_dict(cfg), dry_run=False)


def _pos(ticker="KXFAKE-1", contracts=25):
    return Position(ticker=ticker, contracts=contracts,
                    total_cost_cents=contracts * 20)


# --------------------- closed but not yet resolved ---------------------
def test_a_closed_market_is_awaiting_settlement_not_stuck():
    w = _worker(ClosedMarket(status="closed"), [_pos()])
    r = w.review_once()[0]
    assert r.decision.reason is ExitReason.AWAITING_SETTLEMENT
    assert r.decision.reason is not ExitReason.NO_EXIT_LIQUIDITY


def test_no_order_is_ever_sent_on_a_closed_market():
    ex = FakeExec()
    w = _worker(ClosedMarket(status="closed"), [_pos()], ex=ex)
    w.review_once()
    assert ex.sent == []


def test_the_closed_check_runs_before_the_liquidity_check():
    # An empty book would otherwise call this NO_EXIT_LIQUIDITY. The order of
    # those two checks is the whole bug.
    v = PositionView(ticker="T", side="yes", quantity=10,
                     exit_bid_cents=None, market_status="closed")
    w = _worker(ClosedMarket(), [])
    assert w.decide(v).reason is ExitReason.AWAITING_SETTLEMENT


def test_an_open_market_with_no_bid_is_still_reported_as_stuck():
    # The fix must not swallow the real case it was hiding behind.
    v = PositionView(ticker="T", side="yes", quantity=10,
                     exit_bid_cents=None, market_status="active",
                     minutes_to_close=600)
    w = _worker(ClosedMarket(), [])
    assert w.decide(v).reason is ExitReason.NO_EXIT_LIQUIDITY


def test_an_unknown_status_is_not_treated_as_closed():
    # Inferring "closed" from a missing status would make every market with a
    # patchy API response look settled.
    v = PositionView(ticker="T", side="yes", quantity=10,
                     exit_bid_cents=None, market_status="")
    w = _worker(ClosedMarket(), [])
    assert w.decide(v).reason is ExitReason.NO_EXIT_LIQUIDITY


# --------------------------- resolved ---------------------------
def test_a_resolved_market_is_settled_and_booked_once():
    j = FakeJournal()
    w = _worker(ClosedMarket(status="settled", result="yes"), [_pos()], j)
    r = w.review_once()[0]
    assert r.decision.reason is ExitReason.SETTLED
    assert j.closed == [("KXFAKE-1", 100, 0, "SETTLED")]

    # The position lingers in the portfolio until the exchange clears it.
    # Reviewing again must not book it a second time.
    w.review_once()
    assert len(j.closed) == 1


def test_a_yes_holding_that_resolved_no_is_booked_at_zero():
    j = FakeJournal()
    w = _worker(ClosedMarket(status="settled", result="no"), [_pos()], j)
    r = w.review_once()[0]
    assert r.decision.reason is ExitReason.SETTLED
    assert j.closed == [("KXFAKE-1", 0, 100, "SETTLED")]
    assert "LOST" in r.notes[0]


def test_a_no_holding_wins_when_the_market_resolves_no():
    j = FakeJournal()
    w = _worker(ClosedMarket(status="settled", result="no"),
                [_pos(contracts=-11)], j)
    r = w.review_once()[0]
    assert r.view.side == "no"
    assert r.view.won is True
    # Booked in YES terms regardless of which side we hold.
    assert j.closed == [("KXFAKE-1", 0, 100, "SETTLED")]


def test_a_settled_market_whose_result_is_unreadable_waits_instead_of_guessing():
    # Booking a settlement the wrong way is worse than booking it late.
    j = FakeJournal()
    w = _worker(ClosedMarket(status="settled", result=None), [_pos()], j)
    r = w.review_once()[0]
    assert r.decision.reason is ExitReason.AWAITING_SETTLEMENT
    assert j.closed == []


def test_a_failed_booking_is_retried_next_cycle():
    class Broken(FakeJournal):
        def close_open_trades(self, *a, **kw):
            raise RuntimeError("database is locked")
    j = Broken()
    w = _worker(ClosedMarket(status="settled", result="yes"), [_pos()], j)
    r = w.review_once()[0]
    assert r.error
    # Losing the record of an outcome is worse than logging it twice, so the
    # ticker must NOT be marked as booked.
    assert "KXFAKE-1" not in w.booked


def test_no_estimator_is_called_for_a_closed_market():
    # FakeEstimator raises if it is. Re-forecasting a market whose day is over
    # is wasted API calls on every single review cycle.
    w = _worker(ClosedMarket(status="settled", result="yes"), [_pos()],
                FakeJournal())
    w.review_once()        # would raise AssertionError if estimated


# --------------------------- log volume ---------------------------
def test_the_stuck_warning_is_rate_limited():
    v = PositionView(ticker="T", side="yes", quantity=10,
                     exit_bid_cents=None, market_status="active",
                     minutes_to_close=600)
    w = _worker(ClosedMarket(), [], stuck_warn_every_minutes=30)
    from kalshibot.position_manager import ExitDecision, ReviewResult
    r = ReviewResult(view=v, decision=ExitDecision(
        ExitReason.NO_EXIT_LIQUIDITY, "no bid"))

    w._warn_stuck(r)
    first = w._warned_at["T"]
    for _ in range(10):
        w._warn_stuck(r)
    assert w._warned_at["T"] == first          # not re-warned, not re-stamped

    # Once the window has passed it warns again - the position still matters.
    w._warned_at["T"] = first - timedelta(minutes=31)
    w._warn_stuck(r)
    assert w._warned_at["T"] > first


def test_exits_are_still_never_gated_by_any_of_this():
    # The rule the whole worker is built around. A market that is OPEN with a
    # real bid still exits normally.
    class OpenMarket(ClosedMarket):
        def get_depth(self, ticker, depth=50):
            return DepthBook(ticker, [Level(90, 500)], [])

    ex = FakeExec()
    w = _worker(OpenMarket(status="active", hours_to_close=0.2), [_pos()],
                FakeJournal(), ex=ex)
    r = w.review_once()[0]
    assert r.decision.reason is ExitReason.CLOSING_SOON
    assert ex.sent == [("KXFAKE-1", "sell", "yes", 25, 90)]
