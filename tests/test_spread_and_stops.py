"""The 19 September loss machine.

One market, KXHIGHCHI-26SEP19-T72, lost $24 in a day while every other trade
the bot made was net positive. The log shows exactly how:

    15:55:11  buy  no x12 @ 83c
    15:55:24  EXIT STOP_LOSS  x12 NO @ 14c  (down $8.24 on $9.92 (83%))

Thirteen seconds. Nothing happened to the weather in thirteen seconds. What
happened is that the NO book was bid 14 / ask 83, the bot paid the ask, marked
itself against the bid, called the 69-cent spread an 83% loss, and sold into
that same bid - turning a paper spread into real money. Then it bought back in
and did it again, four more times.

Three separate things had to be wrong for that to happen, and each gets its
own tests here.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.depth import DepthBook, Level
from kalshibot.position_manager import (ExitReason, PositionConfig,
                                        PositionView, PositionWorker)
from kalshibot.strategies.forecast import ForecastConfig, _spread_cents


def book(yes_bid=None, yes_ask=None, no_bid=None, no_ask=None, qty=500):
    """A DepthBook holds BIDS for each side. Buying YES consumes NO bids."""
    yes_bids = [Level(yes_bid, qty)] if yes_bid else []
    no_bids = [Level(no_bid, qty)] if no_bid else []
    return DepthBook("T", yes_bids, no_bids)


# ===================== 1. the spread itself =====================
def test_the_chicago_book_is_recognised_as_unmarketable():
    # NO bid 14 / NO ask 83. In DepthBook terms: selling NO hits the NO bid
    # (14c), buying NO consumes the YES bid at 17c -> a NO ask of 83c.
    b = DepthBook("T", [Level(17, 500)], [Level(14, 500)])
    assert _spread_cents(b, "no") == 83 - 14


def test_a_tight_book_has_a_small_spread():
    b = DepthBook("T", [Level(44, 500)], [Level(54, 500)])
    assert _spread_cents(b, "yes") == 46 - 44


def test_a_one_sided_book_has_no_spread_to_measure():
    assert _spread_cents(DepthBook("T", [Level(40, 10)], []), "no") is None
    assert _spread_cents(DepthBook("T", [], []), "yes") is None


def test_the_spread_limit_is_on_by_default_and_configurable():
    assert ForecastConfig.from_dict({}).max_spread_cents == 15
    assert ForecastConfig.from_dict({"max_spread_cents": 4}).max_spread_cents == 4


# ===================== 2. marking a loss =====================
class FakePortfolio:
    def positions(self):
        return []


def worker(**cfg):
    return PositionWorker(None, FakePortfolio(), None, [], None,
                          PositionConfig.from_dict(cfg))


def view(**kw):
    base = dict(ticker="KXHIGHCHI-26SEP19-T72", side="no", quantity=12,
                exit_bid_cents=14, entry_side_ask_cents=83,
                entry_price_cents=83.0, our_probability=0.60,
                confidence=0.9, minutes_to_close=600.0,
                market_status="active",
                opened_at=datetime.now(timezone.utc) - timedelta(hours=3))
    base.update(kw)
    return PositionView(**base)


def test_the_mark_is_the_mid_not_the_bid():
    """And be honest about how much that buys us.

    On a book this pathological the mid is 48.5c against an 83c entry, so the
    mid mark still shows a 42% loss - it halves the phantom loss rather than
    removing it, and only just stays under a 50% stop. The mid mark is a
    mitigation. The thing that actually fixes this market is never entering
    it: see the spread guard above.
    """
    v = view()
    assert v.mark_cents == (14 + 83) / 2                 # 48.5c
    assert v.unrealised_usd < -8, "the bid mark is still there, for exits"
    assert -5 < v.marked_pnl_usd < -3                    # about -$4.14
    # Bid mark: 83% down, stop fires. Mid mark: 42% down, it does not.
    cost = v.cost_usd
    assert v.unrealised_usd < -0.50 * cost
    assert v.marked_pnl_usd > -0.50 * cost


def test_a_one_sided_book_still_marks_at_the_bid():
    # No ask to average with. The bid is all we have, and it is honest.
    v = view(entry_side_ask_cents=None)
    assert v.mark_cents == 14.0


def test_the_stop_does_not_fire_on_the_spread_alone():
    """THE regression. Entry at the ask of a wide book is not a loss."""
    d = worker(stop_loss_fraction=0.50,
               min_hold_minutes_before_stop=20).decide(view())
    assert d.reason is not ExitReason.STOP_LOSS


def test_a_real_loss_still_stops():
    # Both sides fell: bought at 83c, the whole book is now near 20c.
    v = view(exit_bid_cents=18, entry_side_ask_cents=22)
    d = worker(stop_loss_fraction=0.50,
               min_hold_minutes_before_stop=20).decide(v)
    assert d.reason is ExitReason.STOP_LOSS
    assert "mark" in d.detail


def test_the_exit_price_is_still_the_bid():
    # We mark at the mid to DECIDE. We sell at the bid, because that is the
    # only price actually available.
    v = view(exit_bid_cents=18, entry_side_ask_cents=22)
    d = worker(stop_loss_fraction=0.50,
               min_hold_minutes_before_stop=20).decide(v)
    assert d.sell_price_cents == 18


# ===================== 3. stopping too soon =====================
def test_a_stop_cannot_fire_in_the_first_minutes():
    """Nothing is learned in thirteen seconds. What moves in that window is
    the book, not the world."""
    v = view(exit_bid_cents=18, entry_side_ask_cents=22,
             opened_at=datetime.now(timezone.utc) - timedelta(seconds=13))
    d = worker(stop_loss_fraction=0.50,
               min_hold_minutes_before_stop=20).decide(v)
    assert d.reason is not ExitReason.STOP_LOSS


def test_the_stop_arms_once_the_position_is_old_enough():
    v = view(exit_bid_cents=18, entry_side_ask_cents=22,
             opened_at=datetime.now(timezone.utc) - timedelta(minutes=21))
    d = worker(stop_loss_fraction=0.50,
               min_hold_minutes_before_stop=20).decide(v)
    assert d.reason is ExitReason.STOP_LOSS
    assert "held 21 min" in d.detail


def test_an_unknown_open_time_does_not_disarm_the_stop():
    # Failing open here would mean a position with no recorded entry can never
    # be stopped out at all.
    v = view(exit_bid_cents=18, entry_side_ask_cents=22, opened_at=None)
    d = worker(stop_loss_fraction=0.50,
               min_hold_minutes_before_stop=20).decide(v)
    assert d.reason is ExitReason.STOP_LOSS


def test_settlement_still_beats_the_minimum_hold():
    # A young position inside the settlement window must still be closed.
    v = view(minutes_to_close=10,
             opened_at=datetime.now(timezone.utc) - timedelta(seconds=30))
    d = worker(close_within_minutes=45,
               min_hold_minutes_before_stop=20).decide(v)
    assert d.reason is ExitReason.CLOSING_SOON
