"""V2 brief section 61: timing, execution, modelling, strategy and risk fault
cases, plus the degradation monitor (sections 67, 74) and observe-only
mode (section 65)."""
import json
import tempfile
import unittest
from pathlib import Path

from lvt.alerts import Alerter
from lvt.db import open_state_db
from lvt.models import Trade
from lvt.monitor import Monitor, corrupt_quotes
from tests.edge_helpers import EVENT, TA, EdgeHarness
from tests.helpers import T0, cfg, quote, snap

MAKER = {"execution": {"entry_style": "maker", "exit_style": "maker"}, "maker": {"take_profit": True}}


def calm(x, secs=300, mid=60.5):
    x.run([mid] * int(secs / 2))


def drop_and_stabilise(x):
    x.score(points=(1, 2))
    x.run([58.5, 56.5, 54.5, 52.5, 50.5] + [50.5] * 6 + [51.5] * 8)


class TestTiming(unittest.TestCase):
    def test_no_reversal_trade_without_a_score_update_after_the_move(self):
        x = EdgeHarness(MAKER)
        calm(x)
        x.last_ctx = x.t + 10_000                       # the score feed goes silent from here on
        x.run([58.5, 56.5, 54.5, 52.5, 50.5] + [50.5] * 6 + [51.5] * 8)
        self.assertEqual(x.signals(action="BUY"), [])

    def test_stale_market_data_blocks_decisions(self):
        x = EdgeHarness(MAKER)
        calm(x)
        now = T0 + x.t
        stale = {TA: snap(TA, now - 30, 50, 51)}         # a delayed market feed
        before = len(x.signals())
        x.h.engine.on_snapshots(stale, now)
        self.assertEqual(len(x.signals()), before)


class TestModelling(unittest.TestCase):
    def test_low_confidence_is_no_edge(self):
        x = EdgeHarness({**MAKER, "edge": {"anchor_min_samples": 5, "min_confidence": 0.99}})
        calm(x)
        drop_and_stabilise(x)
        self.assertEqual(x.signals(action="BUY"), [])
        self.assertTrue(any("confidence" in r["text"] for r in x.signals(action="NO_EDGE")))

    def test_extreme_prices_stay_in_range(self):
        x = EdgeHarness(MAKER)
        calm(x, mid=97.5)
        ref = x.edge.refs.reference(TA, T0 + x.t)[0]
        self.assertTrue(ref is not None and 0 <= ref.value <= 100)

    def test_no_reference_means_generic_exits_only(self):
        x = EdgeHarness(MAKER)
        calm(x)
        from lvt.models import FillEvent, Signal
        sig = Signal("s", T0, TA, EVENT, "EDGE_DISLOCATION_REVERSAL", "v", "yes", "BUY", "C", ask=51, stop=45, target=57)
        p = x.h.engine.book.open_pending(sig, x.h.markets[TA], T0)
        x.h.engine.book.apply_entry_fill(p, FillEvent("f", T0, 51, 10, 0))
        self.assertIsNone(x.edge.exit_check(p, snap(TA, T0 + x.t + 60, 50, 51), T0 + x.t + 60))   # stale -> no edge exit


class TestStrategyFaults(unittest.TestCase):
    def test_false_reversal_is_not_bought(self):
        x = EdgeHarness(MAKER)
        calm(x)
        x.score(points=(1, 2))
        x.run([58.5, 56.5, 54.5, 52.5, 50.5, 51.5, 50.5, 49.5, 48.5, 47.5, 46.5, 45.5])   # bounce then new lows
        self.assertEqual(x.signals(action="BUY"), [])

    def test_conflicting_order_flow_blocks_reversal(self):
        x = EdgeHarness(MAKER)
        calm(x)
        x.score(points=(1, 2))
        x.run([58.5, 56.5, 54.5, 52.5, 50.5] + [50.5] * 6)
        for i in range(8):                               # heavy, accelerating selling while price "stabilises"
            now = T0 + x.t
            x.edge.on_trades([Trade(TA, now - 1, 50.0, 200 * (i + 1), "no", f"t{i}")])
            x.tick(51.5)
        # V1 reads flow per market: the market with the selling is not bought. (Its mirror - NO on
        # the other player's market - is the same risk and V1 does not pool flow across mirrors;
        # logged as a V2 hypothesis rather than changed mid-A/B.)
        buys = [r for r in x.signals(action="BUY") if r["strategy"] == "EDGE_DISLOCATION_REVERSAL" and r["ticker"] == TA]
        self.assertEqual(buys, [])

    def test_market_suspension_blocks_entries(self):
        x = EdgeHarness(MAKER)
        calm(x)
        x.score(games=(2, 3), points=(0, 0), server="c2")
        x.h.step(x.t, {TA: (58, 59)}, status="closed")
        self.assertEqual(x.h.db.scalar("SELECT COUNT(*) FROM orders"), 0)


