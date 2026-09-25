"""A sell with no position behind it opens a short. Don't send one.

This is the 19-20 September defect, in the bot's own fill record:

    09-19 12:55  buy  12 NO   KXHIGHCHI-26SEP19-T72    flat -> short
    09-19 12:55  sell 12 NO                            short -> FLAT
    09-19 13:43  sell 11 NO                            FLAT -> long 11   <--
    09-20 01:09  sell 10 YES  KXHIGHMIA-26SEP19-B88.5  long -> FLAT
    09-20 01:15  sell  7 YES                           FLAT -> short 7   <--
    09-20 01:21  sell  4 YES                           short -> short 11 <--

Each marked line is the bot exiting a position it had already exited, because
/portfolio/positions still reported the pre-trade quantity on the next review
thirty seconds later. Kalshi does not reject those orders. It fills them, and
the fill opens the opposite position.

The rule being pinned: we sell the SMALLER of what the exchange reports and
what our own fills say is left - and our fills only get a vote when they have
actually seen the market.
"""
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.depth import DepthBook, Level
from kalshibot.models import Market
from kalshibot.position_manager import (ExitDecision, ExitReason, PositionConfig,
                                        PositionView, PositionWorker, ReviewResult)


class FakeExec:
    def __init__(self):
        self.sent = []

    def place_demo_order(self, ticker, action, side, count, limit_price_cents, **kw):
        self.sent.append((ticker, action, side, count, limit_price_cents))
        return {"order": {"order_id": "o1", "filled_count": count}}


class FakeMarketSrc:
    def get_depth(self, ticker, depth=50):
        return DepthBook(ticker, [Level(60, 500)], [Level(40, 500)])

    def get_market(self, ticker, **kw):
        return Market(ticker=ticker, event_ticker="EV", series_ticker="KXFAKE",
                      status="active",
                      close_time=datetime.now(timezone.utc) + timedelta(hours=6))


class FakeJournal:
    """`held` is what our own fills say is left. None means we have no record."""

    def __init__(self, held=None, raises=False):
        self.held, self.raises = held, raises
        self.legs = []

    def held_from_fills(self, ticker, side):
        if self.raises:
            raise RuntimeError("database is locked")
        return self.held

    def entry_for(self, ticker, side):
        return (self.held or 0), 40.0

    def log_leg_fill(self, **kw):
        self.legs.append(kw)

    def close_open_trades(self, ticker, yes_px, no_px, outcome=None):
        return 1, 1.0


def _worker(journal=None, ex=None, **cfg):
    base = {"bankroll_usd": 1000.0, "kelly_fraction": 0.25,
            "max_cost_per_trade_usd": 25.0}
    base.update(cfg)
    return PositionWorker(
        FakeMarketSrc(), None, ex or FakeExec(), [], journal,
        PositionConfig.from_dict(base), dry_run=False)


def _sell(worker, exchange_says, quantity=None, reason=ExitReason.STOP_LOSS):
    """Ask the worker to sell, with the exchange reporting `exchange_says`."""
    v = PositionView(ticker="KXFAKE-1", side="yes", quantity=exchange_says,
                     exit_bid_cents=60, entry_price_cents=40.0,
                     minutes_to_close=600, market_status="active")
    d = ExitDecision(reason=reason, detail="test",
                     sell_price_cents=60, sell_quantity=quantity)
    r = ReviewResult(view=v, decision=d)
    worker._exit(r)
    return r


# ---------------- the defect, pinned ----------------
def test_a_sell_is_refused_when_our_fills_say_the_position_is_gone():
    ex = FakeExec()
    w = _worker(journal=FakeJournal(held=0), ex=ex)
    r = _sell(w, exchange_says=11)          # exchange is stale, we are flat
    assert ex.sent == []                    # the order that made a short
    assert "already closed" in r.error
    assert r.sold == 0


