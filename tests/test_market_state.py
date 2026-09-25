"""The shared market state.

Every test here is really about one question: when is a cached book safe to
trade on? A stale book and a book that missed an update both look perfectly
healthy from the outside, and both price a trade wrongly, size it wrongly, and
report a profit that was never there.

So the store is tested on what it REFUSES to serve, not on what it caches.
"""
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.depth import DepthBook, Level
from kalshibot.market_state import MarketState, Source
from kalshibot.models import Market


class FakeClient:
    """Counts REST calls, because the whole point is to make fewer of them."""

    def __init__(self, books=None, markets=None, boom=False):
        self.books = books or {}
        self.markets = markets or {}
        self.boom = boom
        self.book_calls = []
        self.market_calls = []

    def get_orderbook(self, ticker, depth=50):
        self.book_calls.append(ticker)
        if self.boom:
            raise RuntimeError("exchange unreachable")
        return self.books.get(ticker, DepthBook.empty(ticker))

    def get_market(self, ticker):
        self.market_calls.append(ticker)
        if self.boom:
            raise RuntimeError("exchange unreachable")
        return self.markets.get(ticker)


def book(ticker="T", yes=((60, 100),), no=((39, 80),)):
    return DepthBook(ticker, [Level(p, q) for p, q in yes],
                     [Level(p, q) for p, q in no])


# ------------------------- read-through -------------------------
def test_a_miss_goes_to_rest_and_is_then_cached():
    c = FakeClient({"T": book()})
    s = MarketState(c)
    assert s.get_depth("T").best_price("yes", "sell") == 60
    assert s.get_depth("T").best_price("yes", "sell") == 60
    assert c.book_calls == ["T"], "second read should have come from cache"
    assert s.stats.book_hits == 1 and s.stats.book_misses == 1


def test_a_stale_book_is_a_miss():
    c = FakeClient({"T": book()})
    s = MarketState(c, max_book_age=0.0)
    s.get_depth("T")
    s.get_depth("T")
    assert len(c.book_calls) == 2, "a stale entry must not be served"
    assert s.stats.book_stale == 1


def test_the_caller_can_demand_a_fresher_book_than_the_default():
    c = FakeClient({"T": book()})
    s = MarketState(c, max_book_age=3600)
    s.get_depth("T")
    s.get_depth("T", max_age=0.0)
    assert len(c.book_calls) == 2


def test_an_unreachable_exchange_returns_an_empty_book_not_an_exception():
    # A scan loop must survive a bad minute. An empty book fails every depth
    # check downstream, which is the safe direction.
    s = MarketState(FakeClient(boom=True))
    b = s.get_depth("T")
    assert b.best_price("yes", "sell") is None
    assert s.stats.rest_errors == 1


def test_with_no_client_it_is_a_pure_store():
    s = MarketState(None)
    assert s.get_depth("T").yes_bids == []
    s.put_book(book("T"))
    assert s.get_depth("T").best_price("yes", "sell") == 60


# ------------------------- snapshots and deltas -------------------------
def test_a_snapshot_populates_the_book():
    s = MarketState(None)
    s.apply_snapshot("T", [(60, 100), (59, 50)], [(39, 80)], seq=1)
    b = s.get_depth("T")
    assert b.best_price("yes", "sell") == 60
    assert b.total_size("yes", "sell") == 150
    assert s.stats.snapshots == 1


def test_a_delta_adds_removes_and_clears_a_level():
    s = MarketState(None)
    s.apply_snapshot("T", [(60, 100)], [], seq=1)
    assert s.apply_delta("T", "yes", 60, +50, seq=2) is True
    assert s.get_depth("T").total_size("yes", "sell") == 150
    assert s.apply_delta("T", "yes", 60, -150, seq=3) is True
    assert s.get_depth("T").best_price("yes", "sell") is None


def test_a_delta_with_no_snapshot_behind_it_is_refused():
    s = MarketState(None)
    assert s.apply_delta("T", "yes", 60, +10, seq=5) is False


def test_an_unknown_side_is_refused():
    s = MarketState(None)
    s.apply_snapshot("T", [(60, 100)], [], seq=1)
    assert s.apply_delta("T", "sideways", 60, +10, seq=2) is False


# ------------------------- the trust rules -------------------------
def test_a_sequence_gap_makes_the_book_untrusted():
    s = MarketState(None)
    s.apply_snapshot("T", [(60, 100)], [], seq=1)
    assert s.apply_delta("T", "yes", 60, +10, seq=3) is False   # 2 never arrived
    assert s.stats.gaps == 1
    assert s.entry("T").trusted is False


def test_an_untrusted_book_is_never_served_from_cache():
    # This is the whole reason the sequence check exists.
    c = FakeClient({"T": book()})
    s = MarketState(c, max_book_age=3600)
    s.apply_snapshot("T", [(60, 100)], [], seq=1)
    s.apply_delta("T", "yes", 60, +10, seq=9)      # gap
    s.get_depth("T")
    assert c.book_calls == ["T"], "an untrusted book was served as if it were fine"
    assert s.stats.book_untrusted == 1


