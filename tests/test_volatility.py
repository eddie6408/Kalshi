"""Volatility engine, velocity, acceleration, market states, scanner
eligibility, spread and liquidity filters, scoring."""
import unittest

from lvt.engine import scoring
from lvt.engine.scanner import Scanner, eligibility
from lvt.engine.states import classify
from lvt.engine.volatility import MarketSeries, compute_metrics, linreg, resample
from tests.helpers import T0, cfg, market, mid_to_book, path, snap


def series_from(pts, spread=1.0, size=500.0, vol_step=20.0):
    c = cfg()
    s = MarketSeries("X", c.volatility.history_s)
    v = 0.0
    for t, mid in path(pts):
        b, a = mid_to_book(mid)
        if spread != 1.0:
            a = b + spread
        v += vol_step
        s.add(snap("X", T0 + t, b, a, volume=v, bid_size=size, ask_size=size))
    return c, s


class TestMath(unittest.TestCase):
    def test_linreg_slope_per_minute(self):
        ts = [0, 60, 120, 180]
        slope, r2 = linreg(ts, [10, 12, 14, 16])
        self.assertAlmostEqual(slope, 2.0)
        self.assertAlmostEqual(r2, 1.0)

    def test_resample_never_looks_forward(self):
        out = resample([0, 90], [1, 5], 60, 120, 0)
        self.assertEqual(out, [1, 1, 5])


class TestMetrics(unittest.TestCase):
    def test_velocity_sign_and_acceleration(self):
        c, s = series_from([(0, 40), (300, 40), (600, 55)])
        m = compute_metrics(s, T0 + 600, c.volatility)
        self.assertGreater(m.velocity_short, 1.0)
        self.assertGreater(m.velocity_medium, 1.0)
        self.assertGreater(m.change_long, 10)
        c, s = series_from([(0, 40), (300, 40), (450, 55), (600, 56)])
        m2 = compute_metrics(s, T0 + 600, c.volatility)
        self.assertLess(m2.acceleration, 0)                      # decelerating

    def test_recent_high_low_range_and_volume(self):
        c, s = series_from([(0, 50), (200, 60), (400, 45), (600, 50)])
        m = compute_metrics(s, T0 + 600, c.volatility)
        self.assertEqual((m.recent_high, m.recent_low), (59.5, 44.5))
        self.assertAlmostEqual(m.range_cents, 15)
        self.assertGreater(m.volume_long, 0)
        self.assertGreater(m.realized_vol, 0)

    def test_data_age(self):
        c, s = series_from([(0, 50), (60, 50)])
        m = compute_metrics(s, T0 + 80, c.volatility)
        self.assertAlmostEqual(m.data_age_s, 20)


class TestStatesAndEligibility(unittest.TestCase):
    def test_quiet_market_is_low_vol_no_trade(self):
        c, s = series_from([(0, 50), (900, 50)])
        m = compute_metrics(s, T0 + 900, c.volatility)
        st = classify(m, c.volatility, c.eligibility)
        self.assertIn("LOW_VOLATILITY", st.labels)
        self.assertFalse(st.tradeable)
        self.assertTrue(all(l in st.explanation for l in st.labels))
        self.assertFalse(eligibility(m, market(), T0 + 900, c.eligibility, 10).ok)

    def test_wide_spread_blocks(self):
        c, s = series_from([(0, 40), (600, 55)], spread=6.0)
        m = compute_metrics(s, T0 + 600, c.volatility)
        st = classify(m, c.volatility, c.eligibility)
        self.assertEqual(st.primary, "WIDE_SPREAD")
        el = eligibility(m, market(), T0 + 600, c.eligibility, 10)
        self.assertIn("spread", el.reasons)

    def test_low_liquidity_blocks(self):
        c, s = series_from([(0, 40), (600, 55)], size=10.0)
        m = compute_metrics(s, T0 + 600, c.volatility)
        self.assertIn("LOW_LIQUIDITY", classify(m, c.volatility, c.eligibility).labels)
        self.assertIn("liquidity", eligibility(m, market(), T0 + 600, c.eligibility, 10).reasons)

    def test_stale_data_blocks(self):
        c, s = series_from([(0, 40), (600, 55)])
        m = compute_metrics(s, T0 + 640, c.volatility)
        self.assertEqual(classify(m, c.volatility, c.eligibility).primary, "STALE_DATA")

    def test_active_market_eligible_and_trending(self):
        c, s = series_from([(0, 40), (300, 40), (600, 55)])
        m = compute_metrics(s, T0 + 600, c.volatility)
        st = classify(m, c.volatility, c.eligibility)
        self.assertIn("STRONG_UPTREND", st.labels)
        self.assertTrue(eligibility(m, market(), T0 + 600, c.eligibility, 10).ok)

    def test_closing_market_not_eligible(self):
        c, s = series_from([(0, 40), (300, 40), (600, 55)])
        m = compute_metrics(s, T0 + 600, c.volatility)
        el = eligibility(m, market(close_in=900), T0 + 600, c.eligibility, 10)
        self.assertIn("time_remaining", el.reasons)


class TestScoring(unittest.TestCase):
    def test_score_range_and_monotonic(self):
        lo = scoring.score({k: 0.1 for k in scoring.WEIGHTS})
        hi = scoring.score({k: 0.9 for k in scoring.WEIGHTS})
        self.assertTrue(0 <= lo < hi <= 100)
        self.assertEqual(scoring.score({k: 5.0 for k in scoring.WEIGHTS}), 100.0)   # clamped


class TestScanner(unittest.TestCase):
    def test_universe_filters_and_hot_selection(self):
        c = cfg()
        sc = Scanner(c)
        sc.catalog = {"KXATPMATCH": {"category": "Sports", "fee_type": "quadratic", "fee_multiplier": 1.0}}
        m1, m2, m3 = market("KXATPMATCH-A-A"), market("KXATPMATCH-B-B"), market("KXATPMATCH-C-C")
        m2.volume_24h = 1                                        # below the universe floor
        m3.close_ts = T0 + 60                                    # closes too soon
        sc.load_universe([m1, m2, m3], T0, set())
        self.assertEqual(list(sc.markets), ["KXATPMATCH-A-A"])
        self.assertEqual(sc.markets["KXATPMATCH-A-A"].sport, "TENNIS")
        from tests.helpers import quote
        sc.note_probe({"KXATPMATCH-A-A": quote("KXATPMATCH-A-A", T0, 40, 41, volume=0)})
        sc.note_probe({"KXATPMATCH-A-A": quote("KXATPMATCH-A-A", T0 + 30, 44, 45, volume=500)})
        hot = sc.select_hot(T0 + 30, pinned={"PINNED"})
        self.assertIn("PINNED", hot)
        self.assertIn("KXATPMATCH-A-A", hot)

    def test_universe_updated_in_place(self):
        sc = Scanner(cfg())
        ref = sc.markets
        sc.load_universe([market()], T0, set())
        self.assertIs(sc.markets, ref)


if __name__ == "__main__":
    unittest.main()
