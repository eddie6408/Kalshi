"""Going straight to the markets a forecast strategy can price.

From the first live run's journal:

    scan 36: markets_seen 80000, priced 426, seconds 10107

Two hours and forty-eight minutes, of which twenty minutes was paging eighty
thousand markets to reach four hundred whose tickers the strategy already knew.
A forecast strategy can name its six series; making it wait on a sweep that
cannot is what these tests exist to prevent coming back.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ["KALSHIBOT_ALLOW_LOOPBACK_FOR_TESTS"] = "1"

from mock_exchange import MockServer, market, seeded_exchange  # noqa: E402

from kalshibot.arb_engine import ArbitrageEngine, TradingMode  # noqa: E402
from kalshibot.clients import MarketDataClient, PortfolioClient  # noqa: E402
from kalshibot.discovery import MarketDiscovery  # noqa: E402
from kalshibot.fees import FeeModel  # noqa: E402
from kalshibot.journal import Journal  # noqa: E402
from kalshibot.market_state import MarketState  # noqa: E402
from test_integration_demo import Settings, _Signer  # noqa: E402


class FakeForecast:
    """A strategy that knows exactly which series it can price."""
    name = "fake_forecast"

    def __init__(self, series=("MOCK",), found=()):
        self.series = list(series)
        self.found = list(found)
        self.skips = []
        self.seen_events = []

    def candidate_series(self):
        return self.series

    def find(self, markets, books, bankroll_usd):
        self.seen_events.append(len(markets))
        return list(self.found)


def _exchange_with_two_series():
    st = seeded_exchange(with_arbitrage=True)
    # A second series the forecast strategy has no opinion about.
    st.add_event("OTHEREVENT", mutually_exclusive=False)
    for i in range(3):
        st.add_market(market(f"OTHER-{i}", "OTHEREVENT", yes_bid=40, yes_ask=41,
                             series="OTHERSERIES"),
                      yes_levels=[[40, 300]], no_levels=[[58, 300]])
    return st


def _engine(url, tmp, forecasts=()):
    state = MarketState(MarketDataClient(url), max_book_age=0.0)
    engine = ArbitrageEngine(
        Settings(url, tmpdir=tmp), state, PortfolioClient(url, _Signer()),
        None, FeeModel({"safety_multiplier": 1.0}),
        Journal(Path(tmp) / "j.db"), TradingMode.SCAN_ONLY)
    engine.forecasts = list(forecasts)
    return engine


# ------------------------- discovery -------------------------
class RecordingClient(MarketDataClient):
    """Remembers which series were asked for."""

    def __init__(self, url):
        super().__init__(url)
        self.asked_series = []

    def get_markets(self, status="open", series_ticker=None, max_pages=60):
        self.asked_series.append(series_ticker)
        return super().get_markets(status=status, series_ticker=series_ticker,
                                   max_pages=max_pages)


def test_discover_series_asks_the_exchange_to_filter():
    st = _exchange_with_two_series()
    with MockServer(st) as url:
        client = RecordingClient(url)
        d = MarketDiscovery(client,
                            {"min_minutes_to_close": 1, "max_hours_to_close": 999,
                             "require_quotes": False})
        events, stats = d.discover_series(["MOCK"])
        assert events, "found no events for a series that exists"
        assert all(ev.series_ticker in ("MOCK", "") for ev in events)
        # The filter is sent to the exchange, not applied after the fact.
        assert client.asked_series == ["MOCK"]
        assert None not in client.asked_series, "swept the whole board anyway"


def test_discover_series_reads_far_less_than_a_full_sweep():
    st = _exchange_with_two_series()
    with MockServer(st) as url:
        cfg = {"min_minutes_to_close": 1, "max_hours_to_close": 999,
               "require_quotes": False}
        d = MarketDiscovery(MarketDataClient(url), cfg)
        _, focused = d.discover_series(["MOCK"])
        _, full = d.discover()
        assert focused.markets_seen < full.markets_seen, (
            f"focused saw {focused.markets_seen}, full saw {full.markets_seen}")


def test_one_bad_series_does_not_cost_us_the_others():
    st = _exchange_with_two_series()
    with MockServer(st) as url:
        class Flaky(MarketDataClient):
            def get_markets(self, status="open", series_ticker=None, max_pages=60):
                if series_ticker == "BROKEN":
                    raise RuntimeError("exchange said no")
                return super().get_markets(status=status,
                                           series_ticker=series_ticker,
                                           max_pages=max_pages)
        d = MarketDiscovery(Flaky(url), {"min_minutes_to_close": 1,
                                         "max_hours_to_close": 999,
                                         "require_quotes": False})
        events, _ = d.discover_series(["BROKEN", "MOCK"])
        assert events, "one failing series took the whole pass down"


def test_no_series_is_an_empty_answer_not_a_full_sweep():
    st = _exchange_with_two_series()
    with MockServer(st) as url:
        d = MarketDiscovery(MarketDataClient(url), {})
        events, stats = d.discover_series([])
        assert events == [] and stats.markets_seen == 0
        assert st.requests == [], "asked the exchange for anything at all"


# ------------------------- the focused scan -------------------------
def test_a_focused_scan_prices_only_the_series_it_named():
    st = _exchange_with_two_series()
    with MockServer(st) as url, tempfile.TemporaryDirectory() as tmp:
        fc = FakeForecast(series=["MOCK"])
        engine = _engine(url, tmp, [fc])
        report = engine.scan_focused()
        assert report.events_inspected > 0
        assert fc.seen_events, "the strategy was never given anything to price"
        books = [r for r in st.requests if "orderbook" in str(r)]
        assert books, "no order books were read"
        assert not any("OTHER-" in str(r) for r in books), (
            "read books for a series the strategy cannot price")


def test_a_focused_scan_with_no_forecast_strategies_does_nothing():
    st = _exchange_with_two_series()
    with MockServer(st) as url, tempfile.TemporaryDirectory() as tmp:
        engine = _engine(url, tmp, [])
        report = engine.scan_focused()
        assert report.events_inspected == 0
        assert st.requests == []


def test_a_strategy_that_raises_does_not_take_the_pass_down():
    class Exploding(FakeForecast):
        def find(self, markets, books, bankroll_usd):
            raise RuntimeError("open-meteo timed out")
    st = _exchange_with_two_series()
    with MockServer(st) as url, tempfile.TemporaryDirectory() as tmp:
        engine = _engine(url, tmp, [Exploding()])
        report = engine.scan_focused()       # must not raise
        assert report.forecast_errors > 0


def test_a_suspended_strategy_is_not_asked_for_opportunities():
    st = _exchange_with_two_series()
    with MockServer(st) as url, tempfile.TemporaryDirectory() as tmp:
        fc = FakeForecast()
        engine = _engine(url, tmp, [fc])
        engine._suspended = {"fake_forecast"}
        # _refresh_suspensions recomputes, so force the gate to say no.
        engine.calibration.may_enter = lambda name: (False, "DEGRADED: test")
        report = engine.scan_focused()
        assert fc.seen_events == [], "a suspended strategy was still consulted"
        assert "fake_forecast" in report.suspended


def test_the_focused_scan_skips_tickers_the_position_worker_is_selling():
    # Two things trading one ticker at once is how a limit gets bypassed.
    st = _exchange_with_two_series()
    with MockServer(st) as url, tempfile.TemporaryDirectory() as tmp:
        fc = FakeForecast()
        engine = _engine(url, tmp, [fc])

        class Busy:
            exiting = {"ARB-A", "EFF-0", "EFF-1", "EFF-2", "EFF-3"}
            lock = engine.lock
        engine.position_worker = Busy()
        engine.scan_focused()
        books = [str(r) for r in st.requests if "orderbook" in str(r)]
        assert not any("ARB-A" in b for b in books), (
            "priced a ticker the position worker was selling")


def test_an_explicit_series_list_overrides_what_the_strategies_claim():
    st = _exchange_with_two_series()
    with MockServer(st) as url, tempfile.TemporaryDirectory() as tmp:
        fc = FakeForecast(series=["MOCK"])
        engine = _engine(url, tmp, [fc])
        client = RecordingClient(url)
        engine.discovery.client = client
        engine.scan_focused(series=["OTHERSERIES"])
        assert client.asked_series == ["OTHERSERIES"]
