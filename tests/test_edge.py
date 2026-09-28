"""EDGE_ENGINE_V1: reference values, event responses, dislocation /
momentum-completion classification, exits, correlation, diagnostics,
counterfactuals, A/B report, replay and account isolation."""
import json
import tempfile
import unittest
from pathlib import Path

from lvt.db import open_state_db, open_tape_db
from lvt.edge.ab import ab_report
from lvt.edge.diagnostics import classify_trade, counterfactuals, label_event_responses
from lvt.edge.engine import severity, state_tags
from lvt.edge.reference import A, B, map_player, parse_state
from lvt.edge.tennis_model import TennisState
from lvt.execution.reconcile import reconcile
from lvt.models import FillEvent
from tests.edge_helpers import EVENT, META, TA, TB, EdgeHarness, raw
from tests.helpers import T0, quote

MAKER = {"execution": {"entry_style": "maker", "exit_style": "maker"}, "maker": {"take_profit": True}}


def calm(x, secs=300, mid=60.5):
    x.run([mid] * int(secs / 2))


class TestReference(unittest.TestCase):
    def test_parse_state(self):
        r = raw(sets=(1, 0), games=(3, 4), points=(3, 2), server="c2")
        st = parse_state(r, 3)
        self.assertEqual((st.sets, st.games, st.points, st.server), ((1, 0), (3, 4), (3, 2), B))
        r["advantage"], r["competitor1_current_round_score"], r["competitor2_current_round_score"] = "c2", 40, 50
        self.assertEqual(parse_state(r, 3).points, (3, 4))
        tb = raw(games=(6, 6), server="c1")
        tb["competitor1_current_round_score"], tb["competitor2_current_round_score"] = 3, 2
        st = parse_state(tb, 3)
        self.assertTrue(st.in_tiebreak)
        self.assertEqual(st.points, (3, 2))
        self.assertIsNone(parse_state({**raw(), "server": ""}, 3))
        self.assertIsNone(parse_state({**raw(), "status": "not_started"}, 3))

    def test_player_mapping(self):
        self.assertEqual(map_player(TA, EVENT, "Alpha Aaa", "Aaa vs Bbb"), A)
        self.assertEqual(map_player(TB, EVENT, "Beta Bbb", "Aaa vs Bbb"), B)
        self.assertIsNone(map_player(TA, EVENT, "Beta Bbb", "Aaa vs Bbb"))          # code and name disagree

    def test_reference_needs_history_then_moves_only_with_the_score(self):
        x = EdgeHarness(MAKER)
        calm(x, 60)
        self.assertIsNone(x.edge.refs.reference(TA, T0 + x.t)[0])                  # not anchored yet
        calm(x, 240)
        r0 = x.edge.refs.reference(TA, T0 + x.t)[0]
        self.assertAlmostEqual(r0.value, 60.5, delta=0.6)                           # anchored to the market
        self.assertTrue(r0.lo <= r0.value <= r0.hi)
        x.run([50.5] * 5)                                                           # market moves, score doesn't
        self.assertAlmostEqual(x.edge.refs.reference(TA, T0 + x.t)[0].value, r0.value, delta=0.5)
        x.score(games=(2, 3), points=(0, 0), server="c2")                           # break against A
        x.run([50.5] * 3)
        self.assertLess(x.edge.refs.reference(TA, T0 + x.t)[0].value, r0.value - 8)

    def test_stale_score_means_no_reference(self):
        x = EdgeHarness(MAKER)
        calm(x)
        ref, why = x.edge.refs.reference(TA, T0 + x.t + 60)
        self.assertIsNone(ref)
        self.assertIn("not trusted", why)

    def test_tags_and_severity(self):
        bp = TennisState((0, 0), (2, 2), (1, 3), A)                                   # 15-40 on A's serve
        self.assertIn("BREAK_POINT", state_tags(bp))
        sp = TennisState((0, 0), (5, 3), (3, 1), A)
        self.assertIn("SET_POINT", state_tags(sp))
        mp = TennisState((1, 0), (5, 4), (3, 0), A)
        self.assertIn("MATCH_POINT", state_tags(mp))
        self.assertEqual([severity(x) for x in (0.2, 2, 5, 12)], ["ORDINARY", "MODERATE", "MAJOR", "CRITICAL"])


