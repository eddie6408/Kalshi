"""EXP-006 offline model study: outcome orientation, no look-ahead in the
anchors, and the pre-registered decision rule."""
import json
import tempfile
import unittest
from pathlib import Path

from lvt.db import open_tape_db
from lvt.edge import model_study as ms
from lvt.edge.reference import parse_state
from lvt.edge.tennis_model import p_match
from lvt.models import MarketInfo
from lvt.tape import TapeRecorder
from tests.edge_helpers import raw
from tests.helpers import T0

EDGE = {"anchor_max_spread_cents": 4.0, "anchor_min_samples": 5}


def build(n_matches=6, legs=("A", "B"), c1_wins=True, d=0.1, dur=1500):
    """Matches at a constant score where the market prices P(c1) exactly as
    the model does with strength difference d."""
    tape = open_tape_db(Path(tempfile.mkdtemp()) / "t.db")
    rec = TapeRecorder(tape)
    st = parse_state(raw(), 3)
    p1 = p_match(0.64 + d / 2, 0.64 - d / 2, st)
    evs = []
    for k in range(n_matches):
        na, nb = f"Aa{chr(97 + k)}", f"Bb{chr(97 + k)}"
        ev = f"KXATPMATCH-26SEP28{na.upper()}{nb.upper()}"
        evs.append(ev)
        base = T0 + k * 3000
        tk = {"A": f"{ev}-{na.upper()}", "B": f"{ev}-{nb.upper()}"}
        rec.record_markets([MarketInfo(ticker=tk[x], event_ticker=ev, series_ticker="KXATPMATCH", category="Sports",
                                       sport="TENNIS", title=f"{n} wins", sub_title=n, close_ts=base + 86400,
                                       fee_type="quadratic", fee_multiplier=1.0)
                            for x, n in (("A", na), ("B", nb)) if x in legs], base)
        rec.record_event_meta(ev, {"milestone_id": f"m{k}", "type": "tennis_tournament_singles", "title": f"{na} vs {nb}",
                                   "details": {"best_of": "3", "tour": "ATP", "gender": "men"}, "group": [ev]}, base)
        for i in range(0, dur, 20):
            tape.execute("INSERT INTO context VALUES (?,?,?,?,?,?,?)",
                         (base + i, ev, "kalshi", "TENNIS", "live", 0, json.dumps({"raw": raw()})))
        for i in range(0, dur + 200, 2):
            for x in legs:
                mid = 100 * p1 if x == "A" else 100 - 100 * p1
                tape.execute("INSERT INTO quotes (ts, ticker, yes_bid, yes_ask) VALUES (?,?,?,?)",
                             (base + i, tk[x], mid - 0.5, mid + 0.5))
        for x in legs:
            won = (x == "A") == c1_wins
            tape.execute("INSERT INTO market_results VALUES (?,?,?,?,?,?)", (tk[x], ev, "yes" if won else "no", "finalized", 0, 0))
    return tape, evs, p1


class TestModelStudy(unittest.TestCase):
    def test_runs_and_orients_outcomes(self):
        for legs in (("A", "B"), ("B",)):                      # the B leg alone must still mean P(c1)
            tape, _, p1 = build(legs=legs)
            rep = ms.model_study(tape, 0, 1e12, EDGE)
            self.assertEqual(rep["matches"], 6)
            self.assertEqual(rep["dev_matches"], 3)
            s = rep["scores"]["ALL"]["V1_600_60"]
            self.assertAlmostEqual(s["market_brier"], round((p1 - 1) ** 2, 4), places=3)
            self.assertAlmostEqual(s["model_brier"], s["market_brier"], places=3)   # anchored model reproduces the market
            self.assertEqual(rep["decision"]["verdict"], "INSUFFICIENT")

    def test_anchors_never_look_ahead(self):
        tape, evs, _ = build(n_matches=1)
        from lvt.tape import load_event_meta
        meta = load_event_meta(tape)[evs[0]]
        legs = {0: f"{evs[0]}-AAA", 1: f"{evs[0]}-BBA"}
        obs = ms._match_obs(tape, evs[0], legs, 1.0, meta, EDGE, 0, 1e12, 30, 20, 4.0, 5, 40, 300, 10)
        t0 = obs[0]["ts"]
        for o in obs:
            if "FIXED_EARLY" in o["preds"]:
                self.assertGreaterEqual(o["ts"], t0 + 4 * 20)     # 5th anchor sample exists
            if "V1_600_60" in o["preds"]:
                self.assertGreaterEqual(o["ts"], t0 + 60 + 4 * 20)
            if "LONG_1800_60" in o["preds"]:
                self.assertGreaterEqual(o["ts"], t0 + 60 + 4 * 20)
        self.assertTrue(all("SERVE_STATS" in o["preds"] for o in obs))
        self.assertTrue(all(o["fut"][120] is None or o["ts"] + 120 <= t0 + 1700 for o in obs))


class TestDecisionRule(unittest.TestCase):
    @staticmethod
    def s(diff, n=400, m=40):
        return {"n": n, "disagree": {"n": n, "matches": m, "diff": diff}}

    def test_kill_when_nothing_beats_market_on_dev(self):
        dev = {v: self.s(0.01) for v in ms.VARIANTS}
        self.assertEqual(ms._decide(dev, dev, 300, 30)["verdict"], "KILL")

    def test_insufficient_below_floor(self):
        dev = {v: self.s(-0.05, n=100) for v in ms.VARIANTS}
        self.assertEqual(ms._decide(dev, dev, 300, 30)["verdict"], "INSUFFICIENT")
        dev = {v: self.s(-0.05, m=10) for v in ms.VARIANTS}
        self.assertEqual(ms._decide(dev, dev, 300, 30)["verdict"], "INSUFFICIENT")

    def test_chosen_on_dev_judged_on_test(self):
        dev = {v: self.s(0.02) for v in ms.VARIANTS}
        dev["SERVE_STATS"] = self.s(-0.01)
        dev["SHORT_300_10"] = self.s(-0.02)
        test = {v: self.s(-0.05) for v in ms.VARIANTS}
        test["SHORT_300_10"] = self.s(0.001)                    # the dev winner fails on test -> KILL,
        r = ms._decide(dev, test, 300, 30)                      # even though another variant looks good there
        self.assertEqual((r["verdict"], r["chosen"]), ("KILL", "SHORT_300_10"))
        test["SHORT_300_10"] = self.s(-0.001)
        self.assertEqual(ms._decide(dev, test, 300, 30)["verdict"], "SURVIVES")


class TestLongshots(unittest.TestCase):
    def test_one_sample_per_match_and_costs(self):
        obs = [{"match": f"m{k}", "ts": k, "market": 0.85, "y": 1.0 if k % 3 else 0.0, "spread": 2.0} for k in range(30)]
        obs += [{"match": "m0", "ts": 99, "market": 0.15, "y": 0.0, "spread": 2.0}]
        r = ms._longshots(obs)
        self.assertEqual(r["n"], 30)                             # the NO side at 15c, once per match
        self.assertAlmostEqual(r["mean_price"], 0.15, places=3)
        self.assertGreater(r["breakeven_rate"], 0.15)
        self.assertTrue(r["status"].startswith("INSUFFICIENT"))


if __name__ == "__main__":
    unittest.main()
