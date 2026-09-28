"""V2 studies: gap predictiveness, decay, calibration, funnel, registry,
and the replay no-look-ahead guarantee for the edge engine."""
import tempfile
import unittest
from pathlib import Path

from lvt.db import open_state_db, open_tape_db
from lvt.edge import studies
from lvt.tape import TapeRecorder
from tests.helpers import T0, quote


def setup(gap_follows: bool, n_matches: int = 8):
    """Dislocation rows (fair above market by 5c) and tape quotes where the
    market either converges to fair value or does not."""
    db = open_state_db(Path(tempfile.mkdtemp()) / "s.db")
    tape = open_tape_db(Path(tempfile.mkdtemp()) / "t.db")
    rec = TapeRecorder(tape)
    for k in range(n_matches):
        t = f"KXATPMATCH-26SEP28M{k:02d}-AAA"
        base = T0 + k * 2000
        for i in range(0, 700, 2):
            mid = 50.5 + (min(i, 120) / 120 * 5 if gap_follows else 0)
            rec.record_quotes({t: quote(t, base + i, mid - 0.5, mid + 0.5, volume=i)}, keepalive_s=6)
        for j in range(0, 600, 30):
            db.insert("dislocations", {"ts": base + j, "env": "PAPER_EDGE", "ticker": t, "match_event": t[:-4],
                                       "kind": "MATCH", "mid": 50.5 if j == 0 else None, "fair": 55.5, "gap": 5.0 if j == 0 else 0.1,
                                       "confidence": 0.8, "state": "sets 0-0, games 2-2"})
        tape.execute("INSERT INTO market_results VALUES (?,?,?,?,?,?)", (t, t[:-4], "yes" if k % 2 else "no", "finalized", 0, 0))
    return db, tape


class TestGapStudy(unittest.TestCase):
    def test_detects_convergence(self):
        db, tape = setup(True)
        rep = studies.gap_study(db, tape, T0 - 10, T0 + 20000)
        big = [r for r in rep["table"] if r["gap"] == "5-8c" and r["horizon_s"] == 120]
        self.assertTrue(big and big[0]["mean_move_toward_fair_c"] > 4)
        self.assertEqual(big[0]["matches"], 8)
        self.assertIn("PREDICTS MOVEMENT", rep["verdict"])

    def test_no_convergence_no_claim(self):
        db, tape = setup(False)
        rep = studies.gap_study(db, tape, T0 - 10, T0 + 20000)
        self.assertEqual(rep["verdict"], "NO EVIDENCE THE GAP PREDICTS MOVEMENT")

    def test_small_samples_are_flagged(self):
        db, tape = setup(True, n_matches=2)
        rep = studies.gap_study(db, tape, T0 - 10, T0 + 20000)
        self.assertTrue(all(not r["reliable"] for r in rep["table"]))
        self.assertEqual(rep["verdict"], "INSUFFICIENT DATA")


class TestOtherStudies(unittest.TestCase):
    def test_decay_curve(self):
        db, tape = setup(True)
        rep = studies.decay_study(db, tape, T0 - 10, T0 + 20000)
        rem = {r["after_s"]: r["median_remaining"] for r in rep["curve"]}
        self.assertGreater(rem[10], rem[120])
        self.assertIsNotNone(rep["median_half_life_s"])

    def test_calibration_scores_model_and_market(self):
        db, tape = setup(True)
        rep = studies.calibration_study(db, tape, T0 - 10, T0 + 20000)
        self.assertGreater(rep["observations"], 0)
        self.assertIn("brier", rep["model"])
        self.assertIn("brier", rep["market"])

    def test_funnel_stages(self):
        db, tape = setup(True)
        rep = studies.funnel_study(db, tape, T0 - 10, T0 + 20000)
        self.assertEqual(rep["funnel"][0]["stage"], "market moves >=3c in <=60s (tennis)")


class TestValidationReport(unittest.TestCase):
    def test_not_ready_without_evidence(self):
        from lvt.edge.validation import validation_report
        from tests.helpers import cfg
        db, tape = setup(True)
        rep = validation_report(cfg(), db, tape)
        self.assertEqual(len(rep["criteria"]), 10)
        self.assertTrue(rep["recommendation"].startswith("NOT READY"))
        by = {c["criterion"][:1]: c["status"] for c in rep["criteria"]}
        self.assertEqual(by["4"], "INSUFFICIENT")          # paper fills can never validate execution


class TestRegistry(unittest.TestCase):
    def test_hypothesis_required(self):
        from lvt.__main__ import main
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["experiment", "add", "--name", "x"])
        self.assertEqual(rc, 2)
        self.assertIn("hypothesis", buf.getvalue())


class TestReplayIntegrity(unittest.TestCase):
    def test_future_scores_cannot_leak_into_earlier_decisions(self):
        """Adversarial replay (V2 brief section 63): a dramatic score change
        recorded later must not alter any decision taken before it."""
        from lvt.data.context import normalize_live_data
        from lvt.replay import run_replay
        from tests.edge_helpers import EVENT, META, TA, TB, market, raw
        from tests.helpers import cfg
        tmp = Path(tempfile.mkdtemp())

        def build(with_future_event: bool):
            tape = open_tape_db(tmp / f"tape{int(with_future_event)}.db")
            rec = TapeRecorder(tape)
            rec.record_markets([market(TA, "Alpha Aaa"), market(TB, "Beta Bbb")], T0)
            rec.record_event_meta(EVENT, META, T0)
            for i in range(0, 600, 2):
                m = 60.5 if i < 300 else 55.5
                b = float(int(m - 0.5))
                rec.record_quotes({TA: quote(TA, T0 + i, b, b + 1, volume=i), TB: quote(TB, T0 + i, 98 - b, 99 - b, volume=i)},
                                  keepalive_s=6)
                if i % 4 == 0:
                    late = with_future_event and i >= 500
                    st = raw(sets=(0, 1), games=(0, 0), points=(0, 0)) if late else raw()
                    rec.last_ctx.clear()
                    rec.record_context(normalize_live_data(EVENT, "tennis_tournament_singles", st, T0 + i))
            return tape

        c = cfg({"execution": {"entry_style": "maker", "exit_style": "maker"}, "maker": {"take_profit": True},
                 "strategies": {k: {"enabled": False} for k in ("DIP", "REVERSAL", "MOMENTUM", "DOWNTREND")}})
        a = run_replay(c, build(False), 0, 1e12, tmp / "a", edge=True, label=False)
        b = run_replay(c, build(True), 0, 1e12, tmp / "b", edge=True, label=False)
        q = ("SELECT ts, ticker, strategy, action, round(score,3) s FROM signals WHERE ts < ? ORDER BY ts, ticker, strategy")
        cut = T0 + 499
        self.assertEqual(open_state_db(a["db_path"]).query(q, (cut,)), open_state_db(b["db_path"]).query(q, (cut,)))


if __name__ == "__main__":
    unittest.main()
