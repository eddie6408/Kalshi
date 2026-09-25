"""The position worker.

Entering a trade and leaving one are different jobs. These tests exist mostly
to pin down the ways leaving can go wrong - especially the one that matters
most: an exit must never be blocked by a check designed to stop an entry.
"""
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


from kalshibot.depth import DepthBook, Level
from kalshibot.estimators.base import Estimate
from kalshibot.models import Market, Position
from kalshibot.position_manager import (ExitReason, PositionConfig,
                                        PositionView, PositionWorker)

SOON = datetime.now(timezone.utc) + timedelta(hours=6)


# ------------------------- fakes -------------------------
class FakeExec:
    def __init__(self, raise_on=None):
        self.sent = []
        self.raise_on = raise_on or set()

    def place_demo_order(self, ticker, action, side, count, limit_price_cents, **kw):
        if ticker in self.raise_on:
            raise RuntimeError("exchange said no")
        self.sent.append((ticker, action, side, count, limit_price_cents))
        return {"order": {"order_id": "o1", "filled_count": count}}


class FakePortfolio:
    def __init__(self, positions=()):
        self._positions = list(positions)

    def positions(self):
        return self._positions


class FakeMarket:
    """Books keyed by ticker; markets built on demand."""

    def __init__(self, books=None, hours_to_close=6.0, missing=()):
        self.books = books or {}
        self.hours = hours_to_close
        self.missing = set(missing)

    def get_depth(self, ticker, depth=50):
        if ticker in self.missing:
            raise RuntimeError("no book")
        return self.books.get(ticker, DepthBook.empty(ticker))

    def get_market(self, ticker, **kw):
        return Market(
            ticker=ticker, event_ticker="EV", series_ticker="KXFAKE",
            status="open",
            close_time=datetime.now(timezone.utc) + timedelta(hours=self.hours))


class FakeEstimator:
    name = "fake"

    def __init__(self, probability=0.60, confidence=0.9, skip=None, boom=False):
        self.probability, self.confidence = probability, confidence
        self.skip_reason, self.boom = skip, boom

    def candidate_series(self):
        return ["KXFAKE"]

    def estimate(self, market):
        if self.boom:
            raise RuntimeError("data source down")
        if self.skip_reason:
            return Estimate.skip(self.skip_reason, self.name)
        return Estimate(self.probability, self.confidence, self.name, {})


class FakeStrategy:
    def __init__(self, estimator):
        self.estimator = estimator

    def candidate_series(self):
        return self.estimator.candidate_series()


class FakeJournal:
    """Speaks the journal's real interface: fills live in leg_orders, so the
    worker asks entry_for(ticker, side) rather than reading a table itself."""

    def __init__(self, legs=()):
        # each leg: (ticker, side, action, filled, price_cents)
        self.legs = list(legs)
        self.closed = []

    def entry_for(self, ticker, side):
        rows = [l for l in self.legs if l[0] == ticker and l[1] == side]
        bought = sum(l[3] for l in rows if l[2] == "buy")
        sold = sum(l[3] for l in rows if l[2] == "sell")
        if not bought:
            return 0, None
        spend = sum(l[3] * l[4] for l in rows if l[2] == "buy")
        return bought - sold, spend / bought

    def close_open_trades(self, ticker, yes_px, no_px, outcome=None):
        self.closed.append((ticker, yes_px, no_px, outcome))
        return 1, 1.23


def book(ticker="KXFAKE-1", yes_bid=None, no_bid=None, qty=500):
    """A book we could SELL into: selling YES consumes the YES bid."""
    yes_bids = [Level(yes_bid, qty)] if yes_bid is not None else []
    no_bids = [Level(no_bid, qty)] if no_bid is not None else []
    return DepthBook(ticker, yes_bids, no_bids)


def worker(positions=(), books=None, estimator=None, journal=None,
           hours_to_close=6.0, ex=None, **cfg):
    est = estimator if estimator is not None else FakeEstimator()
    return PositionWorker(
        FakeMarket(books or {}, hours_to_close),
        FakePortfolio(positions),
        ex or FakeExec(),
        [FakeStrategy(est)],
        journal,
        PositionConfig.from_dict(cfg),
    )


def view(**kw):
    base = dict(ticker="KXFAKE-1", side="yes", quantity=25,
                exit_bid_cents=40, entry_price_cents=30.0,
                our_probability=0.60, confidence=0.9, minutes_to_close=360.0)
    base.update(kw)
    return PositionView(**base)


def cfg(**kw):
    return PositionConfig.from_dict(kw)


