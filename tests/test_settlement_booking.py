"""Booking a position out at what settlement actually paid.

A settled contract pays 100c or 0c and no order is ever sent, so nothing
reached leg_orders - the table every money number on the dashboard is built
from. The consequence was not cosmetic: a position that settled rather than
being sold contributed NOTHING to realised P/L and sat in the open list
forever. For a weather bot that is how most positions end.

Of fifteen positions that settled between 16 and 24 September, twelve were
lost this way, because the position worker can only book what
/portfolio/positions still lists and the exchange drops a position the moment
it pays out.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.journal import Journal
from kalshibot.settlement import SettlementReport, book_settled_positions


class FakeMarket:
    def __init__(self, ticker, result): self.ticker, self.raw = ticker, {
        "status": "settled", "result": result}
    status = "settled"


class FakeMarketClient:
    """Resolves the tickers it was given; raises for anything else."""

    def __init__(self, results): self.results, self.asked = results, []

    def get_market(self, ticker):
        self.asked.append(ticker)
        if ticker not in self.results:
            raise RuntimeError("no such market")
        return FakeMarket(ticker, self.results[ticker])


def _journal(tmp, fills):
    j = Journal(Path(tmp) / "j.db")
    for ticker, side, action, n, px in fills:
        j.log_leg_fill(ticker=ticker, side=side, action=action, filled=n,
                       price_cents=px)
    return j


def test_a_winning_position_is_booked_at_a_dollar():
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal(tmp, [("T-1", "yes", "buy", 10, 40.0)])
        assert j.open_lines() == [("T-1", "yes", 10)]

        r = book_settled_positions(j, FakeMarketClient({"T-1": "yes"}),
                                   SettlementReport())
        assert r.booked == 1
        assert j.open_lines() == []          # the line is closed
        row = j.conn.execute(
            "SELECT * FROM leg_orders WHERE state='SETTLED'").fetchone()
        assert row["action"] == "sell" and row["avg_price_cents"] == 100.0
        assert row["error"] == Journal.SETTLEMENT_NOTE


def test_a_losing_position_is_booked_at_zero():
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal(tmp, [("T-1", "yes", "buy", 10, 40.0)])
        book_settled_positions(j, FakeMarketClient({"T-1": "no"}),
                               SettlementReport())
        row = j.conn.execute(
            "SELECT * FROM leg_orders WHERE state='SETTLED'").fetchone()
        assert row["avg_price_cents"] == 0.0
        assert j.open_lines() == []


def test_a_no_position_wins_when_the_market_resolves_no():
    """The side we hold decides whether a resolution paid us, not the
    resolution on its own. Getting this backwards would invert the P/L of
    every NO position the bot has ever held."""
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal(tmp, [("T-1", "no", "buy", 10, 30.0)])
        book_settled_positions(j, FakeMarketClient({"T-1": "no"}),
                               SettlementReport())
        row = j.conn.execute(
            "SELECT * FROM leg_orders WHERE state='SETTLED'").fetchone()
        assert row["avg_price_cents"] == 100.0      # our NO paid out


def test_booking_twice_does_not_double_count():
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal(tmp, [("T-1", "yes", "buy", 10, 40.0)])
        mc = FakeMarketClient({"T-1": "yes"})
        assert book_settled_positions(j, mc, SettlementReport()).booked == 1
        assert book_settled_positions(j, mc, SettlementReport()).booked == 0
        n = j.conn.execute(
            "SELECT COUNT(*) FROM leg_orders WHERE state='SETTLED'").fetchone()[0]
        assert n == 1


def test_an_unresolved_market_is_left_alone():
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal(tmp, [("T-1", "yes", "buy", 10, 40.0)])
        r = book_settled_positions(j, FakeMarketClient({}), SettlementReport())
        assert r.booked == 0
        assert j.open_lines() == [("T-1", "yes", 10)]


def test_a_position_already_sold_is_not_booked_again():
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal(tmp, [("T-1", "yes", "buy", 10, 40.0),
                           ("T-1", "yes", "sell", 10, 55.0)])
        mc = FakeMarketClient({"T-1": "yes"})
        assert j.open_lines() == []
        assert book_settled_positions(j, mc, SettlementReport()).booked == 0
        assert mc.asked == []              # not even looked up


def test_resolutions_already_known_are_not_fetched_again():
    """settle_forecasts has usually just resolved these very tickers. Asking
    the exchange a second time is a wasted request per market, every pass."""
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal(tmp, [("T-1", "yes", "buy", 10, 40.0)])
        mc = FakeMarketClient({})          # would raise if asked
        r = book_settled_positions(j, mc, SettlementReport(),
                                   resolutions={"T-1": True})
        assert r.booked == 1
        assert mc.asked == []


def test_the_payout_reaches_the_realised_pnl():
    """The whole point. Bought 10 at 40c, settled YES: +$6.00, not nothing."""
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal(tmp, [("T-1", "yes", "buy", 10, 40.0)])
        book_settled_positions(j, FakeMarketClient({"T-1": "yes"}),
                               SettlementReport())
        spent = got = 0.0
        for r in j.conn.execute(
                "SELECT action, filled, avg_price_cents FROM leg_orders"):
            v = r["filled"] * r["avg_price_cents"] / 100.0
            if r["action"] == "buy":
                spent += v
            else:
                got += v
        assert abs((got - spent) - 6.00) < 0.01


# ---------------- shorts ----------------
def test_a_short_is_closed_with_a_buy_not_a_sell():
    """The four accidental shorts of 19-20 September. Booking one with a sell
    would leave the line at DOUBLE the size instead of flat."""
    with tempfile.TemporaryDirectory() as tmp:
        # sold 13 NO with nothing behind it: a short
        j = _journal(tmp, [("T-1", "no", "sell", 13, 41.0)])
        assert j.open_lines() == [("T-1", "no", -13)]

        book_settled_positions(j, FakeMarketClient({"T-1": "yes"}),
                               SettlementReport())
        row = j.conn.execute(
            "SELECT * FROM leg_orders WHERE state='SETTLED'").fetchone()
        assert row["action"] == "buy"
        assert row["filled"] == 13
        assert j.open_lines() == []


def test_a_short_in_a_contract_that_expired_worthless_keeps_the_premium():
    """Sold 13 NO at 41c, the market resolved YES so NO is worthless:
    buy back at 0c and keep $5.33."""
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal(tmp, [("T-1", "no", "sell", 13, 41.0)])
        book_settled_positions(j, FakeMarketClient({"T-1": "yes"}),
                               SettlementReport())
        row = j.conn.execute(
            "SELECT * FROM leg_orders WHERE state='SETTLED'").fetchone()
        assert row["avg_price_cents"] == 0.0
        got = spent = 0.0
        for r in j.conn.execute(
                "SELECT action, filled, avg_price_cents FROM leg_orders"):
            v = r["filled"] * r["avg_price_cents"] / 100.0
            spent += v if r["action"] == "buy" else 0
            got += v if r["action"] == "sell" else 0
        assert abs((got - spent) - 5.33) < 0.01


def test_a_short_in_a_contract_that_paid_out_costs_the_full_dollar():
    """Same trade, other outcome: buy 13 back at 100c for $13.00, a $7.67
    loss. The settlement PRICE does not mirror for a short - only the
    direction does."""
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal(tmp, [("T-1", "no", "sell", 13, 41.0)])
        book_settled_positions(j, FakeMarketClient({"T-1": "no"}),
                               SettlementReport())
        row = j.conn.execute(
            "SELECT * FROM leg_orders WHERE state='SETTLED'").fetchone()
        assert row["avg_price_cents"] == 100.0
        got = spent = 0.0
        for r in j.conn.execute(
                "SELECT action, filled, avg_price_cents FROM leg_orders"):
            v = r["filled"] * r["avg_price_cents"] / 100.0
            spent += v if r["action"] == "buy" else 0
            got += v if r["action"] == "sell" else 0
        assert abs((got - spent) - (-7.67)) < 0.01


def test_a_line_the_exchange_still_lists_is_left_to_the_position_worker():
    """If it is still on the exchange it has not settled for us yet. We only
    write down what the exchange has already let go of."""
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal(tmp, [("T-1", "yes", "buy", 10, 40.0)])
        mc = FakeMarketClient({"T-1": "yes"})
        r = book_settled_positions(j, mc, SettlementReport(),
                                   exchange_tickers={"T-1"})
        assert r.booked == 0
        assert mc.asked == []
        assert j.open_lines() == [("T-1", "yes", 10)]
