"""Reading the real order book, without being able to trade on it.

Demo books are synthetic, so every fill-quality number this bot has measured
was measured against a fiction. Kalshi publishes market data without
authentication, so the real book can be read with no API key, no signature and
no account - there is no credential to leak because there is no credential.

What these tests pin is that it STAYS that way: read-only, GET-only, no path
that can touch an account, and never paired with a mode that sends orders.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from kalshibot.env_guard import (ProductionBlocked, assert_demo_url,
                                 assert_live_books_are_paper_only,
                                 assert_public_market_data)
from kalshibot.livebooks import (LIVE_MARKET_DATA_BASE, LiveBookMarketClient,
                                 LiveBookReader)

PROD = LIVE_MARKET_DATA_BASE


# ---------------- what may be read from production ----------------
def test_public_market_data_may_be_read():
    for path in ("/markets",
                 "/markets/KXHIGHNY-26SEP25-T80",
                 "/markets/KXHIGHNY-26SEP25-T80/orderbook",
                 "/events",
                 "/series"):
        assert_public_market_data(PROD, path, "GET")


def test_nothing_that_touches_an_account_may_be_read():
    for path in ("/portfolio/positions",
                 "/portfolio/balance",
                 "/portfolio/orders",
                 "/portfolio/events/orders",
                 "/portfolio/fills",
                 "/markets/../portfolio/balance"):
        with pytest.raises(ProductionBlocked):
            assert_public_market_data(PROD, path, "GET")


def test_only_get_is_permitted():
    """An order is a POST. There is no method here that could carry one."""
    for method in ("POST", "PUT", "PATCH", "DELETE", "post"):
        with pytest.raises(ProductionBlocked):
            assert_public_market_data(PROD, "/markets", method)


def test_the_trading_lockout_is_untouched():
    """Every order path still calls assert_demo_url, and it still refuses
    production unconditionally. The read exemption did not widen it."""
    with pytest.raises(ProductionBlocked):
        assert_demo_url(PROD, "order")


# ---------------- live books may only be paper ----------------
def test_live_books_require_paper_mode():
    with pytest.raises(ProductionBlocked):
        assert_live_books_are_paper_only("DEMO_LIVE", "live")


def test_live_books_are_fine_with_paper():
    assert_live_books_are_paper_only("PAPER", "live")


def test_demo_books_are_fine_with_any_mode():
    assert_live_books_are_paper_only("DEMO_LIVE", "demo")
    assert_live_books_are_paper_only("PAPER", "")


# ---------------- the reader cannot order ----------------
def test_the_reader_has_no_way_to_send_an_order():
    r = LiveBookReader()
    for forbidden in ("place_order", "place_demo_order", "cancel_order",
                      "post", "create_order"):
        assert not hasattr(r, forbidden)


def test_the_reader_carries_no_authentication():
    r = LiveBookReader()
    headers = {k.lower() for k in r._session.headers}
    for auth_header in ("authorization", "kalshi-access-key",
                        "kalshi-access-signature", "kalshi-access-timestamp"):
        assert auth_header not in headers


# ---------------- the book is parsed correctly ----------------
def test_a_no_bid_becomes_the_mirrored_yes_ask():
    """Someone bidding 55c for NO is offering YES at 45c. Getting this mirror
    wrong would invent liquidity that is not there."""
    b = LiveBookReader._to_book("T", {"yes": [[40, 100], [39, 250]],
                                      "no": [[55, 80], [54, 300]]})
    assert [(l.price_cents, l.contracts) for l in b.yes_bids] == [(40, 100), (39, 250)]
    assert [(l.price_cents, l.contracts) for l in b.yes_asks] == [(45, 80), (46, 300)]
    assert b.best_price("yes", "buy") == 45
    assert b.best_price("yes", "sell") == 40


def test_junk_levels_are_dropped_rather_than_trusted():
    b = LiveBookReader._to_book("T", {"yes": [[40, 100], [0, 5], [150, 5],
                                              ["x", 1], [45, 0]],
                                      "no": [[55, 80]]})
    assert [(l.price_cents, l.contracts) for l in b.yes_bids] == [(40, 100)]


def test_an_empty_book_is_none_not_an_empty_book():
    assert LiveBookReader._to_book("T", {"yes": [], "no": []}) is None


# ---------------- the adapter ----------------
class FakeDemo:
    def __init__(self):
        self.asked = []

    def get_depth(self, ticker, depth=50):
        self.asked.append(ticker)
        return "THE DEMO BOOK"

    def get_markets(self, **kw):
        return ["passed through"]


class FakeReader:
    def __init__(self, books): self.books = books
    def orderbook(self, ticker, depth=50): return self.books.get(ticker)


def test_a_market_production_does_not_list_returns_no_book():
    """NOT the demo book. A fallback would put the fiction straight back into
    the measurement this whole exercise exists to remove."""
    demo = FakeDemo()
    c = LiveBookMarketClient(demo, FakeReader({}))
    assert c.get_depth("T-MISSING") is None
    assert demo.asked == []              # the demo book was never consulted
    assert c.misses == 1


def test_a_live_book_is_returned_when_production_has_one():
    c = LiveBookMarketClient(FakeDemo(), FakeReader({"T-1": "THE LIVE BOOK"}))
    assert c.get_depth("T-1") == "THE LIVE BOOK"
    assert c.hits == 1


def test_everything_else_passes_through_to_demo():
    c = LiveBookMarketClient(FakeDemo(), FakeReader({}))
    assert c.get_markets() == ["passed through"]


def test_the_miss_rate_is_reported():
    c = LiveBookMarketClient(FakeDemo(), FakeReader({"T-1": "B"}))
    c.get_depth("T-1"); c.get_depth("T-2")
    assert "1 read" in c.summary() and "1 not listed" in c.summary()


class _PayloadSession:
    """Serves one orderbook payload to LiveBookReader._get."""

    def __init__(self, payload):
        self.payload, self.headers = payload, {}

    def get(self, url, params=None, timeout=None):
        payload = self.payload

        class R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return payload
        return R()


def test_the_current_orderbook_fp_format_is_read():
    """Production moved to orderbook_fp with dollar strings. Reading only the
    legacy shape made every live book look empty and silenced the execution
    probe for days (26 September). This is the payload production returned."""
    from kalshibot.livebooks import LiveBookReader
    r = LiveBookReader()
    r._session = _PayloadSession({"orderbook_fp": {
        "no_dollars": [["0.9500", "20.00"], ["0.9900", "8667.47"]],
        "yes_dollars": []}})
    book = r.orderbook("KXHIGHNY-26SEP26-T69")
    assert book is not None
    assert book.best_price("yes", "buy") == 1          # 99c NO bid -> 1c YES ask
    assert book.best_price("no", "sell") == 99


def test_a_quiet_current_format_book_is_still_none():
    from kalshibot.livebooks import LiveBookReader
    r = LiveBookReader()
    r._session = _PayloadSession({"orderbook_fp": {"no_dollars": [], "yes_dollars": []}})
    assert r.orderbook("KXHIGHNY-26SEP26-T69") is None
