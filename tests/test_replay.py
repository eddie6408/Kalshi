"""Tape recording, deterministic replay, no look-ahead, backtest metrics,
OOS split, signal outcome labeling, reports."""
import tempfile
import unittest
from pathlib import Path

from lvt.db import open_state_db, open_tape_db
from lvt.labeler import label_signals
from lvt.models import Depth
from lvt.replay import parse_grid, run_oos, run_replay
from lvt.reports import full_report, metrics, verdict
from lvt.tape import TapeRecorder, iter_snapshots
from tests.helpers import T0, cfg, market, mid_to_book, path, quote
from tests.test_strategies import DIP, only


def build_tape(pts=DIP, ticker=None, repeat=1, gap=0.0):
    tmp = Path(tempfile.mkdtemp())
    tape = open_tape_db(tmp / "tape.db")
    rec = TapeRecorder(tape)
    m = market(ticker) if ticker else market()
    rec.record_markets([m], T0)
    vol = 0.0
    for k in range(repeat):
        base = k * (pts[-1][0] + 600 + gap)
        for t, mid in path(pts):
            b, a = mid_to_book(mid)
            vol += 20
            rec.record_quotes({m.ticker: quote(m.ticker, T0 + base + t, b, a, volume=vol)}, keepalive_s=6)
    return tmp, tape, m


