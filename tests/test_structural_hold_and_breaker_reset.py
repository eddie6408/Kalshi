"""26 September: guaranteed arbitrage baskets were being dumped leg by leg.

The position manager's exits are written for FORECAST positions: a stop, a
thesis, an estimate to lose. A leg of a structural basket has none of those -
its payout comes from holding every leg to settlement. Because no estimator
"owned" those markets, each leg was sold at the bid seconds after purchase
("no estimator owns this market"), eight small losses in a row, and the
consecutive-loss breaker tripped.

Then the breaker could not be cleared: it counts all history, only a win ends
a run, and a tripped breaker opens nothing that can win. These pin both fixes.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kalshibot.journal import Journal
from kalshibot.models import Position
from kalshibot.position_manager import ExitReason

from test_circuit_breakers import _engine, _journal_with
from test_positions import book, cfg, view, worker


# ------------------------------------------------ structural legs are held
def test_a_leg_no_forecast_owns_is_held_to_settlement():
    w = worker(positions=[Position(ticker="KXWTI-26SEP2514-T94.99", contracts=-25)],
               books={"KXWTI-26SEP2514-T94.99": book("KXWTI-26SEP2514-T94.99", no_bid=82)})
    r = w.review_once()[0]
    assert r.view.forecast_owned is False
    assert r.decision.reason is ExitReason.HOLD
    assert not r.decision.should_sell and not r.sold


def test_structural_legs_ignore_stops_and_the_closing_window_too():
    """A single leg of a hedged basket can look 90% down on its own mark
    while the basket is fine. None of the forecast exits apply."""
    w = worker()
    v = view(forecast_owned=False, our_probability=None, exit_bid_cents=3,
             entry_price_cents=60.0, minutes_to_close=5.0)
    assert w.decide(v).reason is ExitReason.HOLD


def test_a_settled_structural_leg_is_still_booked():
    w = worker()
    v = view(forecast_owned=False, market_status="settled", resolution=True)
    assert w.decide(v).reason is ExitReason.SETTLED


def test_forecast_positions_keep_every_exit():
    w = worker(exit_when_blind=True)
    assert w.decide(view(our_probability=None)).reason is ExitReason.NO_ESTIMATE


def test_the_old_behaviour_is_one_switch_away():
    w = worker(hold_unowned_to_settlement=False, exit_when_blind=True)
    v = view(forecast_owned=False, our_probability=None)
    assert w.decide(v).reason is ExitReason.NO_ESTIMATE


# ---------------------------------------------------- the breaker reset
LOSING_ROUND_TRIPS = [(f"KXWTI-{i}", "yes", a, 25, px)
                      for i in range(9) for a, px in (("buy", 15.0), ("sell", 14.0))]


def test_without_a_reset_the_run_never_ends():
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal_with(tmp, LOSING_ROUND_TRIPS)
        assert j.consecutive_losses() == 9
        ok, why = _engine({"max_consecutive_losses": 8}, j)._breakers_ok()
        assert not ok and "9 losing positions in a row" in why


def test_a_reset_clears_the_run_and_the_limit_still_applies_to_new_losses():
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal_with(tmp, LOSING_ROUND_TRIPS)
        assert j.reset_breaker("reviewed: arbitrage legs were being dumped") == 9
        assert j.consecutive_losses() == 0
        assert _engine({"max_consecutive_losses": 8}, j)._breakers_ok() == (True, "ok")

        for i in range(8):                               # eight NEW losses
            j.log_leg_fill(ticker=f"NEW-{i}", side="yes", action="buy",
                           filled=10, price_cents=50.0)
            j.log_leg_fill(ticker=f"NEW-{i}", side="yes", action="sell",
                           filled=10, price_cents=40.0)
        assert j.consecutive_losses() == 8
        ok, _why = _engine({"max_consecutive_losses": 8}, j)._breakers_ok()
        assert not ok


def test_the_reset_is_recorded_with_its_reason():
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal_with(tmp, LOSING_ROUND_TRIPS)
        j.reset_breaker("why")
        row = j.conn.execute("SELECT * FROM breaker_resets").fetchone()
        assert row["losses_at_reset"] == 9 and row["note"] == "why"
        assert j.last_breaker_reset() == row["ts"]


def test_an_old_journal_without_resets_behaves_as_before():
    with tempfile.TemporaryDirectory() as tmp:
        j = Journal(Path(tmp) / "j.db")
        assert j.last_breaker_reset() is None and j.consecutive_losses() == 0
