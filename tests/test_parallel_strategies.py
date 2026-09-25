"""One worker per strategy, sharing an account but never a queue.

The requirement: a rates strategy that can reprice every two minutes must not
wait on a weather strategy that is blocked on an ensemble API. Parallel in
fact, not parallel in name.

The danger that comes with it: the per-market exposure guard works by reading
what we hold, deciding, and remembering what we just committed. With two
passes running at once, a race there is not a glitch - it is the limit quietly
not applying, which is how Denver got bought twice in the first place.
"""
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

class _NoSafety:
    """Settings with no safety section: the circuit breakers are not what
    these tests are about, and an engine built with __new__ still needs one."""

    def section(self, name):
        return {}

    def get(self, key, default=None):
        return default

from kalshibot.arb_engine import ArbitrageEngine, RiskLimits
from kalshibot.models import Position


@dataclass
class FakeLeg:
    ticker: str
    side: str
    contracts: int
    limit_price_cents: int
    cost_usd: float = 0.0


class FakeOp:
    def __init__(self, ticker="T", side="yes", contracts=10, price=50):
        self.legs = [FakeLeg(ticker, side, contracts, price,
                             contracts * price / 100.0)]
        self.market_tickers = [ticker]
        self.quantity = contracts
        self.n_legs = 1
        self.gross_cost = contracts * price / 100.0

    def summary(self):
        return "fake"


class FakeStrategy:
    def __init__(self, name, series, on_find=None):
        self.name = name
        self.series = series
        self.skips = []
        self.calls = []
        self.on_find = on_find

    def candidate_series(self):
        return list(self.series)

    def find(self, markets, books, bankroll):
        self.calls.append(len(markets))
        if self.on_find:
            self.on_find(self)
        return []


def engine(strategies=(), positions=()):
    e = ArbitrageEngine.__new__(ArbitrageEngine)
    e.limits = RiskLimits.from_dict({"max_contracts_per_trade": 1000,
                                     "max_total_exposure_usd": 1e6,
                                     "max_event_exposure_usd": 1e6,
                                     "max_market_exposure_usd": 1e6,
                                     "max_trades_per_hour": 1000,
                                     "reentry_cooldown_minutes": 0})
    e.forecasts = list(strategies)
    e.exposure_usd = 0.0
    e.trade_times = []
    e._positions = {}
    e._positions_unknown = False
    e._pending_cost = {}
    e._risk_lock = threading.RLock()
    e._fill_holdings = {}
    e._suspended = set()
    e.s = _NoSafety()
    e.journal = None
    e.portfolio = type("P", (), {"positions": lambda self: list(positions)})()
    return e


# --------------------- each worker gets only its own ---------------------
def test_a_pass_only_runs_the_strategies_it_was_given():
    weather = FakeStrategy("weather_temperature", ["KXHIGHNY"])
    rates = FakeStrategy("rates_fedfunds", ["KXFED"])
    e = engine([weather, rates])

    asked = []
    e.discovery = type("D", (), {
        "discover_series": lambda self, series: (asked.extend(series) or ([], _stats()))})()
    e._refresh_bankroll = lambda: None
    e._refresh_positions = lambda: None
    e.calibration = type("C", (), {"may_enter": lambda self, n: (True, "ok")})()
    e.market = None
    e.book_workers = 1
    e.position_worker = None
    e.mode = type("M", (), {"name": "SCAN_ONLY"})()

    e.scan_focused(strategies=[rates])
    assert asked == ["KXFED"], f"asked for {asked}"
    assert weather.calls == [], "the weather strategy ran in the rates pass"


def test_no_strategies_means_no_work_at_all():
    e = engine([])
    e.discovery = type("D", (), {
        "discover_series": lambda self, s: (_ for _ in ()).throw(
            AssertionError("should never ask the exchange"))})()
    report = e.scan_focused(strategies=[])
    assert report.events_inspected == 0


def _stats():
    from kalshibot.discovery import DiscoveryStats
    return DiscoveryStats()


# --------------------- the shared state is the risk ---------------------
def test_two_passes_cannot_both_spend_the_same_allowance():
    """The race that would undo the per-market limit.

    Two workers judge the same market at the same moment. Exactly one may be
    allowed through, because the exchange will not have reported the first
    fill by the time the second asks.
    """
    e = engine()
    e.limits = RiskLimits.from_dict({"max_contracts_per_trade": 1000,
                                     "max_total_exposure_usd": 1e6,
                                     "max_event_exposure_usd": 1e6,
                                     "max_market_exposure_usd": 6.0,
                                     "max_trades_per_hour": 1000,
                                     "reentry_cooldown_minutes": 0,
                                     "allow_averaging_in": True})
    allowed, start = [], threading.Barrier(8)

    def attempt():
        start.wait()
        op = FakeOp(contracts=10, price=50)        # $5.00 each; cap is $6
        ok, _ = e._risk_ok(op)
        if ok:
            e._remember_pending(op)
            allowed.append(1)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len(allowed) == 1, f"{len(allowed)} passes spent the same allowance"


def test_concurrent_refresh_and_judge_do_not_corrupt_the_state():
    e = engine()
    errors = []

    def judger():
        try:
            for _ in range(200):
                e._risk_ok(FakeOp())
        except Exception as exc:        # noqa: BLE001
            errors.append(exc)

    def refresher():
        try:
            for _ in range(200):
                e._refresh_positions()
        except Exception as exc:        # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=judger) for _ in range(3)]
    threads.append(threading.Thread(target=refresher))
    for t in threads: t.start()
    for t in threads: t.join()
    assert not errors, errors


def test_suspension_is_per_pass_not_shared_state():
    """Two overlapping passes must not overwrite each other's answer."""
    good = FakeStrategy("good", ["A"])
    bad = FakeStrategy("bad", ["B"])
    e = engine([good, bad])
    e.calibration = type("C", (), {
        "may_enter": lambda self, n: (n != "bad", "DEGRADED" if n == "bad" else "ok")})()

    from kalshibot.arb_engine import ScanReport
    only_good = e._refresh_suspensions(ScanReport(), [good])
    only_bad = e._refresh_suspensions(ScanReport(), [bad])
    assert only_good == set()
    assert only_bad == {"bad"}
    # The second call must not have retroactively changed the first answer.
    assert only_good == set()
