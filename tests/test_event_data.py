"""Phase 1 of the edge-engine brief: dense live-score scheduling, match-group
companion markets, the sport-event timeline and data-quality flags."""
import unittest

from lvt.data.context import normalize_live_data
from lvt.data.events import EventDetector
from lvt.data.schedule import PollState, companion_tickers, plan_context_polls
from tests.helpers import cfg


def raw(p1="0", p2="0", g1=0, g2=0, sets=(0, 0), pw=(0, 0), hold=(0, 0), brk=(0, 0), status="1st_set"):
    return {"match_status": status, "competitor1_current_round_score": p1, "competitor2_current_round_score": p2,
            "competitor1_round_scores": [{"score": g1}], "competitor2_round_scores": [{"score": g2}],
            "competitor1_overall_score": sets[0], "competitor2_overall_score": sets[1],
            "competitor1_statistics": {"points_won": pw[0], "service_games_won": hold[0], "breakpoints_won": brk[0]},
            "competitor2_statistics": {"points_won": pw[1], "service_games_won": hold[1], "breakpoints_won": brk[1]}}


def ctx(r, fetched, provider_ts=0.0):
    c = normalize_live_data("KXATPMATCH-X", "tennis_tournament_singles", r, fetched)
    c.event_ts = provider_ts
    return c


class TestSchedule(unittest.TestCase):
    def setUp(self):
        self.c = cfg().context

    def test_live_tennis_polled_densely_and_first(self):
        now = 10_000.0
        st = {"T": PollState(last_ts=now - 5, status="live", sport="TENNIS"),
              "F": PollState(last_ts=now - 10, status="live", sport="FOOTBALL"),
              "S": PollState(last_ts=0, status="scheduled", sport="TENNIS")}
        plan = plan_context_polls(["F", "S", "T"], st, now, self.c, lambda e: True)
        self.assertEqual(plan[0], "T")                     # 4s interval elapsed, live tennis first
        self.assertIn("S", plan)                           # never polled
        self.assertNotIn("F", plan)                        # football waits its 15s
        self.assertEqual(plan_context_polls(["T"], st, now - 2, self.c, lambda e: True), [])

    def test_budget_backoff_and_coverage(self):
        st = {f"E{i}": PollState(status="live", sport="TENNIS") for i in range(10)}
        st["BAD"] = PollState(failed_until=10_000)
        plan = plan_context_polls(list(st) + ["NOPE"], st, 1000, self.c, lambda e: e != "NOPE")
        self.assertEqual(len(plan), self.c.max_live_data_requests_per_cycle)
        self.assertNotIn("BAD", plan)
        self.assertNotIn("NOPE", plan)

    def test_companions_only_for_live_matches_and_capped(self):
        groups = {"M1": ("M1", "SET1", "SPREAD1"), "M2": ("M2", "SET2")}
        markets = {"M1": ["M1-A", "M1-B"], "SET1": ["SET1-A", "SET1-B"], "SPREAD1": ["SP-1"], "SET2": ["SET2-A"]}
        out = companion_tickers(["M1", "M2"], groups, {"M1": "live", "M2": "scheduled"}, markets, {"M1-A"}, 10)
        self.assertEqual(out, ["M1-B", "SET1-A", "SET1-B", "SP-1"])   # hot ticker excluded, M2 not live
        self.assertEqual(len(companion_tickers(["M1"], groups, {"M1": "live"}, markets, set(), 2)), 2)


class TestEventTimeline(unittest.TestCase):
    def test_point_hold_break_set_and_status(self):
        d = EventDetector()
        self.assertEqual(d.update(ctx(raw(), 100, 99)), [])                          # first snapshot: baseline
        ev = d.update(ctx(raw(p1="15", pw=(1, 0)), 110, 109))
        self.assertEqual([(e.kind, e.actor) for e in ev], [("POINT_WON", "1")])
        ev = d.update(ctx(raw(g1=1, pw=(4, 0), hold=(1, 0)), 150, 149))
        game = [e for e in ev if e.kind == "GAME_WON"][0]
        self.assertEqual((game.actor, game.detail["game_type"]), ("1", "HOLD"))
        self.assertIn("GAP", [e for e in ev if e.kind == "POINT_WON"][0].flags)      # 3 points in one poll
        ev = d.update(ctx(raw(g1=2, pw=(8, 1), hold=(1, 0), brk=(1, 0)), 200, 199))
        self.assertEqual([e for e in ev if e.kind == "GAME_WON"][0].detail["game_type"], "BREAK")
        ev = d.update(ctx(raw(g1=2, sets=(1, 0), pw=(8, 1), hold=(1, 0), brk=(1, 0), status="2nd_set"), 210, 209))
        self.assertEqual(sorted(e.kind for e in ev), ["SET_WON", "STATUS"])

    def test_no_event_for_unchanged_state(self):
        d = EventDetector()
        d.update(ctx(raw(), 100, 99))
        self.assertEqual(d.update(ctx(raw(), 104, 99)), [])

    def test_quality_flags(self):
        d = EventDetector(lag_flag_s=10)
        d.update(ctx(raw(), 100, 99))
        ev = d.update(ctx(raw(p1="15", pw=(1, 0)), 150, 120))
        self.assertIn("LAGGED", ev[0].flags)
        ev = d.update(ctx(raw(p1="30", pw=(2, 0)), 160, 110))
        self.assertIn("OUT_OF_ORDER", ev[0].flags)
        ev = d.update(ctx(raw(p1="40", pw=(3, 0)), 170, 0))
        self.assertIn("NO_PROVIDER_TS", ev[0].flags)

    def test_raw_payload_kept_for_research_but_not_in_signals(self):
        c = ctx(raw(p1="30", pw=(2, 0)), 100, 99)
        self.assertIn("raw", c.summary())
        self.assertEqual(c.detail["raw"]["competitor1_current_round_score"], "30")


if __name__ == "__main__":
    unittest.main()