def test_a_sell_is_trimmed_to_what_our_fills_say_is_left():
    ex = FakeExec()
    w = _worker(journal=FakeJournal(held=4), ex=ex)
    r = _sell(w, exchange_says=11)
    assert len(ex.sent) == 1
    assert ex.sent[0][3] == 4               # not 11
    assert r.sold == 4


def test_the_whole_position_still_sells_when_the_two_agree():
    ex = FakeExec()
    w = _worker(journal=FakeJournal(held=11), ex=ex)
    r = _sell(w, exchange_says=11)
    assert ex.sent[0][3] == 11
    assert r.sold == 11
    assert not r.error


# ---------------- and it must not trap real positions ----------------
def test_a_position_our_record_has_never_seen_can_still_be_exited():
    # Opened by hand, or a journal that was replaced. Abstain, do not veto.
    ex = FakeExec()
    w = _worker(journal=FakeJournal(held=None), ex=ex)
    r = _sell(w, exchange_says=11)
    assert ex.sent[0][3] == 11
    assert not r.error


def test_a_journal_we_cannot_read_does_not_mean_zero():
    ex = FakeExec()
    w = _worker(journal=FakeJournal(raises=True), ex=ex)
    r = _sell(w, exchange_says=11)
    assert ex.sent[0][3] == 11
    assert not r.error


class OldJournal:
    """A journal from before the clamp existed - no held_from_fills."""

    def __init__(self): self.legs = []
    def entry_for(self, ticker, side): return 0, 40.0
    def log_leg_fill(self, **kw): self.legs.append(kw)
    def close_open_trades(self, t, y, n, outcome=None): return 1, 1.0


def test_a_journal_too_old_for_the_clamp_says_so_out_loud():
    """The 22 September incident: an older installer re-run put back a
    journal.py from before the clamp. Config still said reduce_only: true,
    the bot still started, and the protection was simply gone.

    Abstaining is the right behaviour - we cannot prove anything about the
    position - but doing it silently is how a safety feature becomes
    decorative. The sale still goes through; the log has to shout."""
    ex = FakeExec()
    w = _worker(journal=OldJournal(), ex=ex)
    log = logging.getLogger("kalshibot.position_manager")
    cap = _Capture()
    log.addHandler(cap)
    previous = log.level
    log.setLevel(logging.WARNING)
    try:
        r = _sell(w, exchange_says=11)
    finally:
        log.removeHandler(cap)
        log.setLevel(previous)
    assert ex.sent[0][3] == 11          # not trapped
    assert not r.error
    assert any("THE CLAMP IS NOT RUNNING" in m for m in cap.messages)


def test_no_journal_at_all_behaves_as_before():
    ex = FakeExec()
    w = _worker(journal=None, ex=ex)
    r = _sell(w, exchange_says=11)
    assert ex.sent[0][3] == 11


def test_the_clamp_can_be_turned_off():
    ex = FakeExec()
    w = _worker(journal=FakeJournal(held=0), ex=ex, reduce_only=False)
    r = _sell(w, exchange_says=11)
    assert ex.sent[0][3] == 11              # the old, short-making behaviour
    assert PositionConfig.from_dict({}).reduce_only is True


# ---------------- the clamp applies to trims too ----------------
def test_a_partial_reduction_is_clamped_the_same_way():
    # The Miami sequence: 10 sold, then trims of 7, 4 and 1 against nothing.
    ex = FakeExec()
    w = _worker(journal=FakeJournal(held=0), ex=ex)
    r = _sell(w, exchange_says=10, quantity=7, reason=ExitReason.REDUCE)
    assert ex.sent == []
    assert r.sold == 0


def test_nothing_is_journalled_when_nothing_was_sold():
    j = FakeJournal(held=0)
    w = _worker(journal=j, ex=FakeExec())
    _sell(w, exchange_says=11)
    assert j.legs == []


