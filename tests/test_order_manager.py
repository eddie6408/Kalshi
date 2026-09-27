"""Duplicate-order protection and unknown order state (fail closed)."""
import tempfile
import unittest
from pathlib import Path

from lvt.db import open_state_db
from lvt.execution.order_manager import OrderManager
from lvt.execution.paper import PaperVenue
from lvt.models import OrderRequest, OrderUpdate
from tests.helpers import T0, cfg, market, snap


def req(coid, ticker="X"):
    return OrderRequest(coid, ticker, "E", "buy", "yes", 5, 51.0, "ENTRY", 50.0, T0, position_id="p")


class ExplodingVenue(PaperVenue):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.explode = True
        self.seen = {}

    def submit(self, r, now):
        if self.explode:
            self.seen[r.client_order_id] = r
            raise TimeoutError("network timeout after send")
        return super().submit(r, now)

    def lookup(self, coid):
        return OrderUpdate(coid, "NO_FILL", T0) if coid in self.seen else None


class TestOrderManager(unittest.TestCase):
    def setUp(self):
        self.db = open_state_db(Path(tempfile.mkdtemp()) / "s.db")
        self.updates = []
        self.venue = PaperVenue(cfg({"execution": {"depth_participation": 1.0}}), "PAPER", {"X": market("X")})
        self.om = OrderManager(self.db, self.venue, "PAPER", "r", lambda r, u: self.updates.append(u))

    def test_one_working_order_per_market(self):
        self.assertEqual(self.om.submit(req("a"), T0).status, "ACKED")
        up = self.om.submit(req("b"), T0)
        self.assertEqual(up.status, "REJECTED")
        self.assertIn("DUPLICATE_ORDER_RISK", up.reason)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM orders"), 1)

    def test_client_order_id_never_reused(self):
        self.om.submit(req("a"), T0)
        self.om.process({"X": snap("X", T0 + 2, 49, 50)}, T0 + 2)
        up = self.om.submit(req("a"), T0 + 3)
        self.assertEqual(up.status, "REJECTED")
        self.assertIn("reused", up.reason)

    def test_intent_persisted_before_submit(self):
        seen = {}
        orig = self.venue.submit

        def spy(r, now):
            seen["status"] = self.db.scalar("SELECT status FROM orders WHERE client_order_id=?", (r.client_order_id,))
            return orig(r, now)
        self.venue.submit = spy
        self.om.submit(req("a"), T0)
        self.assertEqual(seen["status"], "PENDING_SUBMIT")

    def test_fill_recorded_with_slippage(self):
        self.om.submit(req("a"), T0)
        self.om.process({"X": snap("X", T0 + 2, 50, 51)}, T0 + 2)
        o = self.db.one("SELECT * FROM orders")
        self.assertEqual(o["status"], "FILLED")
        self.assertEqual(o["avg_fill_price"], 51.0)
        self.assertEqual(o["slippage_cents"], 1.0)
        self.assertEqual(self.db.scalar("SELECT SUM(count) FROM fills"), 5)

    def test_exception_means_unknown_and_blocks_market(self):
        v = ExplodingVenue(cfg(), "PAPER", {"X": market("X")})
        om = OrderManager(self.db, v, "PAPER", "r", lambda r, u: self.updates.append(u))
        up = om.submit(req("u1"), T0)
        self.assertEqual(up.status, "UNKNOWN")
        self.assertTrue(om.has_working("X"))                     # no second order while unknown
        self.assertEqual(om.submit(req("u2"), T0).status, "REJECTED")
        still = om.resolve_unknown(T0 + 1)
        self.assertEqual(still, [])
        self.assertFalse(om.has_working("X"))
        self.assertEqual(self.db.scalar("SELECT status FROM orders WHERE client_order_id='u1'"), "NO_FILL")

    def test_engine_fails_closed_on_unknown(self):
        from tests.helpers import Harness
        h = Harness()
        h.engine.om.unknown["zz"] = req("zz")
        self.db2 = h.db
        h.db.insert("orders", {"client_order_id": "zz", "env": "PAPER", "ticker": "X", "status": "UNKNOWN",
                               "action": "buy", "side": "yes", "count": 5, "limit_price": 51, "purpose": "ENTRY"})

        class Stuck:
            def lookup(self, c):
                raise TimeoutError("still down")
        real = h.engine.om.venue
        h.engine.om.venue = type("V", (), {"lookup": Stuck().lookup, "on_market_data": real.on_market_data})()
        h.step(0, {})
        self.assertIn("UNKNOWN_ORDER_STATE", h.engine.risk.fail_closed)


if __name__ == "__main__":
    unittest.main()
