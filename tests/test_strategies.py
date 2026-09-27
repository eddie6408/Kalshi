"""Dip / momentum / reversal / downtrend detection, anti-chase and
anti-falling-knife behaviour, signal explanations."""
import json
import unittest

from tests.helpers import Harness, T0, path

DIP = [(0, 65), (300, 65), (480, 45), (540, 45.4), (570, 48.5), (900, 58)]
SPIKE = [(0, 35), (300, 35), (480, 55), (540, 54.6), (570, 51.5), (900, 42)]
KNIFE = [(0, 65), (300, 65), (700, 30)]
RISE = [(0, 40), (180, 40), (360, 50.5), (600, 56)]
FALL = [(0, 60), (180, 60), (360, 49.5), (600, 44)]
CHASE = [(0, 40), (200, 40), (250, 62), (400, 62)]


def only(name, **extra):
    o = {"strategies": {k: {"enabled": k == name} for k in ("DIP", "REVERSAL", "MOMENTUM", "DOWNTREND")}}
    o.update(extra)
    return o


def run(name, pts):
    h = Harness(only(name))
    h.run_path(list(h.markets)[0], path(pts))
    return h


class TestDip(unittest.TestCase):
    def test_dip_bought_after_stabilisation_and_reversal(self):
        h = run("DIP", DIP)
        buys = h.signals(action="BUY")
        self.assertEqual(len(buys), 1)
        b = buys[0]
        self.assertEqual(b["side"], "yes")
        self.assertGreaterEqual(b["ts"] - T0, 540 + 20)            # waited for stabilisation
        self.assertLess(b["stop"], 45)                              # stop below the low
        self.assertGreater(b["expected_net_cents"], 0)
        exp = json.loads(b["explanation"])
        self.assertIn("recent_high", exp)
        self.assertIn("REASON:", b["text"])

    def test_falling_knife_is_not_bought(self):
        h = run("DIP", KNIFE)
        self.assertEqual(h.signals(action="BUY"), [])
        watch = h.signals(action="WATCH")
        self.assertTrue(watch)
        self.assertIn("falling knife", watch[0]["text"])

    def test_dip_position_exits_at_target_with_net_profit(self):
        h = run("DIP", DIP)
        pos = h.db.query("SELECT * FROM positions")
        self.assertEqual(len(pos), 1)
        p = pos[0]
        self.assertEqual(p["status"], "CLOSED")
        self.assertEqual(p["exit_reason"], "FIXED_PROFIT_TARGET")
        fees = p["entry_fees_usd"] + p["exit_fees_usd"]
        self.assertAlmostEqual(p["realized_pnl_usd"], p["gross_pnl_usd"] - fees, places=3)
        self.assertAlmostEqual(p["gross_mid_pnl_usd"] - p["spread_cost_usd"] - p["slippage_cost_usd"],
                               p["gross_pnl_usd"], places=3)


class TestReversal(unittest.TestCase):
    def test_spike_then_reversal_buys_no(self):
        h = run("REVERSAL", SPIKE)
        buys = h.signals(action="BUY")
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["side"], "no")                     # downside = buying NO, never a short

    def test_mirror_symmetry_with_dip(self):
        a, b = run("DIP", DIP), run("REVERSAL", SPIKE)
        pa, pb = a.db.query("SELECT * FROM positions")[0], b.db.query("SELECT * FROM positions")[0]
        # same trade on the mirrored book; the 1c grid rounds fees a cent apart at most
        self.assertEqual(pa["entry_qty"], pb["entry_qty"])
        self.assertAlmostEqual(pa["gross_pnl_usd"], pb["gross_pnl_usd"], places=4)
        self.assertAlmostEqual(pa["realized_pnl_usd"], pb["realized_pnl_usd"], delta=0.02)


class TestMomentum(unittest.TestCase):
    def test_persistent_rise_is_bought(self):
        h = run("MOMENTUM", RISE)
        buys = h.signals(action="BUY", decision="TAKEN")
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["side"], "yes")

    def test_single_tick_is_not_momentum(self):
        h = run("MOMENTUM", [(0, 40), (300, 40), (302, 46), (400, 46)])
        self.assertEqual(h.signals(action="BUY"), [])

    def test_does_not_chase_an_exhausted_move(self):
        h = run("MOMENTUM", CHASE)
        self.assertEqual(h.signals(action="BUY"), [])

    def test_downtrend_buys_no(self):
        h = run("DOWNTREND", FALL)
        buys = h.signals(action="BUY", decision="TAKEN")
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["side"], "no")

    def test_momentum_exit_when_move_collapses(self):
        h = run("MOMENTUM", [(0, 40), (180, 40), (300, 47), (330, 42), (400, 40)])
        pos = h.db.query("SELECT * FROM positions")
        self.assertEqual(len(pos), 1)
        self.assertIn(pos[0]["exit_reason"], ("MAX_LOSS_EXIT", "REVERSAL_EXIT", "MOMENTUM_EXIT"))

    def test_no_entries_during_warm_up(self):
        h = Harness(only("MOMENTUM", signals={"min_history_s": 600}))
        h.run_path(list(h.markets)[0], path(RISE))
        self.assertEqual([r for r in h.signals() if r["ts"] < T0 + 600], [])   # the rise was at 180-360s

    def test_quiet_market_generates_nothing(self):
        h = run("MOMENTUM", [(0, 50), (900, 50)])
        self.assertEqual(h.signals(), [])


class TestCostGate(unittest.TestCase):
    def test_small_dip_rejected_because_fees_eat_it(self):
        h = run("DIP", [(0, 60), (300, 60), (480, 48), (530, 48.4), (560, 51), (700, 52)])
        self.assertEqual(h.signals(action="BUY"), [])
        self.assertTrue(any("edge too thin" in r["text"] or "reward:risk" in r["text"]
                            for r in h.signals(action="WATCH")))


if __name__ == "__main__":
    unittest.main()
