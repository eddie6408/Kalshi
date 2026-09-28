"""Package C2, phase M0: simulated maker orders on the production tape.

Pinned: the simulated price never crosses and never gives the edge away;
fills are only credited for prints a resting order would really have met
(queue first, taker on the other side, inside its lifetime); the lifecycle,
drift and settlement markouts are recorded; a restart never double counts;
and nothing in the module can send an order.
"""
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from kalshibot.depth import DepthBook, Level
from kalshibot.fees import FeeModel
from kalshibot.journal import Journal
from kalshibot.maker_shadow import (EXPIRED, FILLED, INTERRUPTED, OPEN, PARTIAL,
                                    MakerShadow, MakerShadowConfig, apply_trades,
                                    choose, price_order)
from kalshibot.models import Market
from kalshibot.prodwatch import Quote, Trade

FEES = FeeModel({"safety_multiplier": 1.0})
T0 = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc).timestamp()
CFG = MakerShadowConfig()


def bk(yes_bid=40, yes_ask=46, qty=20):
    """YES bid at yes_bid; YES ask at yes_ask (= a NO bid at 100 - yes_ask)."""
    return DepthBook("W-1", [Level(yes_bid, qty)], [Level(100 - yes_ask, qty)])


# ------------------------------------------------------------------ pricing
def test_it_improves_the_bid_by_a_cent_when_the_edge_allows():
    p = price_order(0.60, bk(), "yes", CFG, FEES)
    assert p["price_cents"] == 41 and p["queue_ahead"] == 0
    assert (p["best_bid"], p["best_ask"]) == (40, 46)


def test_it_never_crosses_the_spread():
    for q in (0.9, 0.99):
        p = price_order(q, bk(yes_bid=44, yes_ask=45), "yes", CFG, FEES)
        assert p["price_cents"] <= 44            # ask - 1, never the ask


def test_it_never_posts_a_price_that_gives_the_edge_away():
    # fair 45c: best we may pay is 45 - 3 edge - fee -> 41, below bid+1
    p = price_order(0.45, bk(), "yes", CFG, FEES)
    assert p["price_cents"] <= 45 - CFG.min_edge_cents - p["maker_fee_cents"]
    # joining behind the displayed 40c queue records the queue ahead
    p = price_order(0.44, bk(), "yes", CFG, FEES)
    assert p["price_cents"] == 40 and p["queue_ahead"] == 20
    # further down it rests behind the bid (allowed; it may still fill)
    p = price_order(0.40, bk(), "yes", CFG, FEES)
    assert p["price_cents"] == 36 and p["queue_ahead"] == 0
    # and with no price that leaves the edge, no order at all
    assert price_order(0.03, bk(), "yes", CFG, FEES) is None


def test_it_needs_a_two_sided_book():
    one_sided = DepthBook("W-1", [Level(40, 5)], [])
    assert price_order(0.9, one_sided, "yes", CFG, FEES) is None


def test_it_picks_the_side_our_probability_favours():
    assert choose(0.70, bk(), CFG, FEES)["side"] == "yes"
    no = choose(0.20, bk(), CFG, FEES)
    assert no["side"] == "no" and no["best_bid"] == 54 and no["best_ask"] == 60
    assert choose(0.9, DepthBook("W-1", [Level(40, 5)], []), CFG, FEES) is None


# ------------------------------------------------------------------ fills
def order(side="yes", price=41, contracts=10, queue=0, created=T0, expires=T0 + 900):
    return {"side": side, "price_cents": price, "contracts": contracts,
            "queue_left": queue, "created_at": created, "expires_at": expires,
            "filled": 0, "first_fill_at": None, "full_fill_at": None}


def tr(ts, yes, n=5, taker="no"):
    return Trade(f"{ts}-{yes}-{n}", ts, yes, 100 - yes, n, taker)