class TestClassification(unittest.TestCase):
    def test_overreaction_is_bought_only_after_confirmation(self):
        x = EdgeHarness(MAKER)
        calm(x)
        x.score(points=(1, 2))                                                      # ordinary point
        x.run([58.5, 56.5, 54.5, 52.5, 50.5] + [50.5] * 6 + [51.5] * 8)
        watch = x.signals(strategy="EDGE_DISLOCATION_REVERSAL", action="WATCH")
        buys = x.signals(strategy="EDGE_DISLOCATION_REVERSAL", action="BUY")
        self.assertTrue(watch and buys)
        self.assertLess(watch[0]["ts"], buys[0]["ts"])
        self.assertEqual((buys[0]["ticker"], buys[0]["side"], buys[0]["decision"]), (TA, "yes", "TAKEN"))
        exp = json.loads(buys[0]["explanation"])
        self.assertGreater(exp["overreaction"], 3)
        self.assertIn("REFERENCE:", buys[0]["text"])
        order = x.h.db.one("SELECT * FROM orders")
        self.assertTrue(json.loads(json.dumps(order))["limit_price"] < exp["reference"])

    def test_justified_repricing_is_not_traded(self):
        x = EdgeHarness(MAKER)
        calm(x)
        r0 = x.edge.refs.reference(TA, T0 + x.t)[0].value
        x.run([45.5, 45.5])                                   # market reprices before our poll sees the break
        x.score(games=(2, 3), points=(0, 0), server="c2")
        x.run([r0 - 15.0] * 12)
        self.assertEqual(x.signals(action="BUY"), [])
        self.assertTrue(x.signals(strategy="EDGE_EVENT_REPRICING"))

    def test_unfinished_momentum_buys_the_side_the_score_favours(self):
        x = EdgeHarness(MAKER)
        calm(x)
        x.score(games=(2, 3), points=(0, 0), server="c2")
        x.run([60.5, 59.5, 59.5, 58.5])
        buys = x.signals(action="BUY")
        self.assertEqual(buys[0]["strategy"], "EDGE_UNFINISHED_MOMENTUM")
        self.assertEqual((buys[0]["ticker"], buys[0]["side"]), (TB, "yes"))
        self.assertLess(json.loads(buys[0]["explanation"])["move_completion"], 0.7)

    def test_one_position_per_match(self):
        x = EdgeHarness(MAKER)
        calm(x)
        x.score(games=(2, 3), points=(0, 0), server="c2")
        x.run([60.5, 59.5, 59.5, 58.5, 58.0, 57.5])
        self.assertEqual(x.h.db.scalar("SELECT COUNT(*) FROM positions"), 1)

    def test_taker_costs_can_kill_the_same_setup(self):
        x = EdgeHarness()                                    # taker entry + taker exit
        calm(x)
        x.score(points=(1, 2))
        x.run([58.5, 56.5, 54.5, 52.5, 50.5] + [50.5] * 6 + [51.5] * 8)
        self.assertEqual(x.signals(action="BUY"), [])
        self.assertTrue(any("does not cover costs" in r["text"] for r in x.signals()))


