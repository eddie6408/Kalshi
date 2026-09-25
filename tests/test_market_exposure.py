"""What we already hold in a market, before buying more of it.

On 18 September the bot bought Denver T82 fourteen contracts at 63c, and then
bought fourteen more at 63c twenty minutes later. 28 contracts in one market,
and nothing said a word - every other limit is per-trade or per-event, so each
buy passed on its own.

`max_market_exposure_usd` was declared in RiskLimits, parsed from config, and
read by nothing. That is the second limit-shaped comment found in this
codebase, after max_unhedged_exposure_usd. These tests exist so it is the last.
"""
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

class _NoSafety:
    """Settings with no safety section: the circuit breakers are not what
    these tests are about, and an engine built with __new__ still needs one."""

    def section(self, name):
        return {}

    def get(self, key, default=None):
        return default

from kalshibot.arb_engine import ArbitrageEngine, RiskLimits
from kalshibot.models import Position


@dataclass
class FakeLeg:
    ticker: str
    side: str
    contracts: int
    limit_price_cents: int
    cost_usd: float = 0.0


class FakeOp:
    def __init__(self, legs):
        self.legs = legs
        self.market_tickers = [l.ticker for l in legs]
        self.quantity = max(l.contracts for l in legs)
        self.n_legs = len(legs)
        self.gross_cost = sum(l.cost_usd or l.contracts * l.limit_price_cents / 100.0
                              for l in legs)

    def summary(self):
        return "fake"


def engine(limits=None, positions=(), portfolio_raises=False):
    """An engine with only the pieces the risk check touches."""
    e = ArbitrageEngine.__new__(ArbitrageEngine)
    # Every OTHER limit non-binding, so a failure here is about the per-market
    # rule and nothing else.
    base = {"max_contracts_per_trade": 1000, "max_total_exposure_usd": 1e6,
            "max_event_exposure_usd": 1e6, "max_trades_per_hour": 1000,
            "max_legs_per_trade": 14}
    base.update(limits or {})
    e.limits = RiskLimits.from_dict(base)
    e.exposure_usd = 0.0
    e.trade_times = []
    e._positions = {}
    e._positions_unknown = False
    e._pending_cost = {}
    e._fill_holdings = {}
    e._risk_lock = threading.RLock()
    e.s = _NoSafety()
    e.journal = None          # no exit history; the cooldown has its own tests

    class P:
        def positions(self_inner):
            if portfolio_raises:
                raise RuntimeError("portfolio endpoint down")
            return list(positions)
    e.portfolio = P()
    e._refresh_positions()
    return e


def op(ticker="KXHIGHDEN-26SEP17-T82", side="yes", contracts=14, price=63):
    return FakeOp([FakeLeg(ticker, side, contracts, price,
                           contracts * price / 100.0)])


def held(ticker="KXHIGHDEN-26SEP17-T82", contracts=14, price=63):
    return Position(ticker=ticker, contracts=contracts,
                    total_cost_cents=abs(contracts) * price)


# ------------------------- the Denver case -------------------------
def test_the_second_buy_of_the_same_market_is_refused():
    e = engine({"max_market_exposure_usd": 250.0}, positions=[held()])
    ok, why = e._risk_ok(op())
    assert ok is False
    assert "already hold" in why


def test_a_market_we_do_not_hold_is_fine():
    e = engine({"max_market_exposure_usd": 250.0})
    ok, why = e._risk_ok(op())
    assert ok is True, why


def test_averaging_in_can_be_turned_on_deliberately():
    e = engine({"max_market_exposure_usd": 250.0, "allow_averaging_in": True},
               positions=[held()])
    assert e._risk_ok(op())[0] is True


def test_the_other_side_of_the_same_market_is_not_averaging_in():
    # Holding YES and buying NO is a different position, not a bigger one.
    e = engine({"max_market_exposure_usd": 250.0},
               positions=[held(contracts=14)])          # YES
    ok, why = e._risk_ok(op(side="no"))
    assert ok is True, why


# ------------------------- the money cap -------------------------
def test_the_per_market_cap_is_enforced_at_all():
    e = engine({"max_market_exposure_usd": 5.0, "allow_averaging_in": True})
    ok, why = e._risk_ok(op(contracts=14, price=63))     # $8.82
    assert ok is False
    assert "per-market limit" in why


