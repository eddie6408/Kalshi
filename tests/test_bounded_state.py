"""Long-running process hygiene: per-market state and research rows are
bounded, while open positions, working orders and trade records are kept."""
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lvt.db import open_state_db
from tests.edge_helpers import TA, EdgeHarness
from tests.helpers import T0, Harness, cfg, market

A, B = "KXATPMATCH-26SEP29AAABBB-AAA", "KXATPMATCH-26SEP29CCCDDD-CCC"


class TestEnginePrune(unittest.TestCase):
    def test_quiet_markets_are_forgotten(self):
        h = Harness(tickers=[market(A), market(B)])
        for t in range(0, 120, 2):
            h.step(t, {A: (50, 51), B: (40, 41)})
        for t in range(120, 4000, 60):                        # B goes quiet for more than an hour
            h.step(t, {A: (50, 51)})
        h.engine._last_state_prune = 0.0
        h.engine._prune(T0 + 4000)
        self.assertIn(A, h.engine.series)
        self.assertNotIn(B, h.engine.series)
        self.assertNotIn(B, h.engine.metrics)
        self.assertNotIn(B, h.engine.scanner_rows)

    def test_open_position_is_never_forgotten(self):
        h = Harness(tickers=[market(A), market(B)])
        for t in range(0, 120, 2):
            h.step(t, {A: (50, 51), B: (40, 41)})
        with mock.patch.object(h.engine.book, "open_positions", return_value=[SimpleNamespace(ticker=B)]):
            h.engine._last_state_prune = 0.0
            h.engine._prune(T0 + 9000)
        self.assertIn(B, h.engine.series)
        self.assertNotIn(A, h.engine.series)


class TestEdgePrune(unittest.TestCase):
    def test_edge_state_released(self):
        e = EdgeHarness()
        e.score()
        e.run([60.5] * 200)
        self.assertTrue(e.edge.ref_hist and e.edge.refs.matches)
        e.edge.prune(set(), T0 + 5 * 3600)
        self.assertFalse(e.edge.ref_hist)
        self.assertNotIn(TA, e.edge.refs.market_match)
        self.assertFalse(e.edge.refs.matches)
        self.assertFalse(e.edge.flow.book_prev)

    def test_live_market_state_kept(self):
        e = EdgeHarness()
        e.score()
        e.run([60.5] * 200)
        before = len(e.edge.ref_hist.get(TA, []))
        e.edge.prune(set(e.h.engine.series), T0 + 400)
        self.assertEqual(len(e.edge.ref_hist.get(TA, [])), before)
        self.assertTrue(e.edge.refs.matches)


class TestStateRetention(unittest.TestCase):
    def runner(self):
        from lvt import runner
        c = cfg(env={"TRADING_MODE": "PAPER"})
        with mock.patch.object(runner, "open_state_db", side_effect=lambda p: open_state_db(Path(tempfile.mkdtemp()) / "s.db")), \
                mock.patch.object(runner, "open_tape_db", side_effect=lambda p: open_state_db(Path(tempfile.mkdtemp()) / "t.db")):
            return runner.LiveRunner(c)

    def test_research_rows_age_out_trade_records_stay(self):
        r = self.runner()
        now = time.time()
        old, new = now - 40 * 86400, now - 3600
        for i, (ts, dec) in enumerate([(old, "NO_EDGE"), (old, "WATCH"), (old, "REJECTED"), (old, "TAKEN"),
                                       (old, "SHADOW"), (new, "NO_EDGE")]):
            r.db.insert("signals", {"id": f"s{i}", "ts": ts, "env": "PAPER", "ticker": A, "decision": dec})
            r.db.insert("signal_outcomes", {"signal_id": f"s{i}", "horizon_s": 60})
        r.db.insert("market_states", {"ts": now - 8 * 86400, "ticker": A})
        r.db.insert("market_states", {"ts": new, "ticker": A})
        r.db.insert("decisions", {"ts": old, "ticker": A, "decision": "HOLD"})
        r.db.insert("decisions", {"ts": old, "ticker": A, "decision": "EXIT"})
        r._prune_state(now)
        left = {x["id"] for x in r.db.query("SELECT id FROM signals")}
        self.assertEqual(left, {"s3", "s4", "s5"})           # TAKEN, SHADOW and recent rows survive
        self.assertEqual({x["signal_id"] for x in r.db.query("SELECT signal_id FROM signal_outcomes")}, left)
        self.assertEqual(r.db.query("SELECT count(*) n FROM market_states")[0]["n"], 1)
        self.assertEqual([x["decision"] for x in r.db.query("SELECT decision FROM decisions")], ["EXIT"])

    def test_bookkeeping_prune(self):
        r = self.runner()
        now = time.time()
        r.engine = SimpleNamespace(series={A: None})
        r.ctx_last = {"OLD": now - 7 * 3600, "NEW": now}
        r.event_markets = {"OLD": (now - 7200, [B]), "NEW": (now, [A])}
        r.companion_info = {A: 1, B: 2}
        r.scanner.probe_hist = {A: [(now, 50, 1)], B: [(now - 7200, 50, 1)]}
        r._prune_bookkeeping(now)
        self.assertEqual(set(r.ctx_last), {"NEW"})
        self.assertEqual(set(r.event_markets), {"NEW"})
        self.assertEqual(set(r.companion_info), {A})
        self.assertEqual(set(r.scanner.probe_hist), {A})


class TestNoEdgeTextCompact(unittest.TestCase):
    def test_no_edge_rows_store_reason_only(self):
        from lvt.models import Signal
        h = Harness()
        for i, dec in enumerate(("NO_EDGE", "WATCH")):
            sig = Signal(id=f"s{i}", ts=T0, ticker=A, event_ticker="E", strategy="EDGE_DISLOCATION_REVERSAL",
                         strategy_version="v", side="yes", action=dec, stage="SETUP", decision=dec,
                         reasons=["gap below threshold"])
            h.engine._record_signal(sig)
        rows = {x["decision"]: x["text"] for x in h.db.query("SELECT decision, text FROM signals")}
        self.assertEqual(rows["NO_EDGE"], "REASON: gap below threshold")
        self.assertGreater(len(rows["WATCH"]), len(rows["NO_EDGE"]))


if __name__ == "__main__":
    unittest.main()
