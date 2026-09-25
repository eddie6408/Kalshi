"""The shared state standing in for the market client, over real HTTP.

The store replaces the market client everywhere books and markets are read, so
the thing worth proving is not that it caches - it is that a full scan behaves
identically with it in the way, and makes fewer calls doing it.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ["KALSHIBOT_ALLOW_LOOPBACK_FOR_TESTS"] = "1"

from mock_exchange import MockServer, seeded_exchange  # noqa: E402

from kalshibot.arb_engine import ArbitrageEngine, TradingMode  # noqa: E402
from kalshibot.clients import MarketDataClient, PortfolioClient  # noqa: E402
from kalshibot.fees import FeeModel  # noqa: E402
from kalshibot.journal import Journal  # noqa: E402
from kalshibot.market_state import MarketState  # noqa: E402
from test_integration_demo import Settings, _Signer  # noqa: E402


class CountingClient(MarketDataClient):
    """A real client that keeps score.

    It counts `get_depth`, which is the method that actually reaches the
    network - a scan calls it through the batched `get_depths`. Counting the
    wrong method reports a perfect cache hit rate on a client that is doing
    all the same work.
    """

    def __init__(self, url):
        super().__init__(url)
        self.book_calls = 0
        self.market_calls = 0

    def get_depth(self, ticker, depth=50):
        self.book_calls += 1
        return super().get_depth(ticker, depth=depth)

    def get_market(self, ticker):
        self.market_calls += 1
        return super().get_market(ticker)


def _engine(url, source, tmp):
    return ArbitrageEngine(
        Settings(url, tmpdir=tmp), source,
        PortfolioClient(url, _Signer()), None,
        FeeModel({"safety_multiplier": 1.0}),
        Journal(Path(tmp) / "j.db"), TradingMode.SCAN_ONLY)


def _two_scans(use_state, max_book_age=3600.0):
    exchange = seeded_exchange(with_arbitrage=True)
    with MockServer(exchange) as url, tempfile.TemporaryDirectory() as tmp:
        client = CountingClient(url)
        source = MarketState(client, max_book_age=max_book_age) if use_state else client
        engine = _engine(url, source, tmp)
        first = engine.scan_once()
        second = engine.scan_once()
        return first, second, client, source


# ------------------------------------------------------------------
def test_a_scan_through_the_state_finds_what_a_scan_without_it_finds():
    plain, _, _, _ = _two_scans(use_state=False)
    cached, _, _, _ = _two_scans(use_state=True)
    assert cached.qualifying == plain.qualifying
    assert cached.discovery.markets_seen == plain.discovery.markets_seen
    assert ([o.market_tickers for o in cached.accepted]
            == [o.market_tickers for o in plain.accepted])


def test_the_second_scan_reads_far_fewer_books():
    # The point of the whole exercise: a sweep should not re-read a book that
    # has not moved.
    _, _, plain_client, _ = _two_scans(use_state=False)
    _, _, cached_client, state = _two_scans(use_state=True)
    assert cached_client.book_calls < plain_client.book_calls, (
        f"cached {cached_client.book_calls} vs plain {plain_client.book_calls}")
    assert state.stats.book_hits > 0


def test_a_stale_cache_still_goes_back_to_the_exchange():
    # With a zero-second limit nothing is served from cache, and the scan must
    # still produce the same answer.
    plain, _, _, _ = _two_scans(use_state=False)
    cached, _, _, state = _two_scans(use_state=True, max_book_age=0.0)
    assert state.stats.book_hits == 0
    assert cached.qualifying == plain.qualifying


def test_an_untrusted_book_is_refetched_during_a_scan():
    exchange = seeded_exchange(with_arbitrage=True)
    with MockServer(exchange) as url, tempfile.TemporaryDirectory() as tmp:
        client = CountingClient(url)
        state = MarketState(client, max_book_age=3600)
        engine = _engine(url, state, tmp)
        engine.scan_once()
        after_first = client.book_calls
        engine.scan_once()
        assert client.book_calls == after_first, "expected a fully cached scan"

        state.invalidate_all("pretend the feed dropped")
        engine.scan_once()
        assert client.book_calls > after_first, (
            "a scan reused books the store had marked untrusted")


def test_the_state_forwards_everything_it_does_not_cache():
    # exchange_status, get_markets and the rest still have to work, or the
    # store cannot stand in for the client at all.
    exchange = seeded_exchange()
    with MockServer(exchange) as url:
        state = MarketState(MarketDataClient(url))
        assert isinstance(state.exchange_status(), dict)
        assert hasattr(state, "get_markets")