def test_the_cap_counts_what_is_already_held():
    e = engine({"max_market_exposure_usd": 15.0, "allow_averaging_in": True},
               positions=[held()])                       # $8.82 held
    ok, why = e._risk_ok(op())                           # + $8.82 = $17.64
    assert ok is False
    assert "held" in why


def test_the_cap_allows_a_trade_that_fits():
    e = engine({"max_market_exposure_usd": 25.0, "allow_averaging_in": True},
               positions=[held()])
    assert e._risk_ok(op())[0] is True                   # $17.64 < $25


def test_a_second_opportunity_in_the_same_pass_cannot_spend_the_same_allowance():
    # The exchange will not show a position for a few seconds. Without this,
    # two opportunities in one scan both see an empty book of holdings.
    e = engine({"max_market_exposure_usd": 15.0, "allow_averaging_in": True})
    first = op()
    assert e._risk_ok(first)[0] is True
    e._remember_pending(first)
    ok, why = e._risk_ok(op())
    assert ok is False
    assert "pending" in why


def test_pending_blocks_averaging_in_even_before_the_cap_bites():
    e = engine({"max_market_exposure_usd": 250.0})
    first = op()
    assert e._risk_ok(first)[0] is True
    e._remember_pending(first)
    ok, why = e._risk_ok(op())
    assert ok is False
    assert "already buying" in why


# ------------------------- failure modes -------------------------
def test_not_knowing_what_we_hold_fails_closed():
    """Not knowing is not the same as holding nothing.

    Assuming an empty portfolio when the endpoint is down is precisely the
    assumption that bought Denver twice.
    """
    e = engine({"max_market_exposure_usd": 250.0}, portfolio_raises=True)
    ok, why = e._risk_ok(op())
    assert ok is False
    assert "cannot read current positions" in why


def test_a_refresh_clears_last_passs_pending():
    e = engine({"max_market_exposure_usd": 250.0})
    e._remember_pending(op())
    assert e._pending_cost
    e._refresh_positions()
    assert e._pending_cost == {}


def test_every_leg_of_a_multi_leg_trade_is_checked():
    e = engine({"max_market_exposure_usd": 250.0},
               positions=[held(ticker="B", contracts=10)])
    two = FakeOp([FakeLeg("A", "yes", 10, 40, 4.0),
                  FakeLeg("B", "yes", 10, 40, 4.0)])
    ok, why = e._risk_ok(two)
    assert ok is False and "B" in why


def test_a_zero_position_is_not_a_position():
    e = engine({"max_market_exposure_usd": 250.0},
               positions=[held(contracts=0)])
    assert e._risk_ok(op())[0] is True


# ------------------------- the re-entry cooldown -------------------------
class ExitHistory:
    """A journal that remembers when we last left a market."""

    def __init__(self, when=None):
        self.when = when

    def last_exit_at(self, ticker):
        return self.when


def test_a_market_we_just_left_is_not_bought_straight_back():
    """19 September: bought KXHIGHCHI-26SEP19-T72 back four times, paying a
    69-cent spread each way. The forecast does not change because a position
    closed - the estimator that said "buy" says it again the moment we are
    flat."""
    from datetime import datetime, timedelta, timezone
    e = engine({"max_market_exposure_usd": 250.0,
                "reentry_cooldown_minutes": 120})
    left = datetime.now(timezone.utc) - timedelta(minutes=5)
    e.journal = ExitHistory(left.isoformat())
    ok, why = e._risk_ok(op())
    assert ok is False
    assert "cooling off" in why


def test_the_cooldown_lapses():
    from datetime import datetime, timedelta, timezone
    e = engine({"max_market_exposure_usd": 250.0,
                "reentry_cooldown_minutes": 120})
    left = datetime.now(timezone.utc) - timedelta(minutes=121)
    e.journal = ExitHistory(left.isoformat())
    assert e._risk_ok(op())[0] is True


def test_a_market_never_traded_has_no_cooldown():
    e = engine({"max_market_exposure_usd": 250.0,
                "reentry_cooldown_minutes": 120})
    e.journal = ExitHistory(None)
    assert e._risk_ok(op())[0] is True