# ===================== the rule that matters most =====================
def test_an_exit_is_never_blocked_by_an_entry_style_edge_check():
    """The entry gate refuses a trade with too little edge. Run an exit
    through that and a losing position can never be closed. The exit path
    must reach the exchange on its own terms."""
    ex = FakeExec()
    w = worker(positions=[Position(ticker="KXFAKE-1", contracts=25)],
               books={"KXFAKE-1": book(yes_bid=5)},
               estimator=FakeEstimator(probability=0.02),
               ex=ex, min_remaining_edge_cents=2.0)
    results = w.review_once()
    assert results and results[0].decision.should_exit
    assert ex.sent == [("KXFAKE-1", "sell", "yes", 25, 5)]


def test_exiting_sells_the_side_we_hold():
    ex = FakeExec()
    w = worker(positions=[Position(ticker="KXFAKE-1", contracts=-11)],
               books={"KXFAKE-1": book(no_bid=82)},
               estimator=FakeEstimator(probability=0.90),  # P(YES) high -> NO is dead
               ex=ex)
    w.review_once()
    assert ex.sent == [("KXFAKE-1", "sell", "no", 11, 82)]


# ===================== the decision rules, in order =====================
def test_settlement_close_beats_everything_else():
    d = worker().decide(view(minutes_to_close=20, our_probability=0.99))
    assert d.reason is ExitReason.CLOSING_SOON


def test_stop_loss_does_not_wait_for_the_model_to_agree():
    """A stop that fires only when the estimator concurs is not a stop."""
    w = worker(stop_loss_fraction=0.50)
    # 25 @ 30c cost $7.50; a 10c bid is worth $2.50, down 67%
    d = w.decide(view(exit_bid_cents=10, our_probability=0.95))
    assert d.reason is ExitReason.STOP_LOSS


def test_losing_the_estimate_is_an_exit_not_a_hold():
    """Holding a forecast position with no forecast is hoping."""
    w = worker(exit_when_blind=True)
    d = w.decide(view(our_probability=None, estimate_error="no forecast data"))
    assert d.reason is ExitReason.NO_ESTIMATE
    assert "no forecast data" in d.detail


def test_being_blind_can_be_configured_to_hold_instead():
    d = worker(exit_when_blind=False).decide(view(our_probability=None))
    assert d.reason is ExitReason.HOLD


def test_the_thesis_dying_is_its_own_reason():
    """Bought at 30c believing 60%; now we think 18%."""
    d = worker(thesis_shift=0.08).decide(
        view(our_probability=0.18, exit_bid_cents=20, entry_price_cents=30.0))
    assert d.reason is ExitReason.THESIS_GONE


def test_the_market_coming_to_us_is_taking_profit():
    """Bought at 30c, market now bids 62c, we think 60%. Nothing left."""
    d = worker().decide(
        view(our_probability=0.60, exit_bid_cents=62, entry_price_cents=30.0))
    assert d.reason is ExitReason.TAKE_PROFIT
    assert "up $" in d.detail


def test_edge_decaying_while_underwater_is_labelled_differently():
    d = worker().decide(
        view(our_probability=0.28, exit_bid_cents=28, entry_price_cents=30.0))
    assert d.reason is ExitReason.EDGE_DECAYED
    assert "down $" in d.detail


def test_a_healthy_position_is_held():
    d = worker().decide(
        view(our_probability=0.60, exit_bid_cents=40, entry_price_cents=30.0))
    assert d.reason is ExitReason.HOLD
    assert not d.should_exit


# ===================== no way out =====================
def test_a_position_with_no_bid_is_reported_not_sold():
    ex = FakeExec()
    w = worker(positions=[Position(ticker="KXFAKE-1", contracts=25)],
               books={"KXFAKE-1": book()},   # nobody bidding at all
               ex=ex)
    r = w.review_once()[0]
    assert r.decision.reason is ExitReason.NO_EXIT_LIQUIDITY
    assert not r.decision.should_exit, "we cannot sell into an empty book"
    assert ex.sent == []


def test_a_one_cent_bid_can_be_refused_as_an_exit():
    w = worker(min_exit_price_cents=5)
    d = w.decide(view(exit_bid_cents=2))
    assert d.reason is ExitReason.NO_EXIT_LIQUIDITY


# ===================== arithmetic on the view =====================
def test_the_view_computes_cost_value_and_pnl():
    v = view(quantity=25, entry_price_cents=30.0, exit_bid_cents=40)
    assert v.cost_usd == 7.50
    assert v.value_usd == 10.00
    assert round(v.unrealised_usd, 2) == 2.50


def test_remaining_edge_is_our_probability_minus_the_exit_price():
    assert view(our_probability=0.60, exit_bid_cents=40).remaining_edge_cents == 20.0
    assert view(our_probability=0.30, exit_bid_cents=40).remaining_edge_cents == -10.0


