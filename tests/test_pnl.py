"""The P/L the operator sees has to be the P/L that happened.

Until 27 September the watch screen summed executions.realised_edge_usd -
the edge a trade was EXPECTED to make at entry. A forecast trade has no
guaranteed payout, so that was recorded as minus the whole purchase price:
every weather purchase read as a total loss, and the screen showed -$119 on
an account that was down about $20.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kalshibot.journal import Journal, pnl_summary


def _j(tmp):
    return Journal(Path(tmp) / "j.db")


def fill(j, ticker, action, n, px, side="yes"):
    j.log_leg_fill(ticker=ticker, side=side, action=action, filled=n, price_cents=px)


def test_buying_is_not_losing():
    with tempfile.TemporaryDirectory() as tmp:
        j = _j(tmp)
        fill(j, "W-1", "buy", 25, 15.0)
        p = pnl_summary(j.conn)
        assert p["realised"] == 0.0 and p["closed"] == 0
        assert p["open"] == 1 and abs(p["open_cost"] - 3.75) < 1e-9


def test_a_sale_and_a_settlement_both_realise():
    with tempfile.TemporaryDirectory() as tmp:
        j = _j(tmp)
        fill(j, "W-1", "buy", 25, 15.0)
        fill(j, "W-1", "sell", 25, 3.0)                 # -$3.00
        fill(j, "W-2", "buy", 10, 40.0)
        j.log_settlement(ticker="W-2", side="yes", contracts=10, won=True)   # +$6.00
        p = pnl_summary(j.conn)
        assert abs(p["realised"] - 3.0) < 1e-9
        assert p["closed"] == 2 and p["wins"] == 1 and p["open"] == 0


def test_a_settlement_at_zero_is_a_loss_not_a_missing_price():
    with tempfile.TemporaryDirectory() as tmp:
        j = _j(tmp)
        fill(j, "W-1", "buy", 10, 20.0)
        j.log_settlement(ticker="W-1", side="yes", contracts=10, won=False)
        assert abs(pnl_summary(j.conn)["realised"] - (-2.0)) < 1e-9


def test_today_counts_only_positions_closed_since():
    with tempfile.TemporaryDirectory() as tmp:
        j = _j(tmp)
        fill(j, "OLD", "buy", 10, 50.0)
        fill(j, "OLD", "sell", 10, 40.0)
        cut = j.conn.execute("SELECT MAX(ts) FROM leg_orders").fetchone()[0]
        fill(j, "NEW", "buy", 10, 50.0)
        fill(j, "NEW", "sell", 10, 70.0)
        p = pnl_summary(j.conn, since=cut + "z")      # after OLD's close
        assert abs(p["realised"] - 1.0) < 1e-9 and abs(p["realised_since"] - 2.0) < 1e-9


def test_a_forecast_trade_books_no_entry_edge_but_arbitrage_does():
    from test_execution import FakeExec, FakeMarket, deep, manager, opportunity
    m = manager(FakeExec(), mk=FakeMarket({"A": deep("A"), "B": deep("B")}))
    r = m.execute(opportunity(("A", "B"), sides=("yes",)))
    assert r.realised_edge_usd is not None            # guaranteed basket: booked
    r.opportunity.guaranteed = False                  # the same fill, as a forecast
    m._finalise(r)
    assert r.realised_edge_usd is None                # nothing is known at entry


def test_the_balance_is_recorded():
    with tempfile.TemporaryDirectory() as tmp:
        j = _j(tmp)
        j.log_balance(442.56)
        assert j.conn.execute("SELECT balance_usd FROM account_snapshots").fetchone()[0] == 442.56


def test_the_watch_screen_shows_real_pnl_and_the_balance():
    import sqlite3
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import monitor
    with tempfile.TemporaryDirectory() as tmp:
        j = _j(tmp)
        fill(j, "W-1", "buy", 25, 15.0)
        fill(j, "W-1", "sell", 25, 3.0)
        j.log_balance(442.56)
        j.conn.execute("INSERT INTO executions (ts, strategy, outcome, total_cost_usd, "
                       "total_fees_usd, realised_edge_usd, exposed) VALUES "
                       "('t', 'forecast:w', 'COMPLETE', 144.0, 1.0, -145.0, 0)")
        j.conn.commit()
        conn = sqlite3.connect(f"file:{Path(tmp) / 'j.db'}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        frame = monitor.render(conn, Path(tmp) / "j.db", 5)
        assert "REALISED P/L" in frame and "$-3.00" in frame
        assert "DEMO BALANCE" in frame and "$442.56" in frame
        assert "-145" not in frame and "DEMO P/L" not in frame
