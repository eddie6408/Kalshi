"""Risk engine: every entry check, sizing and caps, daily loss limit."""
import unittest

from lvt.models import FillEvent, Signal
from tests.helpers import Harness, T0, market, mid_to_book, path, snap
from tests.test_strategies import DIP, only


def setup_market(h, pts=((0, 40), (300, 40), (600, 55))):
    tk = list(h.markets)[0]
    for t, mid in path(list(pts)):
        h.step(t, {tk: mid_to_book(mid)})
    return tk


def buy_signal(tk, score=80.0, ask=55.0, stop=50.0):
    return Signal("sig-x", T0, tk, tk.rsplit("-", 1)[0], "DIP", "V1", "yes", "BUY", "CONFIRMED", score=score,
                  bid=ask - 1, ask=ask, mid=ask - 0.5, target=ask + 8, stop=stop, explanation={"risk_cents": 9.0})


class TestRiskChecks(unittest.TestCase):
    def setUp(self):
        self.h = Harness(only("NONE"))
        self.tk = setup_market(self.h)
        self.snap = self.h.last[self.tk]
        self.m = self.h.engine.metrics[self.tk]
        self.info = self.h.markets[self.tk]
        self.now = self.snap.ts

    def check(self, sig=None, snap=None, has_order=False):
        return self.h.engine.risk.check_entry(sig or buy_signal(self.tk), snap or self.snap, self.m, self.info,
                                              self.now, has_order)

    def test_passes_and_sizes(self):
        d = self.check()
        self.assertTrue(d.ok, d.reason)
        self.assertGreaterEqual(d.qty, 1)
        self.assertLessEqual(d.qty, self.h.cfg.risk.max_position_contracts)
        self.assertEqual(d.limit_price, 57.0)                      # ask + max entry slippage, whole cent

    def test_size_scales_with_score(self):
        self.assertGreater(self.check(buy_signal(self.tk, 95)).qty, self.check(buy_signal(self.tk, 50)).qty)

    def test_cost_cap(self):
        h = Harness({**only("NONE"), "risk": {"max_position_cost_usd": 5.0, "risk_per_trade_pct": 0.5}})
        tk = setup_market(h)
        d = h.engine.risk.check_entry(buy_signal(tk), h.last[tk], h.engine.metrics[tk], h.markets[tk], h.last[tk].ts, False)
        self.assertLessEqual(d.qty * d.limit_price / 100, 5.0 + 1e-9)

    def test_depth_participation_cap(self):
        s = snap(self.tk, self.now, 54, 55, bid_size=500, ask_size=8)
        d = self.check(snap=s)
        self.assertFalse(d.ok)                                     # 25% of 8 contracts rounds below 2 ... or zero
        s = snap(self.tk, self.now, 54, 55, bid_size=500, ask_size=40)
        d = self.check(snap=s)
        self.assertLessEqual(d.qty, 10)

    def test_blocks(self):
        r = self.h.engine.risk
        cases = []
        r.kill_switch = True
        cases.append(("kill_switch", self.check()))
        r.kill_switch = False
        r.fail_closed["MARKET_DATA"] = "down"
        cases.append(("fail_closed", self.check()))
        r.fail_closed.clear()
        cases.append(("one_position_per_market", self.check(has_order=True)))
        cases.append(("max_spread", self.check(snap=snap(self.tk, self.now, 50, 56, bid_size=500, ask_size=500))))
        cases.append(("min_liquidity", self.check(snap=snap(self.tk, self.now, 54, 55, bid_size=5, ask_size=5))))
        cases.append(("data_fresh", self.check(snap=snap(self.tk, self.now - 60, 54, 55))))
        for name, d in cases:
            self.assertFalse(d.ok, name)
            self.assertTrue(d.reason.startswith(name), (name, d.reason))

    def test_trade_frequency_and_concurrency(self):
        r = self.h.engine.risk
        for i in range(self.h.cfg.risk.max_trades_per_hour):
            r.note_entry(self.now - 10)
        self.assertTrue(self.check().reason.startswith("max_trade_frequency"))
        r.entry_times = []
        for i in range(self.h.cfg.risk.max_concurrent_positions):
            p = self.h.engine.book.open_pending(buy_signal(f"T{i}-A-B"), market(f"T{i}-A-B"), self.now)
            self.h.engine.book.apply_entry_fill(p, FillEvent("f", self.now, 50, 1, 0.02))
        self.assertTrue(self.check().reason.startswith("max_concurrent_positions"))

    def test_event_exposure_cap(self):
        h = Harness({**only("NONE"), "risk": {"max_event_exposure_usd": 10.0}})
        tk = setup_market(h)
        p = h.engine.book.open_pending(buy_signal(tk.rsplit("-", 1)[0] + "-OTHER"), market(tk.rsplit("-", 1)[0] + "-OTHER"), T0)
        h.engine.book.apply_entry_fill(p, FillEvent("f", T0, 50, 19, 0.3))    # $9.50 in the same event
        d = h.engine.risk.check_entry(buy_signal(tk), h.last[tk], h.engine.metrics[tk], h.markets[tk], h.last[tk].ts, False)
        self.assertFalse(d.ok)
        self.assertIn("max_event_exposure", d.reason)


class TestDailyLoss(unittest.TestCase):
    def test_daily_loss_halts_entries_and_does_not_chase(self):
        h = Harness({**only("DIP"), "risk": {"max_daily_loss_usd": 1.0}})
        tk = list(h.markets)[0]
        # a closed losing position today
        p = h.engine.book.open_pending(buy_signal(tk), h.markets[tk], T0)
        h.engine.book.apply_entry_fill(p, FillEvent("f", T0, 50, 10, 0.18))
        h.engine.book.apply_exit_fill(p, FillEvent("g", T0 + 5, 40, 10, 0.17))
        h.run_path(tk, path(DIP))
        self.assertTrue(h.engine.risk.entries_halted_reason.startswith("daily loss"))
        taken = h.signals(decision="TAKEN")
        self.assertEqual(taken, [])
        self.assertTrue(h.db.query("SELECT * FROM alerts WHERE code='DAILY_LOSS_LIMIT'"))

    def test_strategy_loss_limit(self):
        h = Harness({**only("NONE"), "risk": {"max_strategy_daily_loss_usd": 0.5}})
        tk = setup_market(h)
        p = h.engine.book.open_pending(buy_signal(tk), h.markets[tk], T0)
        h.engine.book.apply_entry_fill(p, FillEvent("f", T0, 50, 10, 0.18))
        h.engine.book.apply_exit_fill(p, FillEvent("g", T0 + 5, 45, 10, 0.17))
        d = h.engine.risk.check_entry(buy_signal("Z-A-B"), h.last[tk], h.engine.metrics[tk], h.markets[tk], h.last[tk].ts, False)
        self.assertTrue(d.reason.startswith("max_strategy_loss"), d.reason)


if __name__ == "__main__":
    unittest.main()