class TestTape(unittest.TestCase):
    def test_dedupes_unchanged_quotes_but_keeps_alive(self):
        tape = open_tape_db(Path(tempfile.mkdtemp()) / "t.db")
        rec = TapeRecorder(tape)
        for i in range(301):                                      # 10 minutes of identical 2s polls
            rec.record_quotes({"X": quote("X", T0 + 2 * i, 49, 50, volume=100)}, keepalive_s=6)
        n = tape.scalar("SELECT COUNT(*) FROM quotes")
        self.assertLess(n, 301)                                   # not every 2s poll
        self.assertGreaterEqual(n, 600 / 6 - 2)                   # but a keepalive at least every 6s

    def test_depth_dedupe(self):
        tape = open_tape_db(Path(tempfile.mkdtemp()) / "t.db")
        rec = TapeRecorder(tape)
        d = Depth("X", T0, [(49.0, 10.0)], [(50.0, 5.0)])
        rec.record_depth(d)
        rec.record_depth(Depth("X", T0 + 2, [(49.0, 10.0)], [(50.0, 5.0)]))
        self.assertEqual(tape.scalar("SELECT COUNT(*) FROM depth"), 1)

    def test_iterator_never_yields_future_rows(self):
        tmp, tape, m = build_tape()
        last = 0
        for t, snaps in iter_snapshots(tape, 0, 1e12, step_s=2, keepalive_s=6):
            self.assertGreaterEqual(t, last)
            for s in snaps.values():
                self.assertLessEqual(s.ts, t)
            last = t

    def test_market_goes_stale_when_recording_stops(self):
        tmp, tape, m = build_tape([(0, 50), (60, 51)])
        tape.execute("INSERT INTO quotes VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (T0 + 600, m.ticker, 50, 51, 500, 500, None, 999, 0, "active"))
        ts = [t for t, s in iter_snapshots(tape, 0, 1e12, step_s=2, keepalive_s=6)]
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        self.assertTrue(any(g > 100 for g in gaps))               # idle stretch skipped, not filled in


class TestReplay(unittest.TestCase):
    def test_backtest_trades_and_is_deterministic(self):
        tmp, tape, m = build_tape(repeat=3)
        c = cfg(only("DIP"))
        state = open_state_db(tmp / "state.db")
        a = run_replay(c, tape, 0, 1e12, tmp / "bt", state_db=state)
        b = run_replay(c, tape, 0, 1e12, tmp / "bt", state_db=state)
        self.assertEqual(a["metrics"]["trades"], 3)
        for k in ("trades", "net_pnl_usd", "fees_usd", "slippage_usd", "win_rate"):
            self.assertEqual(a["metrics"][k], b["metrics"][k], k)
        self.assertEqual(state.scalar("SELECT COUNT(*) FROM backtest_runs"), 2)
        # backtest trades never land in the live state database
        self.assertEqual(state.scalar("SELECT COUNT(*) FROM positions"), 0)

    def test_no_look_ahead_decisions_identical_on_truncated_data(self):
        tmp, tape, m = build_tape(repeat=2)
        c = cfg(only("DIP"))
        cut = T0 + 700
        full = run_replay(c, tape, 0, 1e12, tmp / "bt")
        part = run_replay(c, tape, 0, cut, tmp / "bt")
        fdb, pdb = open_state_db(full["db_path"]), open_state_db(part["db_path"])
        q = "SELECT ts, strategy, action, decision, ask, target, stop FROM signals WHERE ts < ? ORDER BY ts, strategy"
        self.assertEqual(fdb.query(q, (cut - 30,)), pdb.query(q, (cut - 30,)))

    def test_oos_split_and_grid(self):
        tmp, tape, m = build_tape(repeat=5)
        state = open_state_db(tmp / "state.db")
        from lvt.config import ROOT
        import lvt.replay as rp
        orig = rp.load_config
        rp.load_config = lambda root, overrides=None: cfg({**only("DIP"), **(overrides or {})})
        try:
            res = run_oos(ROOT, tape, state, tmp / "bt", ["strategies.DIP.min_drop_cents=6,15"])
        finally:
            rp.load_config = orig
        segs = [r["segment"] for r in state.query("SELECT segment FROM backtest_runs ORDER BY ts")]
        self.assertEqual(segs.count("train"), 2)
        self.assertEqual(segs.count("test"), 1)
        tr, va, te = res["split"]["train"], res["split"]["validation"], res["split"]["test"]
        self.assertLessEqual(tr[1], va[0] + 1e-6)
        self.assertLessEqual(va[1], te[0] + 1e-6)

    def test_parse_grid(self):
        g = parse_grid(["a.b=1,2", "c=x"])
        self.assertEqual(g, [{"a": {"b": 1}, "c": "x"}, {"a": {"b": 2}, "c": "x"}])


class TestLabelerAndReports(unittest.TestCase):
    def test_labels_every_signal_from_later_data_only(self):
        tmp, tape, m = build_tape(repeat=2)
        c = cfg(only("DIP"))
        s = run_replay(c, tape, 0, 1e12, tmp / "bt", label=False)
        db = open_state_db(s["db_path"])
        n = label_signals(db, tape, c, T0 + 1e6)
        self.assertGreater(n, 0)
        rows = db.query("SELECT * FROM signal_outcomes o JOIN signals s ON s.id=o.signal_id WHERE s.action='BUY' "
                        "AND o.horizon_s=900")
        self.assertTrue(rows)
        self.assertTrue(all(r["first_hit"] in ("TARGET", "STOP", "NEITHER") for r in rows))
        self.assertEqual(label_signals(db, tape, c, T0 + 1e6), 0)          # idempotent

    def test_report_separates_environments_and_never_claims_profit_on_small_samples(self):
        db = open_state_db(Path(tempfile.mkdtemp()) / "s.db")
        db.insert("positions", {"id": "a", "env": "PAPER", "status": "CLOSED", "realized_pnl_usd": 5.0,
                                "opened_ts": 0, "closed_ts": 10, "strategy": "DIP"})
        db.insert("positions", {"id": "b", "env": "LIVE", "status": "CLOSED", "realized_pnl_usd": -1.0,
                                "opened_ts": 0, "closed_ts": 10, "strategy": "DIP"})
        p = full_report(db, "PAPER")
        self.assertEqual(p["overall"]["net_pnl_usd"], 5.0)
        self.assertIn("INSUFFICIENT SAMPLE", p["verdict"])
        self.assertEqual(full_report(db, "LIVE")["overall"]["net_pnl_usd"], -1.0)

    def test_metrics(self):
        rows = [{"realized_pnl_usd": x, "opened_ts": i * 100, "closed_ts": i * 100 + 50} for i, x in
                enumerate([1, -2, 3, -1, -1, 4])]
        m = metrics(rows)
        self.assertEqual(m["trades"], 6)
        self.assertAlmostEqual(m["win_rate"], 0.5)
        self.assertAlmostEqual(m["expectancy_usd"], 4 / 6, places=3)
        self.assertEqual(m["max_drawdown_usd"], 2.0)          # peak 1 -> -1 and peak 2 -> 0
        self.assertEqual(m["longest_loss_streak"], 2)
        self.assertAlmostEqual(m["profit_factor"], 2.0)
        self.assertIn("INSUFFICIENT", verdict(m))


if __name__ == "__main__":
    unittest.main()
