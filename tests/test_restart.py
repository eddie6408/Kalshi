"""Restart recovery: open positions are restored, in-flight orders become
UNKNOWN until resolved, the paper venue ledger is rebuilt from fills, and
reconciliation agrees before trading resumes."""
import unittest

from lvt.execution.reconcile import reconcile
from tests.helpers import Harness, mid_to_book, path
from tests.test_strategies import DIP, only


class TestRestart(unittest.TestCase):
    def _run_until(self, h, tk, cond):
        for t, mid in path(DIP):
            h.step(t, {tk: mid_to_book(mid)})
            if cond(h):
                return t
        self.fail("condition never met")

    def test_open_position_survives_restart(self):
        h = Harness(only("DIP"))
        tk = list(h.markets)[0]
        t = self._run_until(h, tk, lambda h: any(p.status == "OPEN" for p in h.engine.book.open_positions()))
        before = h.engine.book.open_positions()[0]
        # "crash": build a new harness on the same database
        h2 = Harness(only("DIP"), db_path=h.db_path)
        h2.engine.restore(h2.clock.now())
        restored = h2.engine.book.open_positions()
        self.assertEqual(len(restored), 1)
        self.assertEqual((restored[0].id, restored[0].qty, restored[0].avg_entry),
                         (before.id, before.qty, before.avg_entry))
        self.assertTrue(reconcile(h2.engine.book, h2.venue, []).ok)
        # and it keeps being managed to an exit
        for tt, mid in path(DIP):
            if tt > t:
                h2.step(tt, {tk: mid_to_book(mid)})
        self.assertEqual(h2.db.one("SELECT status FROM positions")["status"], "CLOSED")

    def test_in_flight_entry_is_resolved_not_duplicated(self):
        h = Harness({**only("DIP"), "execution": {"latency_ms": 5000}})
        tk = list(h.markets)[0]
        self._run_until(h, tk, lambda h: bool(h.engine.om.working))
        h2 = Harness(only("DIP"), db_path=h.db_path)
        h2.engine.restore(h2.clock.now())
        self.assertEqual(len(h2.engine.om.unknown), 1)
        still = h2.engine.om.resolve_unknown(h2.clock.now())
        self.assertEqual(still, [])
        self.assertEqual(h2.db.one("SELECT status FROM orders")["status"], "REJECTED")
        self.assertEqual(h2.db.one("SELECT status FROM positions")["status"], "VOID")
        self.assertEqual(h2.db.scalar("SELECT COUNT(*) FROM orders"), 1)


if __name__ == "__main__":
    unittest.main()