def test_a_no_position_is_priced_in_no_terms():
    """P(YES)=0.30 means the NO we hold is worth 70%, not 30%."""
    w = worker(positions=[Position(ticker="KXFAKE-1", contracts=-10)],
               books={"KXFAKE-1": book(no_bid=50)},
               estimator=FakeEstimator(probability=0.30))
    v = w.review_once()[0].view
    assert v.side == "no"
    assert round(v.our_probability, 4) == 0.70


# ===================== entry price from the journal =====================
def test_average_entry_price_spans_several_buys():
    legs = [("KXFAKE-1", "yes", "buy", 10, 20), ("KXFAKE-1", "yes", "buy", 30, 40)]
    w = worker(positions=[Position(ticker="KXFAKE-1", contracts=40)],
               books={"KXFAKE-1": book(yes_bid=45)},
               journal=FakeJournal(legs))
    v = w.review_once()[0].view
    assert v.entry_price_cents == 35.0, "weighted, not the latest fill"


def test_no_journal_history_still_produces_a_decision():
    """Stop loss needs an entry price; everything else does not."""
    w = worker(positions=[Position(ticker="KXFAKE-1", contracts=25)],
               books={"KXFAKE-1": book(yes_bid=40)},
               estimator=FakeEstimator(probability=0.60))
    r = w.review_once()[0]
    assert r.view.entry_price_cents is None
    assert r.decision.reason is ExitReason.HOLD


def test_an_exit_closes_the_journal_entries():
    j = FakeJournal([("KXFAKE-1", "yes", "buy", 25, 30)])
    w = worker(positions=[Position(ticker="KXFAKE-1", contracts=25)],
               books={"KXFAKE-1": book(yes_bid=62)},
               estimator=FakeEstimator(probability=0.60), journal=j)
    w.review_once()
    assert j.closed and j.closed[0][0] == "KXFAKE-1"
    assert j.closed[0][1] == 62, "YES exit price recorded as the YES price"


# ===================== robustness =====================
def test_one_bad_position_does_not_stop_the_others():
    ex = FakeExec(raise_on={"BAD"})
    w = PositionWorker(
        FakeMarket({"BAD": book("BAD", yes_bid=60),
                    "KXFAKE-1": book(yes_bid=62)}),
        FakePortfolio([Position(ticker="BAD", contracts=5),
                       Position(ticker="KXFAKE-1", contracts=25)]),
        ex, [FakeStrategy(FakeEstimator(probability=0.60))], None,
        PositionConfig.from_dict({}))
    results = w.review_once()
    assert len(results) == 2
    assert any(r.error for r in results), "the failure must be recorded"
    assert any(r.sold for r in results), "the healthy one must still exit"


def test_an_estimator_that_raises_is_treated_as_blind():
    w = worker(positions=[Position(ticker="KXFAKE-1", contracts=25)],
               books={"KXFAKE-1": book(yes_bid=40)},
               estimator=FakeEstimator(boom=True))
    r = w.review_once()[0]
    assert r.view.our_probability is None
    assert "data source down" in r.view.estimate_error
    assert r.decision.reason is ExitReason.NO_ESTIMATE


def test_a_missing_book_does_not_raise():
    w = worker(positions=[Position(ticker="KXFAKE-1", contracts=25)],
               books={}, hours_to_close=6.0)
    w.market.missing = {"KXFAKE-1"}
    r = w.review_once()[0]
    assert r.decision.reason is ExitReason.NO_EXIT_LIQUIDITY


def test_a_portfolio_error_returns_no_reviews_rather_than_crashing():
    class Broken:
        def positions(self):
            raise RuntimeError("portfolio down")

    w = worker()
    w.portfolio = Broken()
    assert w.review_once() == []


def test_flat_positions_are_ignored():
    w = worker(positions=[Position(ticker="KXFAKE-1", contracts=0)])
    assert w.review_once() == []


# ===================== it does not fight the scan loop =====================
def test_the_engine_skips_a_ticker_the_worker_is_exiting():
    """Buying into a ticker being sold would have the bot trading against
    itself and leave reconciliation unable to explain the result."""
    from kalshibot.arb_engine import ArbitrageEngine
    w = worker()
    engine = ArbitrageEngine.__new__(ArbitrageEngine)
    engine.position_worker = w
    with w.lock:
        w.exiting.add("KXFAKE-1")
    assert engine._busy_tickers() == {"KXFAKE-1"}


def test_no_worker_means_no_busy_tickers():
    from kalshibot.arb_engine import ArbitrageEngine
    engine = ArbitrageEngine.__new__(ArbitrageEngine)
    engine.position_worker = None
    assert engine._busy_tickers() == set()