def test_a_print_through_our_price_fills_us():
    o = order()
    assert apply_trades(o, [tr(T0 + 60, 39, n=4)])
    assert (o["filled"], o["fill_kind"], o["first_fill_at"]) == (4, "through", T0 + 60)
    apply_trades(o, [tr(T0 + 70, 38, n=50)])
    assert o["filled"] == 10 and o["full_fill_at"] == T0 + 70


def test_prints_at_our_price_eat_the_queue_ahead_first():
    o = order(price=40, queue=20)
    apply_trades(o, [tr(T0 + 10, 40, n=15)])
    assert o["filled"] == 0 and o["queue_left"] == 5
    apply_trades(o, [tr(T0 + 20, 40, n=8)])
    assert o["filled"] == 3 and o["fill_kind"] == "queue"


def test_prints_that_could_not_have_met_us_are_ignored():
    o = order()
    apply_trades(o, [
        tr(T0 + 10, 39, taker="yes"),     # a taker BUYING yes lifts offers
        tr(T0 + 20, 42),                  # hit a better bid than ours
        tr(T0 - 5, 30),                   # before we were posted
        tr(T0 + 901, 30),                 # after we expired
    ])
    assert o["filled"] == 0


def test_a_no_order_fills_against_yes_buyers():
    o = order(side="no", price=55)
    apply_trades(o, [tr(T0 + 5, 46, n=3, taker="yes")])      # no price 54 < 55
    assert o["filled"] == 3
    apply_trades(o, [tr(T0 + 6, 44, n=3, taker="no")])       # wrong taker
    assert o["filled"] == 3


# ------------------------------------------------------------ the lifecycle
class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class Watch:
    def __init__(self):
        self.mids = {}

    def mid_at(self, ticker, ts, side="yes", tolerance=180.0):
        return self.mids.get((ticker, round(ts), side))


def info(q=0.60, at=T0, lead=3.0, close_ts=None, stage="MARKET_SKILL"):
    return {"q": q, "at": at, "lead": lead, "close_ts": close_ts, "stage": stage,
            "reason": "market forecasts better here", "series": "KXHIGHNY"}


def shadow(tmp, clock, watch=None):
    j = Journal(Path(tmp) / "j.db")
    return MakerShadow(j, FEES, MakerShadowConfig(), watch=watch, clock=clock,
                       versions={"code_version": "abc", "config_version": "cfg-1"}), j


def quote(b=None, at=T0):
    b = b or bk()
    return Quote.of("W-1", b, at)


def test_one_simulated_order_per_market_is_posted_and_journalled():
    with tempfile.TemporaryDirectory() as tmp:
        clock = Clock()
        ms, j = shadow(tmp, clock)
        ms.on_book("W-1", quote(), info())
        ms.on_book("W-1", quote(), info())          # already resting
        rows = j.maker_shadow_rows()
        assert len(rows) == 1
        r = rows[0]
        assert (r["side"], r["price_cents"], r["state"], r["stage"]) == \
            ("yes", 41, OPEN, "MARKET_SKILL")
        assert r["expires_at"] == T0 + 900 and r["code_version"] == "abc"


def test_nothing_is_posted_without_a_fresh_usable_verdict():
    with tempfile.TemporaryDirectory() as tmp:
        clock = Clock()
        ms, j = shadow(tmp, clock)
        ms.on_book("W-1", quote(), {})                               # no verdict
        ms.on_book("W-1", quote(), info(q=None))                     # no probability
        ms.on_book("W-1", quote(), info(at=T0 - 600))                # stale verdict
        ms.on_book("W-1", quote(), info(lead=30))                    # too far out
        ms.on_book("W-1", quote(), info(close_ts=T0 + 20 * 60))      # closing soon
        assert j.maker_shadow_rows() == []


def test_an_order_expires_early_before_the_market_closes():
    with tempfile.TemporaryDirectory() as tmp:
        ms, j = shadow(tmp, Clock())
        ms.on_book("W-1", quote(), info(close_ts=T0 + 40 * 60))
        assert j.maker_shadow_rows()[0]["expires_at"] == T0 + 10 * 60