class TestExitsAndEvents(unittest.TestCase):
    def _open(self, x, entry=51.0, qty=20):
        from lvt.models import Signal
        sig = Signal("s1", T0, TA, EVENT, "EDGE_DISLOCATION_REVERSAL", "v", "yes", "BUY", "CONFIRMED",
                     score=70, bid=entry - 1, ask=entry, mid=entry - 0.5, target=57, stop=45)
        p = x.h.engine.book.open_pending(sig, x.h.markets[TA], T0)
        x.h.engine.book.apply_entry_fill(p, FillEvent("f", T0, entry, qty, 0.0))
        return p

    def test_reference_reached_and_thesis_invalidated(self):
        x = EdgeHarness(MAKER)
        calm(x)
        p = self._open(x)
        ref = x.edge.refs.reference(TA, T0 + x.t)[0].value
        from tests.helpers import snap
        s = snap(TA, T0 + x.t, ref - 0.5, ref + 0.5)
        self.assertEqual(x.edge.exit_check(p, s, T0 + x.t).reason, "REFERENCE_VALUE_REACHED")
        x.score(sets=(0, 1), games=(0, 0), points=(0, 0))                            # A loses a set
        x.run([60.5] * 2)
        s = snap(TA, T0 + x.t, 50, 51)
        d = x.edge.exit_check(p, s, T0 + x.t)
        self.assertEqual(d.reason, "THESIS_INVALIDATED")

    def test_event_responses_recorded_and_labeled(self):
        x = EdgeHarness(MAKER)
        tape = open_tape_db(Path(tempfile.mkdtemp()) / "t.db")
        calm(x)
        x.score(games=(2, 3), points=(0, 0), server="c2")
        x.run([60.5])
        rows = x.h.db.query("SELECT * FROM event_responses")
        self.assertEqual(len(rows), 2)                                              # both players' markets
        a = next(r for r in rows if r["ticker"] == TA)
        self.assertLess(a["expected_impact"], -8)
        self.assertIn("BREAK", a["kinds"])
        t0 = a["ts"]
        for i in range(-10, 200, 2):
            m = 60.5 if i < 0 else 45.5
            tape.execute("INSERT INTO quotes VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (t0 + i, TA, m - 0.5, m + 0.5, 100, 100, None, 0, 0, "active"))
        self.assertGreaterEqual(label_event_responses(x.h.db, tape, t0 + 400), 1)
        a = x.h.db.one("SELECT * FROM event_responses WHERE ticker=?", (TA,))
        self.assertAlmostEqual(a["actual_60s"], -15.0, delta=0.1)
        self.assertEqual(a["response"], "PROPORTIONATE")


class TestDiagnosticsAndCounterfactuals(unittest.TestCase):
    def pos(self, **kw):
        base = {"realized_pnl_usd": -1.0, "strategy": "EDGE_DISLOCATION_REVERSAL", "opened_ts": 0, "closed_ts": 50,
                "exit_reason": "MAX_LOSS_EXIT", "avg_entry": 50, "target": 57, "stop": 46, "mfe_cents": 0,
                "side": "yes", "slippage_cost_usd": 0.0}
        base.update(kw)
        return base

    def test_classifications(self):
        self.assertEqual(classify_trade(self.pos(realized_pnl_usd=1, exit_reason="REFERENCE_VALUE_REACHED"), None, [], [])[1],
                         "REFERENCE_VALUE_CONVERGENCE")
        self.assertEqual(classify_trade(self.pos(), None, [], [{"expected_impact": -8}])[1], "EVENT_REPRICING")
        self.assertEqual(classify_trade(self.pos(mfe_cents=4), None, [], [])[1], "CORRECT_ENTRY_WRONG_EXIT")
        self.assertEqual(classify_trade(self.pos(), None, [(60, 58)], [])[1], "CORRECT_DIRECTION_WRONG_ENTRY")
        self.assertEqual(classify_trade(self.pos(strategy="MOMENTUM"), None, [], [])[1], "MOMENTUM_FAILURE")
        self.assertEqual(classify_trade(self.pos(), None, [(60, 40)], [])[1], "CONTINUED_COLLAPSE")
        self.assertEqual(classify_trade(self.pos(slippage_cost_usd=0.8), None, [], [])[1], "SLIPPAGE")

    def test_counterfactual_is_labelled_and_separate(self):
        db = open_state_db(Path(tempfile.mkdtemp()) / "s.db")
        tape = open_tape_db(Path(tempfile.mkdtemp()) / "t.db")
        db.insert("signals", {"id": "s1", "env": "PAPER_EDGE", "ts": T0, "ticker": "X", "side": "yes",
                              "decision": "NO_EDGE", "ask": 50.0, "target": 55.0, "stop": 45.0})
        for i, b in enumerate([50, 52, 55, 56]):
            tape.execute("INSERT INTO quotes VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (T0 + 10 * (i + 1), "X", b, b + 1, 100, 100, None, 0, 0, "active"))
        self.assertEqual(counterfactuals(db, tape, T0 + 2000), 1)
        c = db.one("SELECT * FROM counterfactuals")
        self.assertEqual(c["label"], "COUNTERFACTUAL")
        self.assertGreater(c["hyp_pnl_cents"], 0)
        self.assertEqual(c["time_to_target_s"], 30)
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM positions"), 0)               # never P&L


class TestABAndIsolation(unittest.TestCase):
    def test_ab_report_structure_and_no_false_verdict(self):
        db = open_state_db(Path(tempfile.mkdtemp()) / "s.db")
        for i in range(5):
            db.insert("positions", {"id": f"b{i}", "env": "PAPER", "status": "CLOSED", "sport": "TENNIS",
                                    "strategy": "DIP", "opened_ts": T0 + i, "closed_ts": T0 + i + 50,
                                    "realized_pnl_usd": -1.0})
            db.insert("positions", {"id": f"e{i}", "env": "PAPER_EDGE", "status": "CLOSED", "sport": "TENNIS",
                                    "strategy": "EDGE_DISLOCATION_REVERSAL", "opened_ts": T0 + i,
                                    "closed_ts": T0 + i + 50, "realized_pnl_usd": 1.0})
        rep = ab_report(db)
        self.assertIn("INSUFFICIENT", rep["comparison_vs_baseline_tennis"]["verdict"])
        self.assertEqual(rep["challenger"]["trades"], 5)
        self.assertEqual(rep["primary_experiment"]["enhanced_dip_expectancy"]["trades"], 5)

    def test_accounts_are_isolated(self):
        """Baseline and challenger trade the same market without touching each
        other's positions or venue ledgers."""
        x = EdgeHarness(MAKER)
        calm(x)
        x.score(games=(2, 3), points=(0, 0), server="c2")
        x.run([60.5, 59.5, 59.5, 58.5])
        other_db = x.h.db
        self.assertEqual(other_db.scalar("SELECT COUNT(*) FROM positions WHERE env='PAPER'"), 0)
        self.assertTrue(reconcile(x.h.engine.book, x.h.venue, []).ok)


class TestEdgeReplay(unittest.TestCase):
    def test_replay_feeds_recorded_scores_in_time_order(self):
        from lvt.data.context import normalize_live_data
        from lvt.replay import run_replay
        from lvt.tape import TapeRecorder
        from tests.edge_helpers import market
        tmp = Path(tempfile.mkdtemp())
        tape = open_tape_db(tmp / "tape.db")
        rec = TapeRecorder(tape)
        rec.record_markets([market(TA, "Alpha Aaa"), market(TB, "Beta Bbb")], T0)
        rec.record_event_meta(EVENT, META, T0)
        for i in range(0, 340, 2):
            m = 60.5 if i < 300 else [60.5, 59.5, 59.5, 58.5, 58.5, 58.0, 58.0, 57.5][min((i - 300) // 2, 7)]
            b = float(int(m - 0.5))
            rec.record_quotes({TA: quote(TA, T0 + i, b, b + 1, volume=i),
                               TB: quote(TB, T0 + i, 98 - b, 99 - b, volume=i)}, keepalive_s=6)
            if i % 4 == 0:
                state = raw() if i < 300 else raw(games=(2, 3), points=(0, 0), server="c2")
                rec.last_ctx.clear()
                rec.record_context(normalize_live_data(EVENT, "tennis_tournament_singles", state, T0 + i))
        from tests.helpers import cfg
        c = cfg({**MAKER, "strategies": {k: {"enabled": False} for k in ("DIP", "REVERSAL", "MOMENTUM", "DOWNTREND")}})
        s = run_replay(c, tape, 0, 1e12, tmp / "bt", edge=True, label=False)
        db = open_state_db(s["db_path"])
        self.assertTrue(db.query("SELECT * FROM signals WHERE strategy='EDGE_UNFINISHED_MOMENTUM' AND action='BUY'"))
        self.assertIn("EDGE", s["strategy_versions"])


if __name__ == "__main__":
    unittest.main()