class TestMonitor(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = open_state_db(self.tmp / "s.db")
        self.m = Monitor(cfg(), self.db, Alerter(self.db, "r", 0), self.tmp)
        from tests.helpers import Harness
        self.h = Harness()

    def test_corrupt_quotes_dropped_and_suspend(self):
        qs = {f"T{i}": quote(f"T{i}", T0, 50, 51) for i in range(5)}
        qs["BAD"] = quote("BAD", T0, 60, 55)
        self.assertEqual(corrupt_quotes(qs), ["BAD"])
        kept = self.m.on_quotes(qs, [self.h.engine])
        self.assertNotIn("BAD", kept)
        self.assertIn("DATA_CORRUPTION", self.h.engine.risk.fail_closed)             # 1/6 > 10%
        self.assertEqual(self.m.state, "SUSPENDED")
        self.assertTrue(list((self.tmp / "incidents").glob("*SUSPENDED*.json")))
        self.m.on_quotes({f"T{i}": quote(f"T{i}", T0, 50, 51) for i in range(5)}, [self.h.engine])
        self.assertNotIn("DATA_CORRUPTION", self.h.engine.risk.fail_closed)
        self.assertEqual(self.m.state, "OK")

    def test_api_instability_and_slow_cycles(self):
        class S:
            requests, errors = 0, 0
        s = S()
        self.m.periodic(T0, s, [self.h.engine])
        s.requests, s.errors = 100, 40
        self.m.periodic(T0 + 60, s, [self.h.engine])
        self.assertIn("API_INSTABILITY", self.h.engine.risk.fail_closed)
        for _ in range(5):
            self.m.on_cycle(5000)
        self.assertIn("SLOW_CYCLES", self.m.reasons)

    def test_model_malfunction_suspends_edge_only(self):
        class Edge:
            malfunctions = 3
        class S:
            requests, errors = 0, 0
        e2 = type("E", (), {"edge": Edge(), "risk": type("R", (), {"fail_closed": {}})(), "env": "PAPER_EDGE"})()
        self.h.engine.edge = None
        self.m.periodic(T0, S(), [self.h.engine, e2], Edge())
        self.m.periodic(T0 + 1, S(), [self.h.engine, e2], e2.edge)
        self.assertIn("MODEL_MALFUNCTION", e2.risk.fail_closed)
        self.assertNotIn("MODEL_MALFUNCTION", self.h.engine.risk.fail_closed)


class TestObserveOnly(unittest.TestCase):
    def test_observe_only_records_but_never_trades(self):
        x = EdgeHarness({**MAKER, "edge": {"anchor_min_samples": 5, "observe_only": True}})
        calm(x)
        x.score(games=(2, 3), points=(0, 0), server="c2")
        x.run([60.5, 59.5, 59.5, 58.5])
        shadow = x.h.db.query("SELECT * FROM signals WHERE decision='SHADOW'")
        self.assertTrue(shadow)
        self.assertEqual(x.h.db.scalar("SELECT COUNT(*) FROM orders"), 0)
        self.assertEqual(x.h.db.scalar("SELECT COUNT(*) FROM positions"), 0)
        self.assertEqual(json.loads(shadow[0]["explanation"])["classification"], "UNFINISHED_MOMENTUM")


if __name__ == "__main__":
    unittest.main()
