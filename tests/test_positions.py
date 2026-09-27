"""Position accounting: partial entries/exits, P&L decomposition, marking at
the bid, settlement, void entries."""
import tempfile
import unittest
from pathlib import Path

from lvt.db import open_state_db
from lvt.engine.positions import PositionBook
from lvt.fees import fee_usd
from lvt.models import FillEvent, Signal
from tests.helpers import T0, market


def sig(side="yes"):
    return Signal("sig-1", T0, "X", "E", "DIP", "V1", side, "BUY", "CONFIRMED", score=80, bid=49, ask=50, mid=49.5,
                  target=56, stop=45)


class TestPositions(unittest.TestCase):
    def setUp(self):
        self.db = open_state_db(Path(tempfile.mkdtemp()) / "s.db")
        self.book = PositionBook(self.db, "PAPER", "r")

    def test_partial_entry_then_partial_exits(self):
        p = self.book.open_pending(sig(), market("X"), T0)
        self.book.apply_entry_fill(p, FillEvent("a", T0, 50, 30, fee_usd(30, 50)))
        self.book.apply_entry_fill(p, FillEvent("a", T0, 51, 10, fee_usd(10, 51)))
        self.assertEqual(p.qty, 40)
        self.assertAlmostEqual(p.avg_entry, 50.25)
        p.exit_mid, p.exit_ref_bid = 55.5, 55
        self.book.apply_exit_fill(p, FillEvent("b", T0 + 60, 55, 25, fee_usd(25, 55)))
        self.assertEqual((p.status, p.qty), ("OPEN", 15))
        self.book.apply_exit_fill(p, FillEvent("b", T0 + 61, 54, 15, fee_usd(15, 54)))
        self.assertEqual(p.status, "CLOSED")
        gross = (55 * 25 + 54 * 15 - 50 * 30 - 51 * 10) / 100
        fees = fee_usd(30, 50) + fee_usd(10, 51) + fee_usd(25, 55) + fee_usd(15, 54)
        self.assertAlmostEqual(p.gross_pnl_usd, gross, places=4)
        self.assertAlmostEqual(p.realized_pnl_usd, gross - fees, places=4)
        self.assertAlmostEqual(p.gross_mid_pnl_usd - p.spread_cost_usd - p.slippage_cost_usd, p.gross_pnl_usd, places=4)

    def test_mark_uses_bid_and_exit_fee(self):
        p = self.book.open_pending(sig(), market("X"), T0)
        self.book.apply_entry_fill(p, FillEvent("a", T0, 50, 10, 0.18))
        self.book.mark(p, 52, "quadratic", 1.0)
        self.assertAlmostEqual(p.unrealized_pnl_usd, 0.20 - 0.18 - fee_usd(10, 52), places=4)
        self.assertEqual(p.peak_bid, 52)
        self.book.mark(p, 47, "quadratic", 1.0)
        self.assertEqual(p.mae_cents, -3)

    def test_void_when_entry_never_fills(self):
        p = self.book.open_pending(sig(), market("X"), T0)
        self.book.entry_finished(p, T0 + 2, "ENTRY_NO_FILL")
        self.assertEqual(p.status, "VOID")
        self.assertEqual(self.book.open_positions(), [])

    def test_settlement_pays_100_or_0_without_fee(self):
        p = self.book.open_pending(sig("no"), market("X"), T0)
        self.book.apply_entry_fill(p, FillEvent("a", T0, 40, 10, 0.17))
        self.book.settle(p, "no", T0 + 100)
        self.assertEqual(p.status, "CLOSED")
        self.assertAlmostEqual(p.realized_pnl_usd, 6.0 - 0.17, places=4)

    def test_exposure_and_reload(self):
        p = self.book.open_pending(sig(), market("X"), T0)
        self.book.apply_entry_fill(p, FillEvent("a", T0, 50, 10, 0.18))
        self.assertAlmostEqual(self.book.exposure_usd(), 5.0)
        other = PositionBook(self.db, "PAPER", "r2")
        loaded = other.load_open()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].qty, 10)
        # environments are isolated
        self.assertEqual(PositionBook(self.db, "LIVE", "r3").load_open(), [])


class TestTrailing(unittest.TestCase):
    def test_trail_never_locks_in_a_net_loss(self):
        from lvt.engine.exits import breakeven_bid, evaluate_exit
        from tests.helpers import cfg, snap
        db = open_state_db(Path(tempfile.mkdtemp()) / "s.db")
        book = PositionBook(db, "PAPER", "r")
        p = book.open_pending(sig(), market("X"), T0)
        book.apply_entry_fill(p, FillEvent("a", T0, 50, 80, fee_usd(80, 50)))
        c = cfg()
        be = breakeven_bid(p, market("X"))
        self.assertGreater(be, 53)                                  # ~1.75c fee each way at 50c
        book.mark(p, 54, "quadratic", 1.0)                          # +4c gross, below breakeven + activation
        evaluate_exit(p, snap("X", T0 + 5, 54, 55), None, market("X"), T0 + 5, c.exits)
        self.assertEqual(p.trail_stop, 0.0)
        book.mark(p, 58, "quadratic", 1.0)
        evaluate_exit(p, snap("X", T0 + 6, 58, 59), None, market("X"), T0 + 6, c.exits)
        self.assertGreaterEqual(p.trail_stop, be)
        dec, _ = evaluate_exit(p, snap("X", T0 + 7, 55, 56), None, market("X"), T0 + 7, c.exits)
        self.assertEqual(dec.reason, "TRAILING_PROFIT_TARGET")


if __name__ == "__main__":
    unittest.main()