def test_fill_expiry_drift_and_settlement():
    with tempfile.TemporaryDirectory() as tmp:
        clock = Clock()
        watch = Watch()
        ms, j = shadow(tmp, clock, watch)
        ms.on_book("W-1", quote(), info())
        # a full fill through our price two minutes in
        clock.t = T0 + 300
        ms.on_trades("W-1", [tr(T0 + 120, 39, n=10)], T0 + 295)
        r = j.maker_shadow_rows()[0]
        assert (r["state"], r["filled"], r["fill_kind"]) == (FILLED, 10, "through")
        assert "W-1" not in ms.open
        # drift: 5 and 30 minutes after the fill
        watch.mids[("W-1", round(T0 + 120 + 300), "yes")] = 38.0
        watch.mids[("W-1", round(T0 + 120 + 1800), "yes")] = 45.0
        clock.t = T0 + 120 + 1800 + 1
        ms._drift(clock.t)
        r = j.maker_shadow_rows()[0]
        assert (r["drift_5m"], r["drift_30m"], r["drift_60m"]) == (-3.0, 4.0, None)
        # settlement: YES paid
        m = Market(ticker="W-1", event_ticker="E", series_ticker="KXHIGHNY",
                   status="settled", raw={"status": "settled", "result": "yes"})

        class Client:
            def get_market(self, t):
                return m
        stats = ms.resolve(Client(), now=T0 + 86400)
        r = j.maker_shadow_rows()[0]
        assert stats["settled"] == 1 and r["settled_yes"] == 1
        assert r["maker_markout_cents"] == pytest.approx(100 - 41 - r["maker_fee_cents"])
        assert r["taker_markout_cents"] == pytest.approx(100 - 46 - r["taker_fee_cents"])


def test_an_unfilled_order_expires_only_once_the_tape_is_read_past_it():
    with tempfile.TemporaryDirectory() as tmp:
        clock = Clock()
        ms, j = shadow(tmp, clock)
        ms.on_book("W-1", quote(), info())
        ms.on_trades("W-1", [], T0 + 600)                 # tape read to 10 min
        assert j.maker_shadow_rows()[0]["state"] == OPEN
        ms.on_trades("W-1", [tr(T0 + 700, 39, n=2)], T0 + 950)
        r = j.maker_shadow_rows()[0]
        assert (r["state"], r["filled"], r["closed_at"]) == (PARTIAL, 2, T0 + 900)
        ms.on_book("W-1", quote(), info(at=clock.t))      # free to post again
        assert len(j.maker_shadow_rows()) == 2


def test_an_order_with_no_fill_at_all_is_expired():
    with tempfile.TemporaryDirectory() as tmp:
        ms, j = shadow(tmp, Clock())
        ms.on_book("W-1", quote(), info())
        ms.on_trades("W-1", [], T0 + 1000)
        assert j.maker_shadow_rows()[0]["state"] == EXPIRED


def test_a_restart_closes_resting_orders_instead_of_double_counting():
    with tempfile.TemporaryDirectory() as tmp:
        clock = Clock()
        ms, j = shadow(tmp, clock)
        ms.on_book("W-1", quote(), info())
        again = MakerShadow(j, FEES, MakerShadowConfig(), clock=clock)
        again.load()
        assert j.maker_shadow_rows()[0]["state"] == INTERRUPTED
        assert again.open == {}

        class Client:
            def get_market(self, t):
                raise AssertionError("interrupted orders are never settled")
        assert again.resolve(Client(), now=T0 + 86400)["settled"] == 0


def test_the_module_cannot_send_an_order():
    src = (Path(__file__).resolve().parents[1] / "kalshibot" / "maker_shadow.py").read_text()
    for word in ("place_order", "place_demo_order", "create_order", "cancel_order",
                 "DemoExecutionClient", "requests."):
        assert word not in src
