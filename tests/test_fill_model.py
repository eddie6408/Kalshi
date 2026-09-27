"""Paper fills: full, partial, missed, slippage, delayed (next snapshot
after latency), rejection, fees, determinism. Also fees and V2 encoding."""
import unittest

from lvt.execution import fill_model
from lvt.execution.encoding import EncodingError, encode_order, to_yes_book
from lvt.execution.paper import PaperVenue
from lvt.fees import fee_per_contract_cents, fee_usd
from lvt.models import Depth, OrderRequest
from tests.helpers import T0, cfg, market, snap


def req(coid="lvt-a", action="buy", side="yes", count=10, limit=51.0, ticker="X"):
    return OrderRequest(coid, ticker, "E", action, side, count, limit, "ENTRY" if action == "buy" else "EXIT",
                        50.0, T0)


def book(ts, depth=True):
    # YES bids 49 x100, 48 x100 ; NO bids 50 x40, 49 x100 -> YES asks 50 x40, 51 x100
    d = Depth("X", ts, yes=[(49.0, 100.0), (48.0, 100.0)], no=[(50.0, 40.0), (49.0, 100.0)]) if depth else None
    return snap("X", ts, 49.0, 50.0, d, bid_size=100, ask_size=40)


class TestFillModel(unittest.TestCase):
    def setUp(self):
        self.c = cfg({"execution": {"depth_participation": 1.0}})
        self.x = self.c.execution
        self.m = market("X")

    def test_full_fill_walks_the_book_with_slippage(self):
        up = fill_model.resolve(req(count=60, limit=51), book(T0), self.m, T0, self.x, 6)
        self.assertEqual(up.status, "FILLED")
        self.assertEqual([(f.price, f.count) for f in up.fills], [(50.0, 40), (51.0, 20)])
        self.assertTrue(all(f.fee_usd > 0 for f in up.fills))

    def test_partial_fill_ioc_remainder_cancelled(self):
        up = fill_model.resolve(req(count=60, limit=50), book(T0), self.m, T0, self.x, 6)
        self.assertEqual(up.status, "PARTIAL")
        self.assertEqual(sum(f.count for f in up.fills), 40)

    def test_no_fill_when_price_moved(self):
        up = fill_model.resolve(req(limit=48), book(T0), self.m, T0, self.x, 6)
        self.assertEqual(up.status, "NO_FILL")
        self.assertIn("price moved", up.reason)

    def test_participation_haircut(self):
        c = cfg({"execution": {"depth_participation": 0.5}})
        up = fill_model.resolve(req(count=60, limit=51), book(T0), self.m, T0, c.execution, 6)
        # half of 40 at 50c, then the rest from the 50 available at 51c
        self.assertEqual([(f.price, f.count) for f in up.fills], [(50.0, 20), (51.0, 40)])
        self.assertEqual(up.status, "FILLED")

    def test_sell_no_hits_no_bids(self):
        up = fill_model.resolve(req(action="sell", side="no", count=10, limit=49), book(T0), self.m, T0, self.x, 6)
        self.assertEqual(up.status, "FILLED")
        self.assertEqual(up.fills[0].price, 50.0)

    def test_top_of_book_only_when_depth_stale(self):
        s = book(T0)
        s.depth.ts = T0 - 60
        up = fill_model.resolve(req(count=60, limit=55), s, self.m, T0, self.x, 6)
        self.assertEqual(up.status, "PARTIAL")                     # only the 40 at the touch
        self.assertEqual(sum(f.count for f in up.fills), 40)

    def test_rejects_inactive_market_and_bad_price(self):
        s = book(T0)
        s.quote.status = "closed"
        self.assertEqual(fill_model.resolve(req(), s, self.m, T0, self.x, 6).status, "REJECTED")
        self.assertEqual(fill_model.resolve(req(limit=0), book(T0), self.m, T0, self.x, 6).status, "REJECTED")

    def test_touch_miss_is_deterministic(self):
        c = cfg({"execution": {"touch_miss_probability": 0.5}})
        outs = [fill_model.resolve(req(coid=f"id{i}"), book(T0), self.m, T0, c.execution, 6).status for i in range(40)]
        again = [fill_model.resolve(req(coid=f"id{i}"), book(T0), self.m, T0, c.execution, 6).status for i in range(40)]
        self.assertEqual(outs, again)
        self.assertIn("NO_FILL", outs)
        self.assertIn("FILLED", outs)