def test_the_cooldown_can_be_switched_off():
    from datetime import datetime, timezone
    e = engine({"max_market_exposure_usd": 250.0,
                "reentry_cooldown_minutes": 0})
    e.journal = ExitHistory(datetime.now(timezone.utc).isoformat())
    assert e._risk_ok(op())[0] is True


def test_an_unreadable_exit_time_does_not_block_trading():
    # Failing closed on a malformed timestamp would stop the bot over a
    # bookkeeping detail.
    e = engine({"max_market_exposure_usd": 250.0,
                "reentry_cooldown_minutes": 120})
    e.journal = ExitHistory("not a timestamp")
    assert e._risk_ok(op())[0] is True


# ------------- the race BETWEEN passes, which is the real one -------------
class FillJournal:
    """A journal whose fills know about a buy the exchange has not shown yet."""

    def __init__(self, holdings=None, raises=False):
        self.holdings = holdings or {}
        self.raises = raises

    def entry_for(self, ticker, side):
        if self.raises:
            raise RuntimeError("database is locked")
        if (ticker, side) not in self.holdings:
            return 0, None                 # no record at all
        n, px = self.holdings[(ticker, side)]
        return n, px

    def last_exit_at(self, ticker):
        return None


def test_a_second_pass_is_refused_while_the_exchange_is_still_behind():
    """Denver, exactly: 25 contracts at 16:25 and 25 more at 16:26.

    _pending_cost is wiped by _refresh_positions at the top of every pass, so
    between passes the old guard saw nothing held and nothing pending. The
    fill record is the half that is current.
    """
    e = engine({"max_market_exposure_usd": 250.0}, positions=[])   # exchange: empty
    e.journal = FillJournal({("KXHIGHDEN-26SEP19-B75.5", "yes"): (25, 20.0)})
    e._fill_holdings = {}
    ok, why = e._market_exposure_ok(
        op("KXHIGHDEN-26SEP19-B75.5", "yes", 25, 20))
    assert not ok
    assert "already hold 25 YES" in why


def test_the_larger_of_the_two_sources_wins():
    """A spending limit is the mirror of the exit clamp: there the smaller
    number is safe, here the larger one is."""
    e = engine({"max_market_exposure_usd": 250.0},
               positions=[held("T-1", contracts=10, price=50)])
    e.journal = FillJournal({("T-1", "yes"): (40, 50.0)})   # fills say more
    e._fill_holdings = {}
    n, cost = e._holding("T-1", "yes")
    assert (n, cost) == (40, 20.0)

    e2 = engine({"max_market_exposure_usd": 250.0},
                positions=[held("T-1", contracts=40, price=50)])
    e2.journal = FillJournal({("T-1", "yes"): (10, 50.0)})  # exchange says more
    e2._fill_holdings = {}
    assert e2._holding("T-1", "yes")[0] == 40


def test_a_market_our_fills_have_never_seen_is_not_blocked():
    e = engine({"max_market_exposure_usd": 250.0}, positions=[])
    e.journal = FillJournal({})            # no record of anything
    e._fill_holdings = {}
    ok, why = e._market_exposure_ok(op("T-NEW", "yes", 25, 20))
    assert ok, why


def test_a_journal_we_cannot_read_falls_back_to_the_exchange():
    e = engine({"max_market_exposure_usd": 250.0}, positions=[])
    e.journal = FillJournal(raises=True)
    e._fill_holdings = {}
    ok, why = e._market_exposure_ok(op("T-NEW", "yes", 25, 20))
    assert ok, why                          # not blocked by an unreadable file


def test_the_fill_lookup_is_cached_for_one_pass():
    """Called per leg per opportunity; a SQLite hit each time would be silly."""
    calls = []

    class Counting(FillJournal):
        def entry_for(self, ticker, side):
            calls.append(ticker)
            return 0, None

    e = engine({"max_market_exposure_usd": 250.0}, positions=[])
    e.journal = Counting()
    e._fill_holdings = {}
    for _ in range(5):
        e._holding("T-1", "yes")
    assert len(calls) == 1
    e._refresh_positions()                  # a new pass re-reads
    e._holding("T-1", "yes")
    assert len(calls) == 2
