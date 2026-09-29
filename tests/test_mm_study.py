"""EXP-008 market-making study: the replay fills like paper trading does,
scores adverse selection honestly, and applies the pre-registered rule."""
import tempfile
import unittest
from pathlib import Path

from lvt.db import open_tape_db
from lvt.edge import mm_study as mm
from lvt.models import Trade
from lvt.tape import TapeRecorder
from tests.helpers import T0, market

TK = "KXATPMATCH-26SEP29AAABBB-AAA"
ONE = {"JOIN": (False, 2.0, False)}


def book(t0, t1, bid, ask, size=5.0, step=2.0):
    out, t = [], t0
    while t < t1:
        out.append((T0 + t, bid, ask, size, size, 0.0, "active"))
        t += step
    return out


def sell_yes(t, px, n=10):      # taker sells YES = buys NO: hits the YES bid
    return Trade(TK, T0 + t, px, n, "no")


def buy_yes(t, px, n=10):       # taker buys YES: lifts the YES ask = hits the NO bid
    return Trade(TK, T0 + t, px, n, "yes")


def with_markouts(fills, quotes):
    mids = mm._Mids([q for q in quotes if q[1] is not None])
    for f in fills:
        m = mids.at(f["ts"] + 60)
        f["net_60"] = None if m is None else (m if f["side"] == "yes" else 100 - m) - f["price"] - f["fee_c"]
    return fills


class TestSimulator(unittest.TestCase):
    def test_bouncing_market_pays_the_maker(self):
        q = book(0, 400, 50, 53)
        tr = [sell_yes(t, 50) if i % 2 else buy_yes(t, 53) for i, t in enumerate(range(5, 300, 7))]
        fills, quoted = mm.simulate_market(TK, market(TK), q, tr, [], variants=ONE)
        fills = with_markouts(fills, q)
        self.assertTrue(fills)
        self.assertEqual({f["side"] for f in fills}, {"yes", "no"})
        self.assertTrue(all(abs(f["net_60"] - 1.5) < 1e-9 for f in fills if f["net_60"] is not None))
        self.assertGreater(quoted["JOIN"], 300)

    def test_gap_down_is_adverse_selection(self):
        q = book(0, 20, 50, 53) + book(20, 200, 44, 47)
        fills, _ = mm.simulate_market(TK, market(TK), q, [], [], variants=ONE)
        fills = with_markouts(fills, q)
        yes = [f for f in fills if f["side"] == "yes"]
        self.assertEqual(len(yes), 1)
        self.assertEqual((yes[0]["price"], yes[0]["why"]), (50.0, "touch"))
        self.assertLess(yes[0]["net_60"], -4)

    def test_queue_ahead_and_latency(self):
        q = book(0, 100, 50, 53, size=20)
        # 0.2 s after the first quote (before arrival) and then only 15 lots at our price: all queue ahead
        tr = [sell_yes(0.2, 50, 50), sell_yes(10, 50, 15)]
        fills, _ = mm.simulate_market(TK, market(TK), q, tr, [], variants=ONE)
        self.assertFalse(fills)
        fills, _ = mm.simulate_market(TK, market(TK), q, tr + [sell_yes(20, 50, 6)], [], variants=ONE)
        self.assertEqual([f["why"] for f in fills], ["queue"])

    def test_quote_gap_cancels(self):
        q = book(0, 10, 50, 53) + book(40, 60, 50, 53)
        tr = [sell_yes(25, 49, 10)]                             # during the gap: we were not quoting
        fills, _ = mm.simulate_market(TK, market(TK), q, tr, [], variants=ONE)
        self.assertFalse(fills)

    def test_score_guard_pulls_quotes(self):
        q = book(0, 100, 50, 53)
        tr = [sell_yes(t, 49) for t in range(20, 100, 10)]     # trades through our bid
        v = {"JOIN": (False, 2.0, False), "JOIN+GUARD": (False, 2.0, True)}
        fills, _ = mm.simulate_market(TK, market(TK), q, tr, [T0 + 15], variants=v)
        by = {k: [f for f in fills if f["variant"] == k] for k in v}
        self.assertTrue(by["JOIN"])
        self.assertTrue(all(f["ts"] >= T0 + 30 for f in by["JOIN+GUARD"]))

    def test_improve_joins_at_front_of_queue(self):
        q = book(0, 60, 50, 53, size=100)
        tr = [sell_yes(10, 50, 1)]                              # a 1-lot at the old bid goes THROUGH our 51 bid
        fills, _ = mm.simulate_market(TK, market(TK), q, tr, [], variants={"IMPROVE_1C": (True, 3.0, False)})
        self.assertEqual([(f["price"], f["why"]) for f in fills], [(51.0, "through")])


class TestDecision(unittest.TestCase):
    @staticmethod
    def s(m, ci=(0.1, 0.5), n=400, k=40):
        return {"fills": n, "matches": k, "net_markout_60s_c": m, "ci95_60s": ci}

    def test_rules(self):
        dev = {v: self.s(-0.5) for v in mm.VARIANTS}
        self.assertEqual(mm._decide(dev, dev, 300, 30)["verdict"], "KILL")
        dev["JOIN"] = self.s(0.4)
        test = {v: self.s(0.9) for v in mm.VARIANTS}
        test["JOIN"] = self.s(0.3, ci=(-0.1, 0.7))               # CI reaches 0 -> KILL
        self.assertEqual(mm._decide(dev, test, 300, 30)["verdict"], "KILL")
        test["JOIN"] = self.s(0.3, ci=(0.05, 0.6))
        self.assertEqual(mm._decide(dev, test, 300, 30)["verdict"], "SURVIVES")
        self.assertEqual(mm._decide({v: self.s(1.0, n=50) for v in mm.VARIANTS}, test, 300, 30)["verdict"],
                         "INSUFFICIENT")


class TestEndToEnd(unittest.TestCase):
    def test_study_runs_on_tape(self):
        tape = open_tape_db(Path(tempfile.mkdtemp()) / "t.db")
        rec = TapeRecorder(tape)
        for k in range(4):
            tk = f"KXATPMATCH-26SEP29A{k}ABB{k}-A{k}A"
            rec.record_markets([market(tk)], T0)
            base = k * 1000
            for row in book(base, base + 400, 50, 53):
                tape.execute("INSERT INTO quotes VALUES (?,?,?,?,?,?,?,?,?,?)", (row[0], tk, 50, 53, 5, 5, None, 0, 0, "active"))
            for i, t in enumerate(range(base + 5, base + 300, 7)):
                tape.execute("INSERT INTO trades VALUES (?,?,?,?,?,?)", (T0 + t, tk, 50 if i % 2 else 53, 10,
                                                                        "no" if i % 2 else "yes", f"x{k}-{i}"))
            tape.execute("INSERT INTO market_results VALUES (?,?,?,?,?,?)", (tk, tk.rsplit("-", 1)[0], "yes", "finalized", 0, 0))
        rep = mm.mm_study(tape, 0, 1e12, min_fills=10, min_matches=1)
        self.assertEqual(rep["matches"], 4)
        s = rep["scores"]["ALL"]["JOIN"]
        self.assertAlmostEqual(s["net_markout_60s_c"], 1.5, places=3)
        self.assertIn(rep["decision"]["verdict"], ("SURVIVES", "KILL", "INSUFFICIENT"))
        self.assertIn("EXP-008", mm.format_mm_study(rep))


if __name__ == "__main__":
    unittest.main()
