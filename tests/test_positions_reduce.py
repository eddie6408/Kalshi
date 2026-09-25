"""Partial reduction: "should I reduce exposure?"

An exit is all-or-nothing, which is too blunt. A position can still be on the
right side while being too big for the edge that is left.

The rules being pinned here are the two that keep reduction from quietly
becoming something else:

  1. A trim is only ever considered where every exit rule has ALREADY declined
     to fire. Reduction refines holding; it must never soften leaving.
  2. A trim can never close a position. Closing belongs to the exit rules.

Everything else in this file is about not bleeding money in fees while doing
it, because a rule that trims five contracts every thirty seconds is worse
than no rule at all.
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


class FakeMarketSrc:
    def __init__(self, yes_bid=60, hours=6.0):
        self.yes_bid, self.hours = yes_bid, hours

    def get_depth(self, ticker, depth=50):
        return DepthBook(ticker, [Level(self.yes_bid, 500)],
                         [Level(100 - self.yes_bid, 500)])

    def get_market(self, ticker, **kw):
        return Market(ticker=ticker, event_ticker="EV", series_ticker="KXFAKE",
                      status="active",
                      close_time=datetime.now(timezone.utc)
                      + timedelta(hours=self.hours))


class FakeEstimator:
    name = "fake"

    def __init__(self, probability=0.62, confidence=0.9):
        self.probability, self.confidence = probability, confidence

    def candidate_series(self):
        return ["KXFAKE"]

    def estimate(self, market):
        from kalshibot.estimators.base import Estimate
        return Estimate(self.probability, self.confidence, self.name, {})


class FakeStrategy:
    def __init__(self, est):
        self.estimator = est

    def candidate_series(self):
        return self.estimator.candidate_series()


class FakeJournal:
    def __init__(self, entry=40.0, held=40):
        self.entry, self.held = entry, held
        self.legs = []
        self.closed = []

    def entry_for(self, ticker, side):
        return self.held, self.entry

    def log_leg_fill(self, **kw):
        self.legs.append(kw)

    def close_open_trades(self, ticker, yes_px, no_px, outcome=None):
        self.closed.append((ticker, yes_px, no_px, outcome))
        return 1, 1.0


def _worker(positions=(), yes_bid=60, probability=0.62, confidence=0.9,
            journal=None, ex=None, hours=6.0, **cfg):
    base = {"bankroll_usd": 1000.0, "kelly_fraction": 0.25,
            "max_cost_per_trade_usd": 25.0}
    base.update(cfg)
    return PositionWorker(
        FakeMarketSrc(yes_bid, hours), FakePortfolio(positions),
        ex or FakeExec(), [FakeStrategy(FakeEstimator(probability, confidence))],
        journal, PositionConfig.from_dict(base), dry_run=False)


def _view(quantity=40, bid=60, p=0.62, conf=0.9, entry=40.0):
    return PositionView(ticker="KXFAKE-1", side="yes", quantity=quantity,
                        exit_bid_cents=bid, entry_price_cents=entry,
                        our_probability=p, confidence=conf,
                        minutes_to_close=600, market_status="active")


# ------------------------- the ordering rule -------------------------
def test_reduction_never_pre_empts_an_exit():
    # Inside the settlement window the answer is EXIT, full stop - even though
    # the position is also far too large for its edge.
    w = _worker()
    v = _view(quantity=400)
    v.minutes_to_close = 10
    d = w.decide(v)
    assert d.reason is ExitReason.CLOSING_SOON
    assert d.sell_quantity is None          # the whole thing


def test_a_stop_loss_is_never_softened_into_a_trim():
    w = _worker()
    v = _view(quantity=400, bid=10, entry=80.0)
    d = w.decide(v)
    assert d.reason is ExitReason.STOP_LOSS
    assert d.sell_quantity is None


def test_reduction_is_not_a_full_exit():
    w = _worker()
    d = w.decide(_view(quantity=400))
    assert d.reason is ExitReason.REDUCE
    assert d.should_exit is False           # must not be treated as a close
    assert d.should_sell is True            # but an order does go out


# ------------------------- the floor rule -------------------------
def test_a_trim_can_never_close_a_position():
    # A 5-point edge held at almost no confidence: Kelly says 2 contracts, and
    # the exit rules have not fired because 5c of edge is still 5c of edge.
    # The floor is what stops a trim from finishing the job the exit rules
    # deliberately did not.
    w = _worker(min_hold_contracts=5)
    d = w.decide(_view(quantity=400, bid=60, p=0.65, conf=0.05))
    assert d.reason is ExitReason.REDUCE
    assert d.sell_quantity == 400 - 5
    assert d.sell_quantity < 400


def test_a_thin_edge_is_the_exit_rules_job_not_the_trimmers():
    # 0.1c of edge left. This must come back as an exit, not as a trim down
    # to the floor - which is what it did before the ordering was pinned.
    w = _worker()
    d = w.decide(_view(quantity=400, bid=95, p=0.951))
    assert d.reason in (ExitReason.TAKE_PROFIT, ExitReason.EDGE_DECAYED)
    assert d.sell_quantity is None


def test_a_position_already_at_the_floor_is_left_alone():
    w = _worker(min_hold_contracts=5)
    assert w.decide(_view(quantity=5)).reason is not ExitReason.REDUCE


# ------------------------- fee discipline -------------------------
def test_a_trim_smaller_than_the_minimum_is_not_worth_the_fees():
    w = _worker(min_reduce_contracts=5)
    # Kelly here justifies only a couple fewer contracts than we hold.
    v = _view(quantity=40)
    keep_target = w._reduction(v)
    if keep_target is not None:
        assert keep_target[1] >= 5


def test_a_trim_worth_less_than_the_money_floor_is_skipped():
    # 30 held, Kelly says 18: a 12-contract trim at 60c frees $7.20. Not worth
    # paying a fee and giving up the spread for.
    w = _worker(min_reduce_contracts=1, min_reduce_value_usd=50.0)
    assert w.decide(_view(quantity=30)).reason is not ExitReason.REDUCE


def test_the_cooldown_stops_a_trim_every_thirty_seconds():
    ex = FakeExec()
    j = FakeJournal()
    w = _worker([Position(ticker="KXFAKE-1", contracts=400,
                          total_cost_cents=400 * 40)],
                journal=j, ex=ex, reduce_cooldown_minutes=60)
    first = w.review_once()[0]
    assert first.decision.reason is ExitReason.REDUCE
    assert len(ex.sent) == 1

    for _ in range(5):
        w.review_once()
    assert len(ex.sent) == 1, "trimmed again inside the cooldown"

    # Once the cooldown lapses it may trim again.
    w._reduced_at["KXFAKE-1"] = datetime.now(timezone.utc) - timedelta(minutes=61)
    w.review_once()
    assert len(ex.sent) == 2


def test_the_cooldown_applies_in_dry_run_too():
    # Otherwise the reporting-only log proposes the same trim every cycle and
    # reads nothing like what the live bot would do.
    w = _worker([Position(ticker="KXFAKE-1", contracts=400,
                          total_cost_cents=400 * 40)], journal=FakeJournal())
    w.dry_run = True
    w.review_once()
    assert "KXFAKE-1" in w._reduced_at


# ------------------------- sizing -------------------------
def test_more_confidence_justifies_holding_more():
    shy = _worker(confidence=0.3)._reduction(_view(quantity=400, conf=0.3))
    sure = _worker(confidence=0.95)._reduction(_view(quantity=400, conf=0.95))
    assert shy is not None and sure is not None
    assert sure[0] > shy[0], "a confident estimate should keep more"


def test_a_bigger_edge_justifies_holding_more():
    thin = _worker()._reduction(_view(quantity=400, bid=60, p=0.62))
    fat = _worker()._reduction(_view(quantity=400, bid=60, p=0.85))
    assert thin is not None
    assert fat is None or fat[0] > thin[0]


def test_reduction_prices_off_the_bid_we_could_actually_sell_into():
    # Not the mid. The bid is the price really available to us.
    w = _worker()
    d = w.decide(_view(quantity=400, bid=60))
    assert d.sell_price_cents == 60


def test_reduction_can_be_switched_off_in_one_line():
    w = _worker(reduce_enabled=False)
    assert w.decide(_view(quantity=400)).reason is ExitReason.HOLD


def test_no_estimate_means_no_trim():
    w = _worker()
    v = _view(quantity=400)
    v.our_probability = None
    # With exit_when_blind on this is an exit, not a trim.
    assert w.decide(v).reason is ExitReason.NO_ESTIMATE


# ------------------------- execution -------------------------
def test_only_the_trimmed_quantity_is_sold():
    ex = FakeExec()
    w = _worker([Position(ticker="KXFAKE-1", contracts=400,
                          total_cost_cents=400 * 40)],
                journal=FakeJournal(), ex=ex)
    r = w.review_once()[0]
    ticker, action, side, count, price = ex.sent[0]
    assert action == "sell" and side == "yes"
    assert 0 < count < 400
    assert count == r.decision.sell_quantity
    assert r.sold == count


def test_a_trim_is_written_to_the_table_that_holds_real_fills():
    # entry_for() reconstructs the position from leg_orders. A sale that never
    # lands there leaves that arithmetic describing a position we do not have.
    j = FakeJournal()
    w = _worker([Position(ticker="KXFAKE-1", contracts=400,
                          total_cost_cents=400 * 40)], journal=j)
    w.review_once()
    assert j.legs and j.legs[0]["action"] == "sell"
    assert j.legs[0]["note"] == "REDUCE"


def test_a_trim_does_not_close_the_journal_position():
    j = FakeJournal()
    w = _worker([Position(ticker="KXFAKE-1", contracts=400,
                          total_cost_cents=400 * 40)], journal=j)
    w.review_once()
    assert j.closed == [], "a partial sale must not book the whole position"


def test_a_refused_order_is_not_recorded_as_a_fill():
    class Refusing(FakeExec):
        def place_demo_order(self, **kw):
            raise RuntimeError("exchange said no")
    j = FakeJournal()
    w = _worker([Position(ticker="KXFAKE-1", contracts=400,
                          total_cost_cents=400 * 40)],
                journal=j, ex=Refusing())
    r = w.review_once()[0]
    assert r.error
    assert j.legs == []
    assert "KXFAKE-1" not in w._reduced_at, "a failed trim must not start a cooldown"


def test_a_nonsense_quantity_is_refused_rather_than_sent():
    from kalshibot.position_manager import ExitDecision, ReviewResult
    ex = FakeExec()
    w = _worker(ex=ex)
    v = _view(quantity=10)
    r = ReviewResult(view=v, decision=ExitDecision(
        ExitReason.REDUCE, "bad", 60, sell_quantity=99))
    w._exit(r)
    assert ex.sent == []
    assert "refusing to sell" in r.error
