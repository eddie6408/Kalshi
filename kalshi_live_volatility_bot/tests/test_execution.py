"""Paper fill model (full, partial, missed, rejected, delayed, maker), slippage, payloads."""
from types import SimpleNamespace

import pytest

from conftest import T0, snap


def q(ts, bid, size=500.0, book=True, status="active"):
    """Whole-cent quote: YES bid = `bid`, YES ask = bid + 1."""
    return snap("M", ts, bid + 0.5, spread=1.0, size=size, book=book, status=status)

from klvb.execution.base import build_order_payload, validate_payload
from klvb.execution.paper import PaperExecution
from klvb.models import BookLevel, MarketMeta, Order, OrderBook, OrderStatus, TradePrint


def paper(cfg, fees, **over):
    ex = PaperExecution(cfg.paper, fees, seed=7)
    ex.cfg = SimpleNamespace(**{**cfg.paper.as_dict(), "reject_probability": 0.0, **over})
    return ex


def buy(count=10, limit=51.0, side="yes", style="taker"):
    return Order(env="PAPER", ticker="M", side=side, action="buy", count=count, limit_price=limit, purpose="ENTRY",
                 style=style, ref_price=50.0)


META = MarketMeta(ticker="M", fee_type="quadratic", fee_multiplier=1.0)


async def test_taker_waits_for_latency_then_fills_at_later_quote(cfg, fees):
    ex = paper(cfg, fees)
    o = buy()
    await ex.submit(o, META, T0)
    assert o.status is OrderStatus.SUBMITTED
    # a quote from BEFORE the modeled latency must not fill the order
    assert await ex.on_market("M", q(T0 + 0.01, 50), None, [], T0 + 0.01) == []
    evs = await ex.on_market("M", q(T0 + 2, 50), None, [], T0 + 2)
    assert evs and o.status is OrderStatus.FILLED and o.filled == 10
    f = evs[0].fills[0]
    assert f.price == 51.0 and f.is_taker and f.ts >= T0 + 0.02   # delayed fill, at the ask
    assert f.fee == fees.fee(51.0, 10, True, META)
    assert f.slippage == 1.0


async def test_partial_fill_walks_depth_and_cancels_remainder(cfg, fees):
    ex = paper(cfg, fees, depth_participation=1.0)
    o = buy(count=30, limit=52.0)
    await ex.submit(o, META, T0)
    s = q(T0 + 2, 50, size=10)   # asks 51 x10, 52 x10, 53 x10
    evs = await ex.on_market("M", s, s.book, [], T0 + 2)
    assert o.filled == 20 and o.status is OrderStatus.CANCELED and "partial" in o.reject_reason
    assert [fl.price for fl in evs[0].fills] == [51.0, 52.0]
    assert o.avg_fill_price == pytest.approx(51.5)


async def test_missed_fill_when_price_runs_away(cfg, fees):
    ex = paper(cfg, fees)
    o = buy(limit=51.0)
    await ex.submit(o, META, T0)
    evs = await ex.on_market("M", q(T0 + 2, 56), None, [], T0 + 2)
    assert o.status is OrderStatus.MISSED and o.filled == 0
    assert "beyond limit" in o.reject_reason and evs[0].fills == []


async def test_depth_participation_limits_size(cfg, fees):
    ex = paper(cfg, fees, depth_participation=0.5)
    o = buy(count=100, limit=51.0)
    await ex.submit(o, META, T0)
    s = q(T0 + 2, 50, size=40, book=False)
    await ex.on_market("M", s, None, [], T0 + 2)
    assert o.filled == 20


async def test_rejects_invalid_and_closed_and_random(cfg, fees):
    ex = paper(cfg, fees)
    bad = buy(count=0)
    await ex.submit(bad, META, T0)
    assert bad.status is OrderStatus.REJECTED
    ex.last_snap["M"] = q(T0, 50, status="closed")
    o = buy()
    await ex.submit(o, META, T0)
    assert o.status is OrderStatus.REJECTED and "not open" in o.reject_reason
    ex2 = paper(cfg, fees, reject_probability=1.0)
    o2 = buy()
    await ex2.submit(o2, META, T0)
    assert o2.status is OrderStatus.REJECTED


