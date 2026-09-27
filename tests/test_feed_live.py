"""Phase C: making the WebSocket feed actually raise the cache hit rate.

Two things stood between turning the feed on and it doing any good:

  1. A book the feed maintains was still treated as stale after 20 seconds.
     Most demo markets are quiet, so they went back to REST every scan and
     the hit rate could not move (it ran 0-1% for two days with the feed off).
     While the connection is up, a quiet fed book IS current - the exchange
     would have sent a delta if it had changed.

  2. Subscriptions only ever grew. Once the 500 slots were full - one
     arbitrage sweep could do it - nothing new was followed again, including
     the next day's weather markets.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kalshibot.market_state import MarketState

from test_market_state import FakeClient, book
from test_wsfeed import feed


def _aged(state, ticker, seconds):
    from datetime import timedelta
    state._books[ticker].updated_at -= timedelta(seconds=seconds)


def _fed_state(live=True):
    c = FakeClient({"T": book()})
    s = MarketState(c, max_book_age=20)
    s.apply_snapshot("T", [(60, 100)], [(39, 80)], seq=1)
    s.subscribed.add("T")
    s.feed_live = lambda: live
    return c, s


# ------------------------------------------------------------- freshness
def test_a_quiet_book_the_live_feed_maintains_is_served():
    c, s = _fed_state(live=True)
    _aged(s, "T", 600)                          # ten quiet minutes
    s.get_depth("T")
    assert c.book_calls == [] and s.stats.book_hits == 1


def test_the_same_book_goes_to_rest_when_the_feed_is_down():
    c, s = _fed_state(live=False)
    _aged(s, "T", 600)
    s.get_depth("T")
    assert c.book_calls == ["T"]


def test_a_book_the_feed_does_not_follow_ages_out_as_before():
    c, s = _fed_state(live=True)
    s.subscribed.discard("T")
    _aged(s, "T", 600)
    s.get_depth("T")
    assert c.book_calls == ["T"]


def test_a_rest_book_is_never_treated_as_fed():
    c = FakeClient({"R": book("R")})
    s = MarketState(c, max_book_age=20)
    s.get_depth("R")
    s.subscribed.add("R")
    s.feed_live = lambda: True
    _aged(s, "R", 600)
    s.get_depth("R")
    assert c.book_calls == ["R", "R"]


def test_there_is_still_a_ceiling_on_a_fed_book():
    c, s = _fed_state(live=True)
    s.max_fed_age = 3600
    _aged(s, "T", 7200)
    s.get_depth("T")
    assert c.book_calls == ["T"]


def test_a_caller_demanding_a_fresh_book_still_gets_rest():
    c, s = _fed_state(live=True)
    s.get_depth("T", max_age=0.0)
    assert c.book_calls == ["T"]


def test_batch_reads_serve_live_fed_books_too():
    c, s = _fed_state(live=True)
    _aged(s, "T", 600)
    out = s.get_depths(["T"])
    assert "T" in out and c.book_calls == []


def test_the_feed_tells_the_store_when_it_is_live():
    s = MarketState(None)
    f = feed(s)
    assert s.feed_live() is False                # not connected yet
    f.ws = object()
    assert s.feed_live() is True


# ----------------------------------------------------------- rollover
def test_subscriptions_roll_over_to_the_current_working_set():
    s = MarketState(None)
    f = feed(s, max_subscriptions=3, rollover_seconds=0)
    f.follow(["A", "B", "C"])
    assert f._wanted == {"A", "B", "C"}
    f.follow(["D", "E", "A"])                    # tomorrow's markets
    assert f._wanted == {"D", "E", "A"}
    assert s.subscribed == {"D", "E", "A"}


def test_rollover_is_rate_limited():
    s = MarketState(None)
    f = feed(s, max_subscriptions=2, rollover_seconds=3600)
    f._last_rollover = time.monotonic()
    f.follow(["A", "B"])
    f.follow(["C", "D"])
    assert f._wanted == {"A", "B"}               # full, and too soon to roll


def test_with_room_new_markets_are_simply_added():
    s = MarketState(None)
    f = feed(s, max_subscriptions=10)
    f.follow(["A"])
    f.follow(["B", "A"])
    assert f._wanted == {"A", "B"}