def test_further_deltas_cannot_repair_an_untrusted_book():
    s = MarketState(None)
    s.apply_snapshot("T", [(60, 100)], [], seq=1)
    s.apply_delta("T", "yes", 60, +10, seq=9)
    for n in (10, 11, 12):
        s.apply_delta("T", "yes", 60, +1, seq=n)
    assert s.entry("T").trusted is False


def test_a_fresh_snapshot_restores_trust():
    s = MarketState(None)
    s.apply_snapshot("T", [(60, 100)], [], seq=1)
    s.apply_delta("T", "yes", 60, +10, seq=9)
    assert s.entry("T").trusted is False
    s.apply_snapshot("T", [(61, 90)], [], seq=10)
    assert s.entry("T").trusted is True
    assert s.get_depth("T").best_price("yes", "sell") == 61


def test_a_repeated_delta_after_a_reconnect_is_ignored_not_counted_twice():
    s = MarketState(None)
    s.apply_snapshot("T", [(60, 100)], [], seq=5)
    assert s.apply_delta("T", "yes", 60, +10, seq=5) is True
    assert s.get_depth("T").total_size("yes", "sell") == 100, "applied twice"
    assert s.entry("T").trusted is True


def test_a_level_going_negative_means_the_books_disagree():
    # Clamping at zero and carrying on would hide a real divergence. The
    # point of the check is not to hide it.
    s = MarketState(None)
    s.apply_snapshot("T", [(60, 100)], [], seq=1)
    assert s.apply_delta("T", "yes", 60, -150, seq=2) is False
    assert s.entry("T").trusted is False


def test_a_dropped_feed_distrusts_everything_without_forgetting_it():
    s = MarketState(None)
    s.apply_snapshot("A", [(60, 100)], [], seq=1)
    s.apply_snapshot("B", [(40, 100)], [], seq=1)
    s.invalidate_all("feed disconnected")
    assert s.entry("A").trusted is False and s.entry("B").trusted is False
    # Entries survive so a reconnect knows what to resubscribe to.
    assert len(s) == 2


def test_deltas_without_sequence_numbers_still_apply():
    # Not every channel numbers its messages. Absent a number there is nothing
    # to check, and refusing everything would be worse than trusting it.
    s = MarketState(None)
    s.apply_snapshot("T", [(60, 100)], [], seq=None)
    assert s.apply_delta("T", "yes", 60, +10, seq=None) is True
    assert s.get_depth("T").total_size("yes", "sell") == 110


# ------------------------- markets -------------------------
def test_market_records_are_cached_separately_and_live_longer():
    m = Market(ticker="T", event_ticker="E", series_ticker="S", status="active")
    c = FakeClient(markets={"T": m})
    s = MarketState(c, max_market_age=3600)
    assert s.get_market("T").ticker == "T"
    s.get_market("T")
    assert c.market_calls == ["T"]


def test_a_market_that_cannot_be_read_is_none_not_a_crash():
    s = MarketState(FakeClient(boom=True))
    assert s.get_market("T") is None


# ------------------------- housekeeping -------------------------
def test_the_store_stays_bounded():
    # 80,000 markets exist. An unbounded cache in a process that runs for days
    # is a memory leak with extra steps.
    s = MarketState(None, capacity=10)
    for i in range(50):
        s.apply_snapshot(f"T{i}", [(50, 10)], [], seq=1)
    assert len(s) == 10
    assert s.stats.evictions == 40
    assert s.entry("T49") is not None and s.entry("T0") is None


def test_reads_keep_an_entry_alive():
    s = MarketState(None, capacity=3)
    for t in ("A", "B", "C"):
        s.apply_snapshot(t, [(50, 10)], [], seq=1)
    s.get_depth("A")                      # A is now the most recently used
    s.apply_snapshot("D", [(50, 10)], [], seq=1)
    assert s.entry("A") is not None
    assert s.entry("B") is None


def test_health_reports_what_a_dashboard_needs():
    s = MarketState(None)
    s.apply_snapshot("A", [(60, 10)], [], seq=1)
    s.apply_snapshot("B", [(60, 10)], [], seq=1)
    s.invalidate("B", "test")
    h = s.health()
    assert h["books"] == 2
    assert h["untrusted"] == 1
    assert "B" in h["untrusted_tickers"]


def test_concurrent_readers_and_writers_do_not_corrupt_the_book():
    # The scan loop and the position worker read this while the feed writes it.
    s = MarketState(None)
    s.apply_snapshot("T", [(60, 1000)], [], seq=0)
    errors = []

    def writer():
        try:
            for n in range(1, 401):
                s.apply_delta("T", "yes", 60, +1 if n % 2 else -1, seq=n)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def reader():
        try:
            for _ in range(400):
                s.get_depth("T").total_size("yes", "sell")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer)] + [
        threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert s.entry("T").trusted is True
    assert s.get_depth("T").total_size("yes", "sell") == 1000