class TestPaperVenueTiming(unittest.TestCase):
    def test_order_fills_on_first_snapshot_after_latency_not_the_decision_snapshot(self):
        c = cfg({"execution": {"latency_ms": 500, "depth_participation": 1.0}})
        v = PaperVenue(c, "PAPER", {"X": market("X")})
        self.assertEqual(v.submit(req(limit=51), T0).status, "ACKED")
        # a snapshot stamped before arrival must not fill it
        self.assertEqual(v.on_market_data({"X": book(T0 + 0.2)}, T0 + 0.2), [])
        # price moved up before arrival -> missed trade
        moved = snap("X", T0 + 2, 52.0, 53.0, bid_size=100, ask_size=100)
        ups = v.on_market_data({"X": moved}, T0 + 2)
        self.assertEqual(ups[0].status, "NO_FILL")

    def test_delayed_fill_and_ttl_expiry(self):
        c = cfg({"execution": {"latency_ms": 500}})
        v = PaperVenue(c, "PAPER", {"X": market("X")})
        v.submit(req(coid="a", limit=51), T0)
        ups = v.on_market_data({}, T0 + c.execution.order_ttl_s + 1)
        self.assertEqual(ups[0].status, "NO_FILL")
        self.assertIn("no market data", ups[0].reason)

    def test_duplicate_client_order_id_rejected(self):
        v = PaperVenue(cfg(), "PAPER", {"X": market("X")})
        v.submit(req(coid="dup"), T0)
        self.assertEqual(v.submit(req(coid="dup"), T0).status, "REJECTED")

    def test_shadow_builds_live_payload(self):
        v = PaperVenue(cfg(), "SHADOW", {"X": market("X")}, shadow=True)
        v.submit(req(coid="s1", side="no", limit=57), T0)
        p = v.payloads["s1"]
        self.assertEqual((p["side"], p["price"], p["time_in_force"]), ("ask", "0.4300", "immediate_or_cancel"))


class TestFees(unittest.TestCase):
    def test_kalshi_taker_formula_rounds_up(self):
        self.assertEqual(fee_usd(100, 50), 1.75)                  # 0.07*100*.5*.5
        self.assertEqual(fee_usd(1, 50), 0.02)                    # 0.0175 -> 2c
        self.assertEqual(fee_usd(10, 10), 0.07)                   # 0.063 -> 7c
        self.assertEqual(fee_usd(0, 50), 0.0)

    def test_multiplier_and_maker(self):
        self.assertEqual(fee_usd(100, 50, "quadratic", 0.5), 0.88)
        self.assertEqual(fee_usd(100, 50, "quadratic", 1.0, maker=True), 0.0)
        self.assertEqual(fee_usd(100, 50, "quadratic_with_maker_fees", 1.0, maker=True), 0.44)

    def test_per_contract_estimate(self):
        self.assertAlmostEqual(fee_per_contract_cents(50, count=100), 1.75)


class TestEncoding(unittest.TestCase):
    def test_single_yes_book_translation(self):
        self.assertEqual(to_yes_book("buy", "yes", 40), ("bid", 40))
        self.assertEqual(to_yes_book("sell", "yes", 40), ("ask", 40))
        self.assertEqual(to_yes_book("buy", "no", 57), ("ask", 43))
        self.assertEqual(to_yes_book("sell", "no", 57), ("bid", 43))

    def test_validation(self):
        with self.assertRaises(EncodingError):
            to_yes_book("buy", "yes", 0)
        with self.assertRaises(EncodingError):
            to_yes_book("buy", "yes", 45.5)
        with self.assertRaises(EncodingError):
            encode_order(req(coid=""))

    def test_exit_is_reduce_only(self):
        self.assertTrue(encode_order(req(action="sell", limit=40))["reduce_only"])
        self.assertNotIn("reduce_only", encode_order(req()))


if __name__ == "__main__":
    unittest.main()
