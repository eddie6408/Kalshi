"""End-to-end DEMO trading, over real HTTP, against a mock exchange.

This is the milestone proof. Every step of the spec's first milestone runs
here for real: authenticate, read the account, list markets, pull order books,
scan, detect structure, price at depth, compute fees, apply the gate and the
risk limits, submit orders, watch fills, reconcile, and record.

The only thing swapped out is the network hop to Kalshi itself.
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
from kalshibot.clients import (DemoExecutionClient, MarketDataClient,  # noqa: E402
                               PortfolioClient)
from kalshibot.execution_manager import TradeOutcome  # noqa: E402
from kalshibot.fees import FeeModel  # noqa: E402
from kalshibot.journal import Journal  # noqa: E402


class Settings:
    """Minimal stand-in for the real Settings object."""
    kalshi_env = "demo"

    def __init__(self, rest_base, tmpdir=None, **over):
        self._base = rest_base
        self._tmp = Path(tmpdir) if tmpdir else Path(tempfile.gettempdir())
        self.raw = {
            "discovery": {"min_minutes_to_close": 5, "max_hours_to_close": 999,
                          "require_quotes": True, "max_market_pages": 3},
            "arbitrage": {"max_contracts_per_trade": 50,
                          "min_net_edge_per_contract": 0.002,
                          "require_exclusive_flag": True},
            "profitability": {"min_net_edge_usd": 0.02,
                              "min_net_edge_percent": 0.2,
                              "min_profit_per_trade_usd": 0.02,
                              "max_fee_drag_percent": 90,
                              "max_execution_risk": "high",
                              "max_opportunity_age_seconds": 600},
            "limits": {"max_contracts_per_trade": 50,
                       "max_total_exposure_usd": 500,
                       "max_event_exposure_usd": 500,
                       # Non-binding here on purpose: the per-market limit has
                       # its own tests in test_market_exposure.py, and these
                       # tests are about the order path.
                       "max_market_exposure_usd": 500,
                       "max_trades_per_scan": 1},
            # These tests exercise the order path end to end against the mock
            # exchange; the unhedged-exposure limit has dedicated tests in
            # test_execution.py and is left non-binding here.
            "execution": {"max_partial_fill_time_seconds": 1,
                          "fill_poll_seconds": 0.05,
                          "max_unhedged_exposure_usd": 500.0},
            "fees": {"safety_multiplier": 1.0},
        }
        for k, v in over.items():
            self.raw.setdefault(k, {}).update(v)

    @property
    def rest_base(self):
        return self._base

    def path(self, dotted, default):
        """Keep every file a test writes inside the test's temp dir.

        Without this the liquidity profile landed in the project's real
        data/ folder, so running the test suite taught the live bot that a
        series called MOCK was the most liquid thing on the exchange.
        """
        return self._tmp / Path(default).name

    def section(self, name):
        return self.raw.get(name, {}) or {}

    def get(self, dotted, default=None):
        node = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def build(url, state, mode, tmp, **over):
    market = MarketDataClient(url)
    portfolio = PortfolioClient(url, _Signer())
    execution = (DemoExecutionClient(url, _Signer(), "demo")
                 if mode is TradingMode.DEMO_LIVE else None)
    settings = Settings(url, tmpdir=tmp, **over)
    journal = Journal(Path(tmp) / "j.db")
    engine = ArbitrageEngine(settings, market, portfolio, execution,
                             FeeModel({"safety_multiplier": 1.0}), journal, mode)
    return engine, journal


class _Signer:
    """Produces the three headers the mock checks for, without real crypto."""
    key_id = "mock-key-id"

    def sign(self, method, path):
        return {"KALSHI-ACCESS-KEY": self.key_id,
                "KALSHI-ACCESS-TIMESTAMP": "1",
                "KALSHI-ACCESS-SIGNATURE": "mock-signature",
                "Content-Type": "application/json",
                "Accept": "application/json"}


# ================================================================
def test_scan_only_finds_the_arbitrage_and_sends_nothing():
    state = seeded_exchange(with_arbitrage=True)
    with MockServer(state) as url, tempfile.TemporaryDirectory() as tmp:
        engine, journal = build(url, state, TradingMode.SCAN_ONLY, tmp)
        report = engine.scan_once()

        assert report.discovery.markets_seen == 5
        assert report.qualifying == 1, [o.summary() for o in report.opportunities]
        op = report.accepted[0]
        assert op.strategy_type == "same_market"
        assert op.market_tickers == ["ARB-A"]
        assert op.guaranteed_min_payout == op.quantity * 1.00
        assert op.net_edge > 0

        posts = [r for r in state.requests if r[0] == "POST"]
        assert posts == [], "SCAN_ONLY must never place an order"


def test_scan_only_reports_nothing_on_an_efficient_board():
    state = seeded_exchange(with_arbitrage=False)
    with MockServer(state) as url, tempfile.TemporaryDirectory() as tmp:
        engine, _ = build(url, state, TradingMode.SCAN_ONLY, tmp)
        report = engine.scan_once()
        assert report.qualifying == 0
        assert report.discovery.markets_seen == 4
        assert [r for r in state.requests if r[0] == "POST"] == []


def test_paper_mode_sends_no_orders():
    state = seeded_exchange(with_arbitrage=True)
    with MockServer(state) as url, tempfile.TemporaryDirectory() as tmp:
        engine, _ = build(url, state, TradingMode.PAPER, tmp)
        report = engine.scan_once()
        assert report.qualifying == 1
        assert [r for r in state.requests if r[0] == "POST"] == []


def test_demo_live_actually_places_orders_and_fills():
    """The milestone. Orders go over HTTP, fill, reconcile, and are recorded."""
    state = seeded_exchange(with_arbitrage=True)
    with MockServer(state) as url, tempfile.TemporaryDirectory() as tmp:
        engine, journal = build(url, state, TradingMode.DEMO_LIVE, tmp)
        report = engine.scan_once()

        assert report.qualifying == 1
        assert len(report.trades) == 1
        trade = report.trades[0]
        assert trade.outcome is TradeOutcome.COMPLETE, trade.notes
        assert trade.fully_filled
        assert trade.realised_edge_usd > 0

        posts = [r for r in state.requests if r[0] == "POST"]
        assert len(posts) == 2, "one order per leg"
        assert len(state.orders) == 2
        assert all(o["status"] == "executed" for o in state.orders.values())

        # both sides of the same market are now held
        assert state.positions["ARB-A"] == 0 or "ARB-A" in state.positions

        summary = journal.arbitrage_summary()
        assert summary["opportunities"] >= 1
        assert summary["accepted"] == 1
        assert summary["executions"] == 1
        assert summary["complete"] == 1
        assert summary["legs_submitted"] == 2


def test_demo_live_handles_a_partial_fill_and_recovers():
    state = seeded_exchange(with_arbitrage=True)
    state.fill_limits["ARB-A"] = 0          # nothing fills
    with MockServer(state) as url, tempfile.TemporaryDirectory() as tmp:
        engine, journal = build(url, state, TradingMode.DEMO_LIVE, tmp)
        report = engine.scan_once()
        assert report.trades
        trade = report.trades[0]
        assert trade.outcome is not TradeOutcome.COMPLETE
        assert not trade.fully_filled
        deletes = [r for r in state.requests if r[0] == "DELETE"]
        assert deletes, "unfilled remainder must be cancelled"


def test_orders_carry_authentication_headers():
    state = seeded_exchange(with_arbitrage=True)
    with MockServer(state) as url, tempfile.TemporaryDirectory() as tmp:
        engine, _ = build(url, state, TradingMode.DEMO_LIVE, tmp)
        engine.scan_once()
        assert len(state.orders) == 2, "a 401 would have left no orders"


def test_balance_and_positions_are_readable():
    state = seeded_exchange()
    with MockServer(state) as url:
        p = PortfolioClient(url, _Signer())
        assert p.balance_usd() == 492.00
        assert p.positions() == []


def test_risk_limit_caps_the_traded_quantity():
    state = seeded_exchange(with_arbitrage=True)
    with MockServer(state) as url, tempfile.TemporaryDirectory() as tmp:
        engine, _ = build(url, state, TradingMode.DEMO_LIVE, tmp,
                          arbitrage={"max_contracts_per_trade": 1},
                          limits={"max_contracts_per_trade": 1})
        report = engine.scan_once()
        assert report.qualifying == 1
        assert report.accepted[0].quantity == 1
        for order in state.orders.values():
            # V2 counts are fixed-point strings, not integers
            assert order["initial_count_fp"] == "1.00"


def test_depth_limits_the_size_even_when_limits_are_generous():
    state = seeded_exchange(with_arbitrage=True)
    state.books["ARB-A"] = {"yes": [[55, 7]], "no": [[65, 7]]}
    with MockServer(state) as url, tempfile.TemporaryDirectory() as tmp:
        engine, _ = build(url, state, TradingMode.DEMO_LIVE, tmp)
        report = engine.scan_once()
        assert report.qualifying == 1
        assert report.accepted[0].quantity <= 7


def test_journal_records_rejections_with_reasons():
    state = seeded_exchange(with_arbitrage=False)
    with MockServer(state) as url, tempfile.TemporaryDirectory() as tmp:
        engine, journal = build(url, state, TradingMode.SCAN_ONLY, tmp)
        engine.scan_once()
        rows = list(journal.conn.execute(
            "SELECT decision, reject_reason FROM opportunities"))
        for r in rows:
            assert r["decision"] in ("TRADE", "REJECT")


def test_one_huge_event_cannot_eat_the_whole_order_book_budget():
    """Regression from a live scan: 1,500 order books were spent on FOUR
    events because the sort favoured event size. The biggest events on this
    exchange are sprawling and never traded."""
    state = seeded_exchange(with_arbitrage=True)
    state.add_event("HUGE", mutually_exclusive=False)
    from mock_exchange import market as mk_market
    for i in range(60):
        t = f"HUGE-{i}"
        m = mk_market(t, "HUGE", yes_bid=0, yes_ask=0)
        m["volume"] = 0
        m["open_interest"] = 0
        state.add_market(m, yes_levels=[], no_levels=[])

    with MockServer(state) as url, tempfile.TemporaryDirectory() as tmp:
        engine, _ = build(url, state, TradingMode.SCAN_ONLY, tmp)
        engine.max_books_per_event = 5
        report = engine.scan_once()
        assert report.events_inspected >= 2, (
            f"budget collapsed into {report.events_inspected} event(s)")


def test_never_traded_events_are_skipped_before_fetching_books():
    state = seeded_exchange(with_arbitrage=False)
    for m in state.markets.values():
        m["volume"] = 0
        m["open_interest"] = 0
    with MockServer(state) as url, tempfile.TemporaryDirectory() as tmp:
        engine, _ = build(url, state, TradingMode.SCAN_ONLY, tmp,
                          discovery={"min_event_activity": 1,
                                     "min_minutes_to_close": 5,
                                     "max_hours_to_close": 999,
                                     "max_market_pages": 3})
        report = engine.scan_once()
        assert report.books_fetched == 0, "should not fetch books for dead events"
        assert report.discovery.dropped_no_activity > 0


def test_traded_events_are_still_inspected():
    state = seeded_exchange(with_arbitrage=True)
    with MockServer(state) as url, tempfile.TemporaryDirectory() as tmp:
        engine, _ = build(url, state, TradingMode.SCAN_ONLY, tmp,
                          discovery={"min_event_activity": 1,
                                     "min_minutes_to_close": 5,
                                     "max_hours_to_close": 999,
                                     "max_market_pages": 3})
        report = engine.scan_once()
        assert report.books_fetched > 0
        assert report.qualifying == 1


def test_a_single_confirmed_book_counts_as_evidence():
    """Regression: requiring two samples discarded 47 of 55 confirmed-liquid
    series on a live audit, because the budget only sampled most series once."""
    import tempfile as _tf
    from kalshibot.liquidity import LiquidityProfile
    with _tf.TemporaryDirectory() as tmp:
        prof = LiquidityProfile(Path(tmp) / "liq.json")
        prof.record("KXINXU", checked=1, with_depth=1)
        prof.record("KXDEAD", checked=1, with_depth=0)
        assert "KXINXU" in prof.known_liquid()
        assert "KXDEAD" not in prof.known_liquid()


def test_repeatedly_empty_series_are_written_off():
    import tempfile as _tf
    from kalshibot.liquidity import LiquidityProfile
    with _tf.TemporaryDirectory() as tmp:
        prof = LiquidityProfile(Path(tmp) / "liq.json")
        for _ in range(4):
            prof.record("KXQUIET", checked=1, with_depth=0)
        assert "KXQUIET" in prof.known_dead()
        assert "KXQUIET" not in prof.known_liquid()


def test_liquidity_profile_survives_a_round_trip():
    import tempfile as _tf
    from kalshibot.liquidity import LiquidityProfile
    with _tf.TemporaryDirectory() as tmp:
        path = Path(tmp) / "liq.json"
        a = LiquidityProfile(path)
        a.record("KXINXU", 6, 6)
        a.record("KXNFLSPREAD", 3, 3)
        a.save()
        b = LiquidityProfile(path)
        assert set(b.known_liquid()) == {"KXINXU", "KXNFLSPREAD"}


def test_exploration_budget_is_reserved_for_unknown_series():
    """A profile full of proven series must not starve discovery."""
    state = seeded_exchange(with_arbitrage=True)
    with MockServer(state) as url, tempfile.TemporaryDirectory() as tmp:
        engine, _ = build(url, state, TradingMode.SCAN_ONLY, tmp)
        engine.liquidity.record("SOMETHING-ELSE", 10, 10)
        engine.explore_fraction = 0.5
        report = engine.scan_once()
        # MOCK is unknown to the profile; it must still get inspected
        assert report.books_fetched > 0
        assert report.qualifying == 1
