"""Reconciliation between the engine's book and the venue's record."""
import unittest

from lvt.execution.reconcile import reconcile
from lvt.models import FillEvent
from tests.helpers import Harness, mid_to_book, path
from tests.test_risk import buy_signal
from tests.test_strategies import DIP, only


class FakeVenue:
    def __init__(self, pos=None, fail=False):
        self.pos, self.fail = pos or {}, fail

    def positions(self):
        if self.fail:
            raise ConnectionError("exchange down")
        return self.pos


class TestReconcile(unittest.TestCase):
    def setUp(self):
        self.h = Harness(only("NONE"))
        self.book = self.h.engine.book
        p = self.book.open_pending(buy_signal("A-B-C"), self.h.markets[list(self.h.markets)[0]], 0)
        self.book.apply_entry_fill(p, FillEvent("f", 0, 50, 10, 0.18))

    def test_agree(self):
        r = reconcile(self.book, FakeVenue({"A-B-C": ("yes", 10)}), [])
        self.assertTrue(r.ok)

    def test_quantity_mismatch(self):
        r = reconcile(self.book, FakeVenue({"A-B-C": ("yes", 7)}), [])
        self.assertFalse(r.ok)
        self.assertEqual(r.mismatches[0]["kind"], "QUANTITY_MISMATCH")

    def test_unexpected_and_missing(self):
        r = reconcile(self.book, FakeVenue({"Z": ("no", 3)}), [])
        kinds = sorted(m["kind"] for m in r.mismatches)
        self.assertEqual(kinds, ["MISSING_AT_VENUE", "UNEXPECTED_POSITION"])

    def test_unresolved_orders_or_venue_error_fail(self):
        self.assertFalse(reconcile(self.book, FakeVenue({"A-B-C": ("yes", 10)}), ["coid"]).ok)
        r = reconcile(self.book, FakeVenue(fail=True), [])
        self.assertFalse(r.ok)
        self.assertIn("unavailable", r.summary())

    def test_paper_venue_ledger_agrees_after_real_trading(self):
        h = Harness(only("DIP"))
        tk = list(h.markets)[0]
        for t, mid in path(DIP):
            h.step(t, {tk: mid_to_book(mid)})
            if not h.engine.om.working:
                r = reconcile(h.engine.book, h.venue, [])
                self.assertTrue(r.ok, (t, r.mismatches))


class TestSettlement(unittest.TestCase):
    def test_settled_position_keeps_book_and_venue_in_sync_across_restart(self):
        h = Harness(only("DIP"))
        tk = list(h.markets)[0]
        opened = None
        for t, mid in path(DIP):
            h.step(t, {tk: mid_to_book(mid)})
            if any(p.status == "OPEN" for p in h.engine.book.open_positions()):
                opened = t
                break
        self.assertIsNotNone(opened)
        b, a = mid_to_book(50)
        h.step(opened + 2, {tk: (b, a)}, status="finalized", result="no")
        self.assertEqual(h.engine.book.open_positions(), [])
        self.assertEqual(h.venue.positions(), {})
        self.assertTrue(reconcile(h.engine.book, h.venue, []).ok)
        self.assertEqual(h.db.one("SELECT exit_reason FROM positions")["exit_reason"], "SETTLED_NO")
        self.assertEqual(h.db.scalar("SELECT COUNT(*) FROM fills WHERE action='settle'"), 1)
        h2 = Harness(only("DIP"), db_path=h.db_path)            # restart: ledger rebuilt from fills
        h2.engine.restore(h2.clock.now())
        self.assertTrue(reconcile(h2.engine.book, h2.venue, []).ok)


if __name__ == "__main__":
    unittest.main()