# ---------------- the ledger itself, on a real database ----------------
def test_the_fill_record_replays_the_chicago_sequence():
    """Replay the real 19 September fills and check what the ledger says.

    Only the buys and sells that actually happened, in order. After the first
    round trip the ledger must say zero - which is the answer that would have
    stopped the 13:43 sell from being sent.
    """
    import tempfile
    from kalshibot.journal import Journal

    with tempfile.TemporaryDirectory() as tmp:
        j = Journal(Path(tmp) / "j.db")
        T = "KXHIGHCHI-26SEP19-T72"

        # nothing recorded yet: no opinion, so no veto
        assert j.held_from_fills(T, "no") is None

        j.log_leg_fill(ticker=T, side="no", action="buy", filled=12,
                       price_cents=82.67)
        assert j.held_from_fills(T, "no") == 12

        j.log_leg_fill(ticker=T, side="no", action="sell", filled=12,
                       price_cents=14.0, note="STOP_LOSS")
        assert j.held_from_fills(T, "no") == 0      # <- the 13:43 veto

        # the other side of the same market is tracked separately, and we
        # never bought YES here, so there is nothing to sell there either
        assert j.held_from_fills(T, "yes") is None

        # a fresh entry re-arms it
        j.log_leg_fill(ticker=T, side="no", action="buy", filled=11,
                       price_cents=39.0)
        assert j.held_from_fills(T, "no") == 11


def test_an_oversold_ledger_abstains_instead_of_trapping_the_position():
    """A ledger already gone negative must not be allowed to veto.

    This is the Austin sequence: 16 bought, 16 sold, then 13 more sold against
    nothing. Our record for that side is now demonstrably wrong.

    The tempting answer is to floor it at zero, but zero VETOES - and the
    exchange really is holding a short there, which the bot would then be
    unable to close, trapped behind our own bad arithmetic. So a negative
    abstains and says so, and the exchange's number is used. It re-arms on its
    own once the market is flat and fresh fills start the count over.
    """
    import tempfile
    from kalshibot.journal import Journal

    with tempfile.TemporaryDirectory() as tmp:
        j = Journal(Path(tmp) / "j.db")
        T = "KXHIGHAUS-26SEP19-B96.5"
        j.log_leg_fill(ticker=T, side="no", action="buy", filled=16,
                       price_cents=57.25)
        j.log_leg_fill(ticker=T, side="no", action="sell", filled=16,
                       price_cents=55.0, note="CLOSING_SOON")
        assert j.held_from_fills(T, "no") == 0      # still a clean zero

        j.log_leg_fill(ticker=T, side="no", action="sell", filled=13,
                       price_cents=41.0, note="CLOSING_SOON")
        assert j.held_from_fills(T, "no") is None   # now it abstains


def test_an_oversold_ledger_does_not_block_the_exit():
    """The same thing one level up: the clamp lets the sale through."""
    ex = FakeExec()
    w = _worker(journal=FakeJournal(held=None), ex=ex)   # None = abstained
    r = _sell(w, exchange_says=13)
    assert ex.sent[0][3] == 13
    assert not r.error


class _Capture(logging.Handler):
    """Collect log records without pytest's caplog.

    The bot ships a zero-dependency runner (tests/run_tests.py) so the suite
    works on a machine with nothing but Python, and run_bot.sh refuses to
    start on a failed self-test. A fixture only pytest provides therefore
    fails the startup check on a bot that is perfectly healthy - which is
    exactly what it did.
    """

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def test_repeated_refusals_do_not_spam_the_log():
    """A warning printed 120 times an hour stops being read."""
    w = _worker(journal=FakeJournal(held=0), ex=FakeExec())
    log = logging.getLogger("kalshibot.position_manager")
    cap = _Capture()
    log.addHandler(cap)
    previous = log.level
    log.setLevel(logging.WARNING)
    try:
        for _ in range(5):
            _sell(w, exchange_says=11)
    finally:
        log.removeHandler(cap)
        log.setLevel(previous)
    assert len([m for m in cap.messages if "LEDGER" in m]) == 1
