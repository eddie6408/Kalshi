"""Kill switch: stops new orders at once, cancels working orders, optional
flatten, preserves state."""
import unittest

from tests.helpers import Harness, mid_to_book, path
from tests.test_strategies import DIP, only


class TestKillSwitch(unittest.TestCase):
    def test_blocks_entries(self):
        h = Harness(only("DIP"))
        tk = list(h.markets)[0]
        h.engine.set_kill_switch(True, "test", 0)
        h.run_path(tk, path(DIP))
        self.assertEqual(h.signals(decision="TAKEN"), [])
        rej = h.signals(decision="REJECTED")
        self.assertTrue(rej and rej[0]["reject_reason"].startswith("kill_switch"))
        self.assertTrue(h.db.query("SELECT * FROM alerts WHERE code='KILL_SWITCH'"))

    def test_cancels_working_orders_and_flattens_when_configured(self):
        h = Harness({**only("DIP"), "kill_switch": {"flatten_positions": True},
                     "execution": {"latency_ms": 5000}})
        tk = list(h.markets)[0]
        pts = path(DIP)
        entered_at = None
        for t, mid in pts:
            h.step(t, {tk: mid_to_book(mid)})
            if h.engine.om.working and entered_at is None:
                entered_at = t
                h.engine.set_kill_switch(True, "cancel test", h.clock.now())
                self.assertEqual(h.engine.om.working, {})
                break
        self.assertIsNotNone(entered_at)
        rows = h.db.query("SELECT status FROM orders")
        self.assertEqual(rows[0]["status"], "CANCELLED")
        self.assertEqual(h.db.query("SELECT status FROM positions")[0]["status"], "VOID")

    def test_flatten_exits_open_positions_with_emergency_reason(self):
        h = Harness({**only("DIP"), "kill_switch": {"flatten_positions": True}})
        tk = list(h.markets)[0]
        for t, mid in path(DIP):
            h.step(t, {tk: mid_to_book(mid)})
            if any(p.status == "OPEN" for p in h.engine.book.open_positions()):
                h.engine.set_kill_switch(True, "flatten test", h.clock.now())
                h.step(t + 2, {tk: mid_to_book(mid)})
                h.step(t + 4, {tk: mid_to_book(mid)})
                break
        pos = h.db.query("SELECT * FROM positions")
        self.assertEqual(pos[0]["status"], "CLOSED")
        self.assertEqual(pos[0]["exit_reason"], "EMERGENCY_EXIT")

    def test_off_restores(self):
        h = Harness(only("DIP"))
        h.engine.set_kill_switch(True, "x", 0)
        h.engine.set_kill_switch(False, "", 1)
        self.assertFalse(h.engine.risk.kill_switch)


if __name__ == "__main__":
    unittest.main()
