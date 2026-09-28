"""Package 8: the fee schedule Kalshi publishes, per series.

Pinned: for the schedule every traded series has today (quadratic x1) the
published taker fee IS the configured one, so no decision changes; a series
whose fees are unknown, unreadable or higher is refused for ENTRY and never
priced cheaper; scheduled changes are alerted once; the maker fee is zero
where Kalshi charges none.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest
from kalshibot.fee_schedule import FeeSchedule, SeriesFees, series_of
from kalshibot.fees import FeeModel, taker_fee_usd
from kalshibot.gate import ProfitabilityGate
from kalshibot.journal import Journal


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class Client:
    def __init__(self, series=None, changes=None, fail=False):
        self.series = series or {}
        self.changes = changes or {}
        self.fail = fail
        self.calls = 0

    def get_series(self, s):
        self.calls += 1
        if self.fail:
            raise RuntimeError("503")
        return self.series.get(s, {})

    def get_fee_changes(self, s):
        return self.changes.get(s, [])


Q1 = {"fee_type": "quadratic", "fee_multiplier": 1}


def test_quadratic_x1_is_exactly_the_configured_taker_fee():
    sf = SeriesFees("KXHIGHNY", "quadratic", 1.0, 0)
    for price in range(1, 100):
        for n in (1, 7, 25, 100):
            assert sf.taker_fee_usd(price, n) == taker_fee_usd(price, n)


def test_maker_fees_only_where_kalshi_charges_them():
    assert SeriesFees("KXHIGHNY", "quadratic", 1.0, 0).maker_fee_usd(50, 10) == 0.0
    nfl = SeriesFees("KXNFLTOTAL", "quadratic_with_maker_fees", 1.0, 0)
    assert nfl.maker_fee_usd(50, 10) == pytest.approx(0.05)     # ceil(0.04375)
    assert nfl.maker_fee_usd(50, 100) == pytest.approx(0.44)


def test_the_schedule_is_cached_and_refreshed():
    clock = Clock()
    c = Client({"KXHIGHNY": Q1})
    fs = FeeSchedule(c, clock=clock, refresh_hours=6)
    for _ in range(5):
        assert fs.uncertain("KXHIGHNY") is None
    assert c.calls == 1
    clock.t += 6 * 3600 + 1
    fs.get("KXHIGHNY")
    assert c.calls == 2
    assert fs.version_for(["KXHIGHNY-26SEP28-B70.5"]) == "kalshi:KXHIGHNY:quadratic:x1"


def test_unknown_or_higher_fees_are_uncertain_lower_is_not():
    fs = FeeSchedule(Client({
        "A": {"fee_type": "margin_market_maker_program_fees", "fee_multiplier": 0},
        "B": {"fee_type": "quadratic", "fee_multiplier": 2},
        "C": {"fee_type": "quadratic", "fee_multiplier": 0.5},
        "D": {},
    }), clock=Clock())
    assert "unknown" in fs.uncertain("A")
    assert "above" in fs.uncertain("B")
    assert fs.uncertain("C") is None          # cheaper: still priced at config
    assert "unknown" in fs.uncertain("D")


def test_a_failed_read_keeps_the_last_good_schedule_and_none_is_uncertain():
    clock = Clock()
    c = Client({"KXHIGHNY": Q1})
    fs = FeeSchedule(c, clock=clock)
    fs.get("KXHIGHNY")
    c.fail = True
    clock.t += 7 * 3600
    assert fs.uncertain("KXHIGHNY") is None
    assert "unreadable" in FeeSchedule(Client(fail=True), clock=clock).uncertain("KXHIGHNY")


def test_a_scheduled_change_is_alerted_once():
    alerts = []
    clock = Clock()
    change = {"series_ticker": "KXHIGHNY", "fee_type": "quadratic_with_maker_fees",
              "fee_multiplier": 1, "scheduled_ts": "2026-10-15T04:00:00Z"}
    fs = FeeSchedule(Client({"KXHIGHNY": Q1}, {"KXHIGHNY": [change]}), clock=clock,
                     alert=lambda s, m: alerts.append((s, m)))
    fs.get("KXHIGHNY")
    clock.t += 7 * 3600
    fs.get("KXHIGHNY")
    assert len(alerts) == 1 and alerts[0][0] == "KXHIGHNY"


def test_check_tickers_reads_every_series_and_honours_enforce():
    bad = Client({"KXHIGHNY": Q1, "KXNEW": {"fee_type": "flat", "fee_multiplier": 1}})
    assert "KXNEW" in FeeSchedule(bad, clock=Clock()).check_tickers(
        ["KXHIGHNY-1", "KXNEW-1"])
    assert FeeSchedule(bad, clock=Clock(), enforce=False).check_tickers(["KXNEW-1"]) is None
    assert series_of("kxhighny-26SEP28-B70.5") == "KXHIGHNY"


# ------------------------------------------------------------ the engine
def _engine(journal, schedule):
    from test_opportunity_ledger import _Settings
    from kalshibot.arb_engine import ArbitrageEngine, TradingMode
    e = ArbitrageEngine.__new__(ArbitrageEngine)
    e.gate, e.journal, e.mode = ProfitabilityGate({}), journal, TradingMode.DEMO_LIVE
    e.s = _Settings()
    e._risk_ok = lambda op: (True, "ok")
    e.fees = FeeModel({})
    e.fee_schedule = schedule
    return e


def test_an_entry_on_a_series_with_uncertain_fees_is_refused_and_recorded():
    from test_opportunity_ledger import _arb
    from kalshibot.arb_engine import ScanReport
    with tempfile.TemporaryDirectory() as tmp:
        j = Journal(Path(tmp) / "j.db")
        op = _arb()
        report = ScanReport()
        fs = FeeSchedule(Client({series_of(op.legs[0].ticker):
                                 {"fee_type": "quadratic", "fee_multiplier": 3}}), clock=Clock())
        _engine(j, fs)._judge(op, report)
        assert report.accepted == []
        assert report.rejections == {"GATE:FEE_SCHEDULE_UNCERTAIN": 1}
        r = j.conn.execute("SELECT decision, decision_stage, rejection_reason, "
                           "fee_schedule_version FROM opportunity_ledger").fetchone()
        assert (r[0], r[1]) == ("REJECTED", "GATE") and r[2].startswith("FEE_SCHEDULE_UNCERTAIN")
        assert "x3" in r[3]


def test_a_known_x1_schedule_changes_nothing():
    from test_opportunity_ledger import _arb
    from kalshibot.arb_engine import ScanReport
    with tempfile.TemporaryDirectory() as tmp:
        j = Journal(Path(tmp) / "j.db")
        op = _arb()
        report = ScanReport()
        fs = FeeSchedule(Client({series_of(op.legs[0].ticker): Q1}), clock=Clock())
        _engine(j, fs)._judge(op, report)
        assert report.accepted == [op] and op.decision == "ACCEPTED"
        assert op.fee_schedule_version.endswith(":quadratic:x1")


def test_the_maker_shadow_uses_the_series_maker_fee():
    from kalshibot.depth import DepthBook, Level
    from kalshibot.maker_shadow import MakerShadowConfig, price_order
    book = DepthBook("W-1", [Level(40, 20)], [Level(54, 20)])
    free = SeriesFees("KXHIGHNY", "quadratic", 1.0, 0)
    p = price_order(0.44, book, "yes", MakerShadowConfig(), FeeModel({}), free.maker_fee_usd)
    assert p["maker_fee_cents"] == 0.0
    # with no fee the whole 3c edge is available: 44 - 3 = 41 = bid + 1
    assert p["price_cents"] == 41
