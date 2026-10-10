"""Maker orders, stage 1 (C2 M1+M2; kalshibot/order_manager.py).

Pinned: the maker price never crosses and never gives away the edge; only
single-leg forecast buys rest (arbitrage and exits stay taker); orders go
out post_only with the bot's own prefix; fills are journalled as ordinary
executions as they arrive; orders are cancelled after max_rest_seconds, near
the close, or when the engine may no longer open; start-up cancels only the
bot's own orders; a resting order counts against the exposure limits.
"""
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

from kalshibot.execution_manager import TradeOutcome
from kalshibot.journal import Journal
from kalshibot.order_manager import COID_PREFIX, MakerConfig, OrderManager, maker_price

CFG = MakerConfig(enabled=True, improve_cents=1, min_edge_cents=3, max_rest_seconds=900,
                  close_within_minutes=30, poll_seconds=5, max_resting=2, maker_fee_rate=0.0)


def leg(ticker="KXHIGHNY-26OCT11-T70", side="yes", qty=10, ask=45, bid=40):
    return SimpleNamespace(ticker=ticker, side=side, action="buy", quantity=qty,
                           limit_price_cents=ask, best_ask_cents=ask, best_bid_cents=bid,
                           cost_usd=qty * ask / 100.0, contracts=qty, vwap_cents=ask)


def fop(l=None, p_yes=0.60, strategy="forecast:weather_temperature", close_in_h=10):
    l = l or leg()
    return SimpleNamespace(legs=[l], guaranteed=False, strategy_type=strategy, event_id="EV",
                           probability_yes=p_yes, opportunity_id=None, market_tickers=[l.ticker],
                           settlement_time=datetime.now(timezone.utc) + timedelta(hours=close_in_h),
                           quantity=l.quantity, gross_cost=l.cost_usd, n_legs=1,
                           summary=lambda: "fop")


class Exch:
    def __init__(self):
        self.orders, self.sent, self.cancelled, self.n = {}, [], [], 0

    def place_demo_order(self, ticker, action, side, count, limit_price_cents,
                         client_order_id=None, post_only=False, **kw):
        self.n += 1
        oid = f"o{self.n}"
        self.sent.append(dict(ticker=ticker, action=action, side=side, count=count,
                              price=limit_price_cents, coid=client_order_id, post_only=post_only))
        self.orders[oid] = {"order_id": oid, "status": "resting", "filled_count": 0,
                            "yes_price_cents": None, "ticker": ticker,
                            "client_order_id": client_order_id}
        return {"order": dict(self.orders[oid])}

    def fill(self, oid, n, price):
        o = self.orders[oid]
        o["filled_count"] = n
        o["yes_price_cents"] = price
        if n >= self.sent[int(oid[1:]) - 1]["count"]:
            o["status"] = "executed"

    def get_order(self, oid):
        return {"order": dict(self.orders[oid])}

    def cancel_demo_order(self, oid, market_ticker=None):
        self.cancelled.append(oid)
        self.orders[oid]["status"] = "canceled"
        return {}


def journal():
    return Journal(Path(tempfile.mkdtemp()) / "j.db")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# ---- pricing ---------------------------------------------------------------
def test_the_maker_price_improves_the_bid_never_crosses_and_keeps_the_edge():
    assert maker_price("yes", 0.60, 40, 45, 10, CFG)[0] == 41          # bid + 1
    assert maker_price("yes", 0.60, 40, 41, 10, CFG)[0] == 40          # ask - 1 caps it
    assert maker_price("yes", 0.43, 40, 45, 10, CFG)[0] == 40          # fair 43 - 3 edge
    assert maker_price("yes", 0.40, 40, 45, 10, CFG)[0] == 37          # sits behind the bid
    assert maker_price("no", 0.30, 60, 66, 10, CFG)[0] == 61           # NO side: fair 70c
    assert maker_price("yes", 0.02, 1, 2, 10, CFG)[0] is None          # no edge anywhere
    assert maker_price("yes", 0.6, 40, None, 10, CFG)[0] is None       # nothing to rest under


# ---- what rests --------------------------------------------------------------
def test_only_single_leg_forecast_buys_rest():
    om = OrderManager(Exch(), None, None, CFG)
    assert om.handles(fop())
    arb = fop(strategy="same_market")
    assert not om.handles(arb)
    two = fop(); two.legs = [leg(), leg("B")]
    assert not om.handles(two)
    g = fop(); g.guaranteed = True
    assert not om.handles(g)
    off = OrderManager(Exch(), None, None, MakerConfig(enabled=False))
    assert not off.handles(fop())