def test_the_same_ticker_is_not_exited_twice_concurrently():
    ex = FakeExec()
    w = worker(positions=[Position(ticker="KXFAKE-1", contracts=25)],
               books={"KXFAKE-1": book(yes_bid=62)},
               estimator=FakeEstimator(probability=0.60), ex=ex)
    w.exiting.add("KXFAKE-1")          # pretend another pass is mid-flight
    r = w.review_once()[0]
    assert r.error == "already exiting"
    assert ex.sent == []


# ===================== the loop -=====================
def test_the_loop_survives_a_failing_review_and_stops_when_told():
    w = worker()
    calls = []
    # Wait for the SECOND cycle to happen rather than sleeping and hoping.
    # A fixed sleep here made the suite fail at random on a busy machine, and
    # run_bot.sh refuses to start the bot on a failed self-test - so a flaky
    # test is a bot that sometimes will not run.
    went_round_twice = threading.Event()

    def boom():
        calls.append(1)
        if len(calls) >= 2:
            went_round_twice.set()
        raise RuntimeError("bad cycle")

    w.review_once = boom
    w.cfg.review_interval_seconds = 0.01
    stop = threading.Event()
    t = threading.Thread(target=w.run_forever, args=(stop,), daemon=True)
    t.start()
    try:
        assert went_round_twice.wait(timeout=10), \
            "a failing cycle must not kill the loop"
    finally:
        stop.set()
    t.join(timeout=10)
    assert not t.is_alive(), "the worker thread must exit when asked"


# ===================== watch before it acts =====================
def test_scan_only_reports_what_it_would_do_without_selling():
    """SCAN_ONLY has no execution client. The worker still reviews and still
    says what it would do - it just sends nothing."""
    w = worker(positions=[Position(ticker="KXFAKE-1", contracts=25)],
               books={"KXFAKE-1": book(yes_bid=62)},
               estimator=FakeEstimator(probability=0.60))
    w.dry_run = True
    r = w.review_once()[0]
    assert r.decision.should_exit
    assert r.sold == 0
    assert "dry run" in r.notes[0]


def test_no_execution_client_forces_dry_run():
    w = PositionWorker(FakeMarket(), FakePortfolio(), None,
                       [FakeStrategy(FakeEstimator())], None,
                       PositionConfig.from_dict({}))
    assert w.dry_run, "without a way to trade it must never claim to have sold"



# ===================== entry price comes from the REAL fill record =====================
def test_entry_price_reads_the_table_fills_actually_land_in():
    """Forecast and arbitrage fills are written to leg_orders, not trades.
    Reading only `trades` returned None for every real position, which
    silently disabled the stop loss AND the thesis check - two of six exit
    rules, dead and quiet."""
    import sqlite3
    import tempfile

    from kalshibot.journal import Journal
    with tempfile.TemporaryDirectory() as d:
        j = Journal(Path(d) / "j.db")
        j.conn.execute(
            "INSERT INTO leg_orders (ts, execution_id, ticker, side, action, "
            "requested, filled, limit_price_cents, avg_price_cents, state) "
            "VALUES ('t',1,'KXHIGHNY-26SEP17-T80','yes','buy',25,25,20,19.95,'FILLED')")
        j.conn.commit()
        held, price = j.entry_for("KXHIGHNY-26SEP17-T80", "yes")
        assert held == 25
        assert round(price, 2) == 19.95
        j.close()


def test_selling_reduces_what_the_journal_says_is_held():
    import tempfile

    from kalshibot.journal import Journal
    with tempfile.TemporaryDirectory() as d:
        j = Journal(Path(d) / "j.db")
        for action, filled, px in (("buy", 25, 20.0), ("sell", 10, 30.0)):
            j.conn.execute(
                "INSERT INTO leg_orders (ts, execution_id, ticker, side, action,"
                " requested, filled, limit_price_cents, avg_price_cents, state)"
                " VALUES ('t',1,'T','yes',?,?,?,?,?,'FILLED')",
                (action, filled, filled, int(px), px))
        j.conn.commit()
        held, price = j.entry_for("T", "yes")
        assert held == 15, "25 bought less 10 sold"
        assert price == 20.0, "entry price is what we PAID, not what we sold at"
        j.close()


def test_an_untouched_ticker_has_no_entry_price():
    import tempfile

    from kalshibot.journal import Journal
    with tempfile.TemporaryDirectory() as d:
        j = Journal(Path(d) / "j.db")
        assert j.entry_for("NEVER-TRADED", "yes") == (0, None)
        j.close()
