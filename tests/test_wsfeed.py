"""Turning Kalshi's messages into a book we would trade on.

The dangerous failure here is not a crash. It is a book that parses cleanly
and is wrong - because the price scale was misread, or a delta was applied to
a book that had already missed one. That book prices a trade wrongly, sizes it
wrongly, and reports a profit that was never there.

So most of these tests are about the feed REFUSING things.
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

# The mock exchange runs on loopback. This flag can never unlock a public
# host: the production denylist is checked first and unconditionally.
os.environ["KALSHIBOT_ALLOW_LOOPBACK_FOR_TESTS"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kalshibot.env_guard import ProductionBlocked
from kalshibot.market_state import MarketState
from kalshibot.wsfeed import (DEFAULT_DEMO_WS, FeedConfig, KalshiFeed,
                              cents_from_dollars, count_from_fp)
from ws_server import MiniWSServer, text_frame


def feed(state=None, **cfg):
    base = {"enabled": True}
    base.update(cfg)
    return KalshiFeed(state if state is not None else MarketState(None),
                      cfg=FeedConfig.from_dict(base))


def snapshot(ticker="T", yes=(("0.6000", "100.00"),), no=(("0.3900", "80.00"),),
             seq=1, sid=2):
    return json.dumps({"type": "orderbook_snapshot", "sid": sid, "seq": seq,
                       "msg": {"market_ticker": ticker,
                               "yes_dollars_fp": [list(r) for r in yes],
                               "no_dollars_fp": [list(r) for r in no]}})


def delta(ticker="T", price="0.6000", amount="-54.00", side="yes", seq=2, sid=2):
    return json.dumps({"type": "orderbook_delta", "sid": sid, "seq": seq,
                       "msg": {"market_ticker": ticker, "price_dollars": price,
                               "delta_fp": amount, "side": side}})


# ------------------------- units -------------------------
def test_dollar_strings_become_cents():
    # Same migration that moved orders to "0.4300" and positions to
    # position_fp. Reading these as cents would put every price 100x wrong.
    assert cents_from_dollars("0.9600") == 96
    assert cents_from_dollars("0.0800") == 8
    assert cents_from_dollars("1.0000") == 100


def test_fixed_point_counts_become_integers_and_keep_their_sign():
    assert count_from_fp("300.00") == 300
    assert count_from_fp("-54.00") == -54


def test_an_unreadable_number_is_none_rather_than_a_guess():
    assert cents_from_dollars("n/a") is None
    assert cents_from_dollars(None) is None
    assert count_from_fp({}) is None


# ------------------------- snapshots -------------------------
def test_a_snapshot_becomes_a_book():
    s = MarketState(None)
    feed(s)._handle(snapshot())
    b = s.get_depth("T")
    assert b.best_price("yes", "sell") == 60
    assert b.total_size("yes", "sell") == 100
    assert b.best_price("no", "sell") == 39


def test_the_older_cents_spelling_is_still_read():
    # Reading only the current shape against an older deployment gives an
    # empty book for every market, which looks exactly like a dead exchange.
    s = MarketState(None)
    feed(s)._handle(json.dumps({
        "type": "orderbook_snapshot", "seq": 1,
        "msg": {"market_ticker": "T", "yes": [[60, 100]], "no": [[39, 80]]}}))
    assert s.get_depth("T").best_price("yes", "sell") == 60


def test_a_snapshot_with_no_ticker_is_counted_not_crashed_on():
    f = feed()
    f._handle(json.dumps({"type": "orderbook_snapshot", "seq": 1, "msg": {}}))
    assert f.stats.unparsed == 1


def test_prices_outside_the_tradeable_range_are_dropped():
    s = MarketState(None)
    feed(s)._handle(snapshot(yes=(("0.0000", "5.00"), ("1.0000", "5.00"),
                                  ("0.6000", "100.00"))))
    assert s.get_depth("T").total_size("yes", "sell") == 100


# ------------------------- deltas -------------------------
def test_a_delta_moves_the_book():
    s = MarketState(None)
    f = feed(s)
    f._handle(snapshot(seq=1))
    f._handle(delta(amount="+40.00", seq=2))
    assert s.get_depth("T").total_size("yes", "sell") == 140
    assert f.stats.deltas == 1


def test_a_delta_can_empty_a_level():
    s = MarketState(None)
    f = feed(s)
    f._handle(snapshot(seq=1))
    f._handle(delta(amount="-100.00", seq=2))
    assert s.get_depth("T").best_price("yes", "sell") is None


class _FakeWS:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_many_markets_share_one_subscription_counter():
    """The production case that broke on 27 September: Kalshi numbers
    messages per subscription, so updates to different markets on one sid
    interleave. None of this is a gap."""
    s = MarketState(None)
    f = feed(s)
    f._handle(snapshot(ticker="A", seq=1, sid=2))
    f._handle(snapshot(ticker="B", seq=2, sid=2))
    f._handle(delta(ticker="B", amount="+10.00", seq=3, sid=2))
    f._handle(delta(ticker="A", amount="+40.00", seq=4, sid=2))
    assert f.stats.gaps == 0 and f.stats.deltas == 2
    assert s.entry("A").trusted and s.entry("B").trusted
    assert s.get_depth("A").total_size("yes", "sell") == 140
    assert not f._pending_resnapshot


def test_the_ok_for_added_markets_takes_a_sequence_number():
    """Captured live on 27 September: adding markets to a live subscription
    is acknowledged with an "ok" that uses the next number on that sid."""
    s = MarketState(None)
    f = feed(s)
    f._handle(snapshot(ticker="A", seq=1, sid=1))
    f._handle(json.dumps({"type": "ok", "id": 3, "sid": 1, "seq": 2}))
    f._handle(snapshot(ticker="B", seq=3, sid=1))
    f._handle(delta(ticker="B", amount="+5.00", seq=4, sid=1))
    assert f.stats.gaps == 0 and f.stats.deltas == 1
    assert s.entry("A").trusted and s.entry("B").trusted


def test_a_reconnect_logs_one_line_not_one_per_book():
    import logging
    s = MarketState(None)
    for t in ("A", "B", "C"):
        s.apply_snapshot(t, [(60, 1)], [], seq=None)
    records = []
    h = logging.Handler()
    h.emit = records.append
    logging.getLogger("kalshibot.market_state").addHandler(h)
    try:
        s.invalidate_all("test")
    finally:
        logging.getLogger("kalshibot.market_state").removeHandler(h)
    assert len(records) == 1 and "3 cached books" in records[0].getMessage()
    assert not any(s.entry(t).trusted for t in ("A", "B", "C"))


def test_subscriptions_count_independently():
    s = MarketState(None)
    f = feed(s)
    f._handle(snapshot(ticker="A", seq=1, sid=2))
    f._handle(snapshot(ticker="B", seq=1, sid=3))
    f._handle(delta(ticker="A", seq=2, sid=2))
    f._handle(delta(ticker="B", seq=2, sid=3))
    assert f.stats.gaps == 0 and f.stats.deltas == 2


def test_a_repeat_is_ignored_not_applied_twice():
    s = MarketState(None)
    f = feed(s)
    f._handle(snapshot(seq=1))
    f._handle(delta(amount="+40.00", seq=2))
    f._handle(delta(amount="+40.00", seq=2))        # the same message again
    assert s.get_depth("T").total_size("yes", "sell") == 140
    assert f.stats.gaps == 0


def test_a_real_gap_distrusts_everything_and_reconnects():
    s = MarketState(None)
    f = feed(s)
    f._handle(snapshot(ticker="A", seq=1, sid=2))
    f._handle(snapshot(ticker="B", seq=2, sid=2))
    ws = f.ws = _FakeWS()
    f._handle(delta(ticker="A", seq=9, sid=2))      # 3..8 never arrived
    assert f.stats.gaps == 1 and f.stats.deltas == 0
    assert s.entry("A").trusted is False and s.entry("B").trusted is False
    assert ws.closed and f.ws is None               # run_forever reconnects


def test_after_a_reconnect_fresh_snapshots_restore_trust():
    s = MarketState(None)
    f = feed(s)
    f._handle(snapshot(seq=1, sid=2))
    f._handle(delta(seq=7, sid=2))
    f._sid_seq.clear()                              # what _connect does
    f._handle(snapshot(yes=(("0.6100", "10.00"),), seq=1, sid=5))
    assert s.entry("T").trusted is True
    assert s.get_depth("T").best_price("yes", "sell") == 61


def test_the_resnapshot_request_is_actually_sent_and_not_repeated():
    f = feed()
    sent = []
    f._send = lambda payload: sent.append(payload)
    f._handle(delta(ticker="UNSEEN", seq=5))
    f._request_resnapshots()
    f._handle(delta(ticker="UNSEEN", seq=6))
    f._request_resnapshots()
    assert len(sent) == 1 and sent[0]["params"]["market_tickers"] == ["UNSEEN"]
    assert f.stats.resnapshots == 1


def test_a_delta_for_a_market_we_have_no_snapshot_for_asks_for_one():
    f = feed()
    f._handle(delta(ticker="UNSEEN", seq=5))
    assert "UNSEEN" in f._pending_resnapshot


def test_an_unusable_delta_is_counted_not_applied():
    f = feed()
    f._handle(json.dumps({"type": "orderbook_delta", "seq": 2,
                          "msg": {"market_ticker": "T", "side": "yes"}}))
    assert f.stats.unparsed == 1


# ------------------------- other messages -------------------------
def test_junk_does_not_take_the_feed_down():
    f = feed()
    for junk in ("", "not json", "[]", '{"type":"something_new"}', "null"):
        f._handle(junk)
    assert f.stats.messages == 0     # _handle alone does not count messages


def test_an_error_message_is_recorded_and_surfaced():
    f = feed()
    f._handle(json.dumps({"id": 1, "type": "error",
                          "msg": {"code": 7, "msg": "Unknown subscription ID"}}))
    assert f.stats.errors == 1
    assert "Unknown subscription" in f.stats.last_error


# ------------------------- subscriptions -------------------------
def test_following_is_capped_so_one_scan_cannot_subscribe_to_the_exchange():
    f = feed(max_subscriptions=3)
    f.follow([f"T{i}" for i in range(100)])
    assert len(f._wanted) == 3


def test_following_the_same_ticker_twice_is_harmless():
    f = feed()
    f.follow(["A", "B"])
    f.follow(["B", "C"])
    assert f._wanted == {"A", "B", "C"}


def test_what_we_follow_is_visible_to_the_state():
    s = MarketState(None)
    f = feed(s)
    f.follow(["A", "B"])
    assert s.subscribed == {"A", "B"}


# ------------------------- the lockout -------------------------
def test_the_feed_may_not_point_at_production():
    # A market-data socket pointed at production is still production.
    f = feed(url="wss://external-api-ws.kalshi.com/trade-api/ws/v2")
    try:
        f._connect()
        raise AssertionError("connected a production websocket")
    except ProductionBlocked:
        pass


def test_the_production_lockout_is_never_retried():
    # A retry loop against the lockout is a retry loop against production.
    f = feed(url="wss://external-api-ws.kalshi.com/trade-api/ws/v2",
             reconnect_min=0.01)
    stop = threading.Event()
    try:
        f.run_forever(stop)
        raise AssertionError("run_forever swallowed the lockout")
    except ProductionBlocked:
        pass


def test_the_url_itself_is_what_gets_checked():
    """assert_demo_url(URL, description) - not (env, URL).

    With the arguments the wrong way round the guard checks the string "demo"
    as if it were a hostname. Everything is refused, including the real demo
    URL, and the guard looks like it is working. It is not: the actual URL is
    never examined at all.
    """
    s = MarketState(None)
    server = MiniWSServer([]).start()
    try:
        f = feed(s, url=server.url, recv_timeout=0.2)
        f._connect()          # a permitted loopback URL must be allowed
        assert f.ws is not None
        f._drop("done")
    finally:
        server.stop()


def test_the_default_url_is_the_demo_one():
    assert "demo" in DEFAULT_DEMO_WS
    assert FeedConfig.from_dict({}).url == DEFAULT_DEMO_WS


def test_a_disabled_feed_does_nothing_at_all():
    f = feed(enabled=False)
    f.run_forever(threading.Event())        # returns immediately
    assert f.stats.connects == 0


# ------------------------- connection lifecycle -------------------------
def test_losing_the_feed_distrusts_every_cached_book():
    # The books are not wrong yet. Nothing will tell us when they become
    # wrong, which is the same thing.
    s = MarketState(None)
    f = feed(s)
    f._handle(snapshot(ticker="A", seq=1))
    f._handle(snapshot(ticker="B", seq=2))   # one counter per subscription
    f.ws = object()
    f._drop("test")
    assert s.entry("A").trusted is False and s.entry("B").trusted is False


def test_it_connects_subscribes_and_fills_the_book_for_real():
    s = MarketState(None)
    server = MiniWSServer([]).start()
    try:
        f = feed(s, url=server.url, recv_timeout=0.3, idle_timeout=30)
        f.follow(["KXHIGHNY-26SEP18-T80"])
        stop = threading.Event()
        t = threading.Thread(target=f.run_forever, args=(stop,), daemon=True)
        t.start()

        end = time.time() + 5
        while time.time() < end and not server.received:
            time.sleep(0.02)
        assert server.received, "never sent a subscribe"
        sub = json.loads(server.received[0])
        assert sub["cmd"] == "subscribe"
        assert sub["params"]["channels"] == ["orderbook_delta"]
        assert sub["params"]["market_tickers"] == ["KXHIGHNY-26SEP18-T80"]

        server.send(text_frame(snapshot(ticker="KXHIGHNY-26SEP18-T80",
                                        yes=(("0.2000", "500.00"),), seq=1)))
        end = time.time() + 5
        while time.time() < end and not s.entry("KXHIGHNY-26SEP18-T80"):
            time.sleep(0.02)
        book = s.get_depth("KXHIGHNY-26SEP18-T80")
        assert book.best_price("yes", "sell") == 20
        assert book.total_size("yes", "sell") == 500

        server.send(text_frame(delta(ticker="KXHIGHNY-26SEP18-T80",
                                     price="0.2000", amount="-200.00", seq=2)))
        end = time.time() + 5
        while (time.time() < end
               and s.get_depth("KXHIGHNY-26SEP18-T80").total_size("yes", "sell") != 300):
            time.sleep(0.02)
        assert s.get_depth("KXHIGHNY-26SEP18-T80").total_size("yes", "sell") == 300
        stop.set()
        t.join(timeout=5)
    finally:
        server.stop()