async def test_buy_no_uses_yes_bids(cfg, fees):
    ex = paper(cfg, fees, depth_participation=1.0)
    o = buy(side="no", limit=51.0)  # NO ask = 100 - YES bid(50) = 50
    await ex.submit(o, META, T0)
    s = q(T0 + 2, 50)
    evs = await ex.on_market("M", s, s.book, [], T0 + 2)
    assert o.status is OrderStatus.FILLED and evs[0].fills[0].price == 50.0


async def test_maker_post_only_rejects_crossing_and_fills_on_trade_through(cfg, fees):
    ex = paper(cfg, fees, maker_queue_fraction=0.5)
    ex.last_snap["M"] = q(T0, 50)
    crossing = Order(env="PAPER", ticker="M", side="yes", action="sell", count=10, limit_price=50.0,
                     purpose="TAKE_PROFIT", style="maker")
    await ex.submit(crossing, META, T0)
    assert crossing.status is OrderStatus.REJECTED
    tp = Order(env="PAPER", ticker="M", side="yes", action="sell", count=10, limit_price=55.0,
               purpose="TAKE_PROFIT", style="maker", expires_ts=T0 + 100)
    await ex.submit(tp, META, T0)
    assert tp.status is OrderStatus.RESTING
    # trade AT our price: only the queue share counts
    await ex.on_market("M", q(T0 + 5, 52), None, [TradePrint("M", T0 + 5, 55.0, 8)], T0 + 5)
    assert tp.filled == 4 and tp.status is OrderStatus.PARTIALLY_FILLED
    # trade THROUGH our price fills the rest; maker fee (0 for plain quadratic series)
    evs = await ex.on_market("M", q(T0 + 6, 53), None, [TradePrint("M", T0 + 6, 56.0, 20)], T0 + 6)
    assert tp.status is OrderStatus.FILLED and tp.filled == 10
    assert all(not f.is_taker and f.fee == 0 for f in evs[0].fills)


async def test_maker_fills_when_book_crosses_and_expires_otherwise(cfg, fees):
    ex = paper(cfg, fees, depth_participation=1.0)
    ex.last_snap["M"] = q(T0, 50)
    tp = Order(env="PAPER", ticker="M", side="yes", action="sell", count=10, limit_price=55.0,
               purpose="TAKE_PROFIT", style="maker", expires_ts=T0 + 100)
    await ex.submit(tp, META, T0)
    await ex.on_market("M", q(T0 + 5, 56), None, [], T0 + 5)   # bid 56 >= our 55 offer
    assert tp.status is OrderStatus.FILLED and tp.avg_fill_price == 55.0
    stale = Order(env="PAPER", ticker="M", side="yes", action="buy", count=5, limit_price=40.0, purpose="ENTRY",
                  style="maker", expires_ts=T0 + 10)
    await ex.submit(stale, META, T0)
    await ex.on_market("M", q(T0 + 20, 50), None, [], T0 + 20)
    assert stale.status is OrderStatus.MISSED and "expired" in stale.reject_reason


def test_payload_matches_kalshi_schema():
    o = buy(limit=51.0)
    body = build_order_payload(o)
    assert body["yes_price_dollars"] == "0.5100" and body["time_in_force"] == "immediate_or_cancel"
    assert body["type"] == "limit" and body["client_order_id"] == o.client_order_id
    assert validate_payload(body) == []
    sell = Order(env="LIVE", ticker="M", side="no", action="sell", count=3, limit_price=120, purpose="EXIT")
    b2 = build_order_payload(sell)
    assert b2["reduce_only"] is True and b2["no_price_dollars"] == "0.9900"   # clamped
    tp = Order(env="LIVE", ticker="M", side="yes", action="sell", count=3, limit_price=60, purpose="TAKE_PROFIT",
               style="maker", expires_ts=T0 + 30)
    b3 = build_order_payload(tp)
    assert b3["post_only"] is True and "time_in_force" not in b3 and b3["expiration_ts"] == int(T0 + 30)
    assert validate_payload({"side": "maybe"})