def test_post_sends_post_only_with_the_bot_prefix_and_respects_limits():
    ex = Exch()
    om = OrderManager(ex, None, journal(), CFG)
    r = om.post(fop())
    assert r.outcome is TradeOutcome.RESTING
    s = ex.sent[0]
    assert s["post_only"] and s["coid"].startswith(COID_PREFIX) and s["price"] == 41
    assert om.post(fop()).outcome is TradeOutcome.ABORTED_PRE_TRADE           # same market
    om.post(fop(leg("KXHIGHCHI-26OCT11-T60")))
    assert om.post(fop(leg("KXHIGHDEN-26OCT11-T80"))).outcome is TradeOutcome.ABORTED_PRE_TRADE  # max 2
    assert om.resting_cost("KXHIGHNY-26OCT11-T70", "yes") == pytest.approx(10 * 41 / 100)


# ---- lifecycle -----------------------------------------------------------------
def test_fills_are_journalled_as_executions_and_a_full_fill_closes():
    ex, j = Exch(), journal()
    om = OrderManager(ex, None, j, CFG)
    om.post(fop())
    ex.fill("o1", 4, 41)
    om.run_pass()
    ex.fill("o1", 10, 41)
    om.run_pass()
    legs = j.conn.execute("SELECT filled, avg_price_cents, state FROM leg_orders "
                          "WHERE filled > 0 ORDER BY id").fetchall()
    assert [r[0] for r in legs] == [4, 6] and all(r[1] == 41 for r in legs)
    assert om.total_resting_cost() == 0
    row = j.conn.execute("SELECT state, filled FROM resting_orders").fetchone()
    assert tuple(row) == ("FILLED", 10)


def test_an_unfilled_order_is_cancelled_after_max_rest_and_a_late_fill_is_kept():
    ex, clock = Exch(), Clock()
    om = OrderManager(ex, None, journal(), CFG, clock=clock)
    om.post(fop())
    clock.t += 899
    om.run_pass()
    assert not ex.cancelled
    ex.fill("o1", 3, 41)              # lands just before the cancel
    clock.t += 2
    om.run_pass()
    assert ex.cancelled == ["o1"] and om.stats["contracts_filled"] == 3


def test_near_the_close_or_when_the_engine_may_not_open_the_order_is_cancelled():
    ex = Exch()
    om = OrderManager(ex, None, None, CFG)
    om.post(fop(close_in_h=0.25))     # closes in 15 min < 30
    om.run_pass()
    assert ex.cancelled == ["o1"]
    ex2 = Exch()
    om2 = OrderManager(ex2, None, None, CFG, may_open=lambda op: (False, "breaker tripped"))
    om2.post(fop())
    om2.run_pass()
    assert ex2.cancelled == ["o1"]


def test_startup_cancels_only_the_bots_own_resting_orders():
    ex = Exch()

    class PF:
        def orders(self, status=None):
            return [{"order_id": "a", "client_order_id": COID_PREFIX + "x", "ticker": "T"},
                    {"order_id": "b", "client_order_id": "manual-123", "ticker": "T"},
                    {"order_id": "c", "client_order_id": "kb-taker", "ticker": "T"}]
    ex.orders.update({k: {"status": "resting"} for k in "abc"})
    om = OrderManager(ex, PF(), journal(), CFG)
    assert om.cancel_orphans() == 1 and ex.cancelled == ["a"]


# ---- exposure ------------------------------------------------------------------
def test_a_resting_order_counts_against_the_market_and_total_limits():
    from test_market_exposure import engine, op
    e = engine({"max_market_exposure_usd": 10.0, "max_total_exposure_usd": 12.0})
    e.order_manager = OrderManager(Exch(), None, None, CFG)
    e.order_manager.post(fop(leg("KXHIGHDEN-26SEP17-T82", qty=10, ask=45, bid=40)))
    ok, why = e._market_exposure_ok(op())                    # same market
    assert not ok and "resting" in why
    other = op("KXHIGHDEN-26SEP17-T90", contracts=10, price=50)
    ok, why = e._market_exposure_ok(other)
    assert ok
