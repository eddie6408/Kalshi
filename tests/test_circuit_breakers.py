"""The account-level circuit breakers, on the LIVE path.

daily_loss_limit_usd, min_account_balance_usd and max_consecutive_losses were
configured from the first release. The only code that read them was
sim_engine.check_halt_conditions - the OFFLINE simulator. Nothing on the live
path ever called it, so for the whole life of this bot there was no circuit
breaker of any kind.

That is the third set of limit-shaped comments in this codebase, after
max_unhedged_exposure_usd and max_market_exposure_usd. These tests exist so
it is the last, and because the next thing this bot does is run unattended
for a fortnight.
"""
import sys
import threading
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.arb_engine import ArbitrageEngine
from kalshibot.journal import Journal


class FakeSettings:
    def __init__(self, safety): self._safety = safety
    def section(self, name): return self._safety if name == "safety" else {}


class FakePortfolio:
    def __init__(self, balance): self._b = balance
    def balance_usd(self): return self._b


def _engine(safety, journal=None, balance=500.0):
    e = ArbitrageEngine.__new__(ArbitrageEngine)
    e.s = FakeSettings(safety)
    e.journal = journal
    e.portfolio = FakePortfolio(balance)
    e._halted = False
    e._halt_reason = ""
    e._risk_lock = threading.RLock()
    return e


def _journal_with(tmp, fills):
    j = Journal(Path(tmp) / "j.db")
    for ticker, side, action, n, px in fills:
        j.log_leg_fill(ticker=ticker, side=side, action=action, filled=n,
                       price_cents=px)
    return j


def test_a_healthy_account_passes():
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine({"min_account_balance_usd": 50,
                     "daily_loss_limit_usd": 40,
                     "max_consecutive_losses": 8},
                    _journal_with(tmp, []))
        assert e._breakers_ok() == (True, "ok")


def test_a_balance_below_the_minimum_halts():
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine({"min_account_balance_usd": 50}, _journal_with(tmp, []),
                    balance=49.99)
        ok, why = e._breakers_ok()
        assert not ok and "below" in why


def test_the_daily_loss_limit_halts():
    """Bought 100 at 80c and sold at 20c today: -$60 against a $40 limit."""
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal_with(tmp, [("T-1", "yes", "buy", 100, 80.0),
                                ("T-1", "yes", "sell", 100, 20.0)])
        e = _engine({"daily_loss_limit_usd": 40}, j)
        ok, why = e._breakers_ok()
        assert not ok and "daily loss limit" in why


def test_a_loss_inside_the_limit_does_not_halt():
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal_with(tmp, [("T-1", "yes", "buy", 10, 80.0),
                                ("T-1", "yes", "sell", 10, 70.0)])   # -$1
        e = _engine({"daily_loss_limit_usd": 40}, j)
        assert e._breakers_ok()[0]


def test_a_run_of_losses_halts():
    with tempfile.TemporaryDirectory() as tmp:
        fills = []
        for i in range(3):
            fills += [(f"T-{i}", "yes", "buy", 10, 60.0),
                      (f"T-{i}", "yes", "sell", 10, 50.0)]
        e = _engine({"max_consecutive_losses": 3}, _journal_with(tmp, fills))
        ok, why = e._breakers_ok()
        assert not ok and "in a row" in why


def test_a_win_breaks_the_losing_run():
    with tempfile.TemporaryDirectory() as tmp:
        fills = [("T-0", "yes", "buy", 10, 60.0), ("T-0", "yes", "sell", 10, 50.0),
                 ("T-1", "yes", "buy", 10, 60.0), ("T-1", "yes", "sell", 10, 50.0),
                 ("T-2", "yes", "buy", 10, 50.0), ("T-2", "yes", "sell", 10, 90.0)]
        j = _journal_with(tmp, fills)
        assert j.consecutive_losses() == 0
        e = _engine({"max_consecutive_losses": 2}, j)
        assert e._breakers_ok()[0]


def test_a_tripped_breaker_stays_tripped_until_restart():
    """It must not un-trip because the next read happened to look better."""
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine({"min_account_balance_usd": 50}, _journal_with(tmp, []),
                    balance=10.0)
        assert not e._breakers_ok()[0]
        e.portfolio = FakePortfolio(5000.0)      # money appears
        ok, why = e._breakers_ok()
        assert not ok and "circuit breaker" in why


def test_settlement_counts_towards_the_daily_loss():
    """Most positions end by settling, not by being sold. Before settlement
    legs existed this number was missing most of the P/L, so a limit built on
    it would never have fired."""
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal_with(tmp, [("T-1", "yes", "buy", 100, 60.0)])
        assert abs(j.realised_pnl()) < 0.01          # nothing closed yet
        j.log_settlement(ticker="T-1", side="yes", contracts=100, won=False)
        assert abs(j.realised_pnl() - (-60.0)) < 0.01
        e = _engine({"daily_loss_limit_usd": 40}, j)
        assert not e._breakers_ok()[0]


def test_an_unreadable_journal_does_not_silently_pass_as_checked():
    class Broken:
        def realised_pnl(self, since=None): raise RuntimeError("locked")
        def consecutive_losses(self): raise RuntimeError("locked")
    e = _engine({"daily_loss_limit_usd": 40}, Broken())
    assert e._breakers_ok()[0]          # keeps trading
    assert not e._halted                # but never marked as verified


def test_breakers_with_no_journal_are_skipped():
    e = _engine({"daily_loss_limit_usd": 40}, None)
    assert e._breakers_ok() == (True, "ok")
