"""Parsing of real Kalshi response shapes (recorded 2026-09-27), the rate
limiter, provider fallback and sports context normalization."""
import unittest

from lvt.data.context import normalize_live_data, normalize_status
from lvt.data.http import TokenBucket
from lvt.data.kalshi_public import parse_depth, parse_market, parse_quote
from lvt.data.provider import FallbackMarketData, MarketDataProvider
from lvt.models import YES, NO, Snapshot, classify_sport
from lvt.alerts import Alerter

MARKET = {"ticker": "KXATPMATCH-26SEP27HARHUR-HUR", "event_ticker": "KXATPMATCH-26SEP27HARHUR",
          "yes_bid_dollars": "0.5900", "yes_ask_dollars": "0.6000", "yes_bid_size_fp": "8365.96",
          "yes_ask_size_fp": "10722.05", "last_price_dollars": "0.6400", "volume_fp": "306816.01",
          "volume_24h_fp": "207659.54", "open_interest_fp": "1000.00", "status": "active",
          "close_time": "2026-10-11T08:30:00Z", "occurrence_datetime": "2026-09-27T11:30:00Z",
          "yes_sub_title": "Hubert Hurkacz", "title": "Hubert Hurkacz wins", "can_close_early": True}
BOOK = {"orderbook_fp": {"no_dollars": [["0.3200", "10722.05"], ["0.3300", "31679.21"], ["0.4000", "5"]],
                         "yes_dollars": [["0.5900", "8365.96"], ["0.6000", "18069.00"]]}}


class TestParsing(unittest.TestCase):
    def test_quote(self):
        q = parse_quote(MARKET, 1.0)
        self.assertEqual((q.yes_bid, q.yes_ask, q.last), (59.0, 60.0, 64.0))
        self.assertAlmostEqual(q.volume, 306816.01)

    def test_empty_sides(self):
        q = parse_quote({**MARKET, "yes_bid_dollars": "0.0000", "yes_ask_dollars": "1.0000"}, 1.0)
        self.assertIsNone(q.yes_bid)
        self.assertIsNone(q.yes_ask)

    def test_market(self):
        m = parse_market(MARKET)
        self.assertEqual(m.series_ticker, "KXATPMATCH")
        self.assertGreater(m.close_ts, m.occurrence_ts)

    def test_depth_best_first_and_side_translation(self):
        d = parse_depth("X", BOOK, 1.0)
        self.assertEqual(d.yes[0], (60.0, 18069.0))               # best YES bid first
        self.assertEqual(d.no[0], (40.0, 5.0))
        q = parse_quote({**MARKET, "yes_bid_dollars": "0.6000", "yes_ask_dollars": "0.6000"}, 1.0)
        s = Snapshot("X", 1.0, q, d)
        self.assertEqual(s.ask_levels(YES, 10)[0], (60.0, 5.0))    # YES ask = 100 - best NO bid
        self.assertEqual(s.ask_levels(NO, 10)[0], (40.0, 18069.0))
        self.assertEqual(s.bid_levels(NO, 10)[0], (40.0, 5.0))
        self.assertEqual((s.bid(NO), s.ask(NO)), (40.0, 40.0))

    def test_sport_classification(self):
        self.assertEqual(classify_sport("KXATPCHALLENGERMATCH", "Sports"), "TENNIS")
        self.assertEqual(classify_sport("KXMLBGAME", "Sports"), "BASEBALL")
        self.assertEqual(classify_sport("KXNFLGAME", "Sports"), "FOOTBALL")
        self.assertEqual(classify_sport("KXFOO", "Sports"), "OTHER_SPORT")
        self.assertEqual(classify_sport("KXBTCD", "Crypto"), "NON_SPORT")


class TestContext(unittest.TestCase):
    def test_status_words(self):
        self.assertEqual(normalize_status("1st_set"), "live")
        self.assertEqual(normalize_status("not_started"), "scheduled")
        self.assertEqual(normalize_status("postponed"), "scheduled")
        self.assertEqual(normalize_status("finished"), "final")
        self.assertEqual(normalize_status("inprogress"), "live")

    def test_tennis_and_baseball(self):
        t = normalize_live_data("E", "tennis_tournament_singles", {
            "match_status": "1st_set", "competitor1_round_scores": [{"score": 5}],
            "competitor2_round_scores": [{"score": 6}], "competitor1_current_round_score": 40,
            "competitor2_current_round_score": 30}, 1.0)
        self.assertEqual((t.sport, t.status, t.score), ("TENNIS", "live", "5-6"))
        self.assertEqual(t.detail["game_points"], "40-30")
        b = normalize_live_data("E", "baseball_game", {"away_points": 7, "home_points": 10, "inning": 9,
                                                       "inning_half": 1, "outs": 3, "status": "closed",
                                                       "widget_status": "finished"}, 1.0)
        self.assertEqual((b.sport, b.status, b.score, b.period), ("BASEBALL", "final", "7-10", "top 9"))


class TestInfra(unittest.TestCase):
    def test_token_bucket(self):
        t = [0.0]
        slept = []
        b = TokenBucket(2.0, 2.0, clock=lambda: t[0], sleep=lambda s: (slept.append(s), t.__setitem__(0, t[0] + s)))
        for _ in range(4):
            b.acquire()
        self.assertAlmostEqual(sum(slept), 1.0, places=5)          # 2 burst + 2 at 2/s

    def test_fallback_provider(self):
        class Bad(MarketDataProvider):
            name = "bad"
            def list_open_markets(self, pause_s=0.0, series_ticker=None): raise IOError("x")
            def series_catalog(self): raise IOError("x")
            def quotes(self, t): raise IOError("x")
            def depth(self, t, n): raise IOError("x")

        class Good(Bad):
            name = "good"
            def quotes(self, t): return {"ok": 1}
        f = FallbackMarketData([Bad(), Good()])
        self.assertEqual(f.quotes(["a"]), {"ok": 1})
        self.assertEqual(f.active, "good")

    def test_alert_dedupe(self):
        now = [0.0]
        a = Alerter(None, "r", 900, clock=lambda: now[0])
        self.assertTrue(a.fire("STALE_DATA", "x"))
        self.assertFalse(a.fire("STALE_DATA", "x"))
        now[0] = 1000
        self.assertTrue(a.fire("STALE_DATA", "x"))


if __name__ == "__main__":
    unittest.main()
