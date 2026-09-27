"""Paper research mode: account limits waived in PAPER/BACKTEST (but
recorded), always enforced in SHADOW/LIVE, and LIVE gated on a limits review."""
import tempfile
import time
import unittest
from pathlib import Path

from lvt import live_gate
from lvt.db import open_state_db
from lvt.engine.risk import RiskEngine
from lvt.models import FillEvent
from tests.helpers import Harness, T0, cfg, market, path
from tests.test_risk import buy_signal, setup_market
from tests.test_strategies import DIP, only

RESEARCH = {"risk": {"enforce_limits_in_paper": False, "max_daily_loss_usd": 1.0,
                     "max_concurrent_positions": 1, "max_trades_per_hour": 1}}


def losing_day(h, tk):
    p = h.engine.book.open_pending(buy_signal(tk), h.markets[tk], T0)
    h.engine.book.apply_entry_fill(p, FillEvent("f", T0, 50, 10, 0.18))
    h.engine.book.apply_exit_fill(p, FillEvent("g", T0 + 5, 40, 10, 0.17))


class TestResearchMode(unittest.TestCase):
    def test_paper_keeps_trading_past_every_account_limit_and_records_it(self):
        h = Harness({**only("DIP"), **RESEARCH})
        self.assertFalse(h.engine.risk.limits_enforced)
        tk = list(h.markets)[0]
        losing_day(h, tk)
        h.engine.risk.note_entry(T0)                               # trade-frequency cap already used up
        h.run_path(tk, path(DIP))
        self.assertEqual(h.engine.risk.entries_halted_reason, "")
        taken = h.signals(decision="TAKEN")
        self.assertEqual(len(taken), 1)
        self.assertIn("would_block_live", taken[0]["explanation"])
        waived = h.db.query("SELECT check_name FROM risk_events WHERE detail LIKE 'NOT ENFORCED%'")
        names = {r["check_name"] for r in waived}
        self.assertTrue({"max_daily_loss", "max_trade_frequency"} <= names, names)
        self.assertFalse(h.db.query("SELECT * FROM alerts WHERE code='DAILY_LOSS_LIMIT'"))

    def test_data_validity_checks_still_enforced_in_paper(self):
        h = Harness({**only("NONE"), **RESEARCH})
        tk = setup_market(h)
        h.engine.risk.fail_closed["MARKET_DATA"] = "down"
        d = h.engine.risk.check_entry(buy_signal(tk), h.last[tk], h.engine.metrics[tk], h.markets[tk],
                                      h.last[tk].ts, False)
        self.assertFalse(d.ok)
        self.assertTrue(d.reason.startswith("fail_closed"))
        d = h.engine.risk.check_entry(buy_signal(tk), h.last[tk], h.engine.metrics[tk], h.markets[tk],
                                      h.last[tk].ts, True)
        self.assertFalse(d.ok)

    def test_shadow_and_live_always_enforce(self):
        for env in ("SHADOW", "LIVE"):
            h = Harness({**only("NONE"), **RESEARCH}, env=env)
            self.assertTrue(h.engine.risk.limits_enforced, env)
            tk = setup_market(h)
            losing_day(h, tk)
            d = h.engine.risk.check_entry(buy_signal(tk), h.last[tk], h.engine.metrics[tk], h.markets[tk],
                                          h.last[tk].ts, False)
            self.assertFalse(d.ok, env)
            self.assertTrue(d.reason.startswith("max_daily_loss"), (env, d.reason))

    def test_backtest_matches_paper(self):
        c = cfg(RESEARCH)
        db = open_state_db(Path(tempfile.mkdtemp()) / "s.db")
        from lvt.engine.positions import PositionBook
        self.assertFalse(RiskEngine(c, db, "BACKTEST", PositionBook(db, "BACKTEST", "r")).limits_enforced)

    def test_research_sizing_uses_fixed_equity(self):
        h = Harness({**only("NONE"), **RESEARCH})
        tk = setup_market(h)
        before = h.engine.risk.check_entry(buy_signal(tk), h.last[tk], h.engine.metrics[tk], h.markets[tk],
                                           h.last[tk].ts, False).qty
        p = h.engine.book.open_pending(buy_signal("Q-A-B"), market("Q-A-B"), T0)
        h.engine.book.apply_entry_fill(p, FillEvent("f", T0, 50, 100, 1.75))
        h.engine.book.apply_exit_fill(p, FillEvent("g", T0 + 5, 1, 100, 0.07))   # -$50 paper loss
        after = h.engine.risk.check_entry(buy_signal(tk), h.last[tk], h.engine.metrics[tk], h.markets[tk],
                                          h.last[tk].ts, False)
        self.assertEqual(after.qty, before)
        self.assertTrue(any(w.startswith("drawdown_halving") for w in after.waived))


class TestLimitsReviewGate(unittest.TestCase):
    def test_live_blocked_until_limits_reviewed_for_this_config(self):
        db = open_state_db(Path(tempfile.mkdtemp()) / "s.db")
        c = cfg()
        item = lambda: [i for i in live_gate.readiness(c, db) if i.name == "RISK_LIMITS_REVIEWED"][0]
        self.assertFalse(item().passed)
        self.assertIn("revisit", item().detail)
        db.insert("readiness_evidence", {"item": "RISK_LIMITS_REVIEWED", "ts": time.time(), "git_commit": "x",
                                         "config_hash": "someotherconfig", "passed": 1})
        self.assertFalse(item().passed)                            # a review of a different config does not count
        db.insert("readiness_evidence", {"item": "RISK_LIMITS_REVIEWED", "ts": time.time(), "git_commit": "x",
                                         "config_hash": c.config_hash(), "passed": 1})
        self.assertTrue(item().passed)

    def test_default_config_is_research_mode_with_review_note(self):
        from lvt.config import load_config
        c = load_config(env={})
        self.assertFalse(c.risk.enforce_limits_in_paper)
        text = (Path(__file__).resolve().parent.parent / "config" / "default.toml").read_text()
        self.assertIn("BEFORE SHADOW/LIVE: REVISIT EVERY LIMIT", text)


if __name__ == "__main__":
    unittest.main()
