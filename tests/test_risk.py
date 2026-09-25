import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.fees import FeeModel
from kalshibot.models import Market, Orderbook, OrderbookLevel, Position, TradeIntent
from kalshibot.risk import HaltReason, RiskManager, RiskState

CFG = {
    "risk": {
        "bankroll_usd": 1000.0,
        "max_cost_per_trade_usd": 50.0,
        "max_exposure_per_market_usd": 250.0,
        "max_total_open_exposure_usd": 1000.0,
        "max_fraction_of_bankroll_at_risk": 0.02,
        "max_open_positions": 10,
        "max_orders_per_hour": 20,
        "kelly_fraction": 0.25,
        "allow_averaging_down": False,
    },
    "edge": {
        "min_edge_after_fees": 0.05,
        "min_confidence": 0.60,
        "min_price_cents": 5,
        "max_price_cents": 95,
        "min_resting_contracts_at_our_price": 25,
        "max_spread_cents": 4,
        "min_minutes_to_close": 45,
        "max_hours_to_close": 60,
    },
    "safety": {
        "min_account_balance_usd": 50.0,
        "daily_loss_limit_usd": 40.0,
        "max_consecutive_losses": 5,
    },
}


def rm():
    return RiskManager(CFG, FeeModel({"safety_multiplier": 1.0}))


def deep_book(n=500):
    # Resting NO bids at 30c == YES offered at 70c
    return Orderbook("T", yes=[OrderbookLevel(30, n)], no=[OrderbookLevel(30, n)])


def state(**kw):
    base = dict(balance_usd=1000.0, positions={}, open_exposure_usd=0.0)
    base.update(kw)
    return RiskState(**base)


def intent(price=70, prob=0.85, conf=0.8, side="yes", ticker="T"):
    return TradeIntent(ticker=ticker, side=side, action="buy", limit_price_cents=price,
                       fair_probability=prob, confidence=conf, edge_after_fees=prob - price / 100)


# ---------------- circuit breakers ----------------
def test_halts_below_minimum_balance():
    with pytest.raises(HaltReason):
        rm().check_halt_conditions(state(balance_usd=10.0))


def test_halts_on_daily_loss_limit():
    with pytest.raises(HaltReason):
        rm().check_halt_conditions(state(realized_pnl_today_usd=-45.0))


def test_halts_on_consecutive_losses():
    with pytest.raises(HaltReason):
        rm().check_halt_conditions(state(consecutive_losses=5))


def test_healthy_state_does_not_halt():
    rm().check_halt_conditions(state())


# ---------------- market gates ----------------
def mk(**kw):
    d = dict(ticker="T", status="open", yes_bid=68, yes_ask=70, no_bid=30, no_ask=32,
             close_time=datetime.now(timezone.utc) + timedelta(hours=6))
    d.update(kw)
    return Market(**d)


def test_rejects_wide_spread():
    ok, why = rm().market_is_tradable(mk(yes_bid=50, yes_ask=70), None)
    assert not ok and "spread" in why


def test_rejects_imminent_close():
    ok, why = rm().market_is_tradable(
        mk(close_time=datetime.now(timezone.utc) + timedelta(minutes=10)), None)
    assert not ok and "closes in" in why


def test_rejects_far_future():
    ok, why = rm().market_is_tradable(
        mk(close_time=datetime.now(timezone.utc) + timedelta(days=10)), None)
    assert not ok


def test_rejects_extreme_price():
    ok, _ = rm().market_is_tradable(mk(yes_bid=97, yes_ask=99), None)
    assert not ok


def test_accepts_a_good_market():
    ok, why = rm().market_is_tradable(mk(), None)
    assert ok, why


# ---------------- sizing ----------------
def test_rejects_thin_edge():
    d = rm().size_order(intent(price=70, prob=0.73), state(), deep_book())
    assert not d.approved and "edge" in d.reasons[0]


def test_rejects_low_confidence():
    d = rm().size_order(intent(conf=0.30), state(), deep_book())
    assert not d.approved and "confidence" in d.reasons[0]


def test_per_trade_cap_binds():
    # q=.85 at 70c -> kelly f*=0.50; quarter-kelly * confidence 0.8 -> 0.10 of
    # bankroll = $100 = 142 contracts. Loosen the 2% bankroll rule so the
    # $50-per-trade cap is the binding constraint: $50 / 0.70 = 71 contracts.
    cfg = {**CFG, "risk": {**CFG["risk"], "max_fraction_of_bankroll_at_risk": 0.50}}
    m = RiskManager(cfg, FeeModel({"safety_multiplier": 1.0}))
    d = m.size_order(intent(), state(), deep_book())
    assert d.approved
    assert d.order.contracts == 71
    assert d.order.est_cost_usd <= 50.0


def test_bankroll_fraction_cap_binds():
    cfg = {**CFG, "risk": {**CFG["risk"], "max_cost_per_trade_usd": 500.0}}
    m = RiskManager(cfg, FeeModel({"safety_multiplier": 1.0}))
    d = m.size_order(intent(), state(), deep_book())
    # 2% of $1000 = $20 -> 28 contracts at 70c
    assert d.approved and d.order.contracts == 28


def test_thin_liquidity_blocks_the_trade():
    thin = Orderbook("T", yes=[OrderbookLevel(30, 5)], no=[OrderbookLevel(30, 5)])
    d = rm().size_order(intent(), state(), thin)
    assert not d.approved and any("offered" in r for r in d.reasons)


def test_sizes_down_to_available_depth():
    # Sizing wants 28 contracts (2% bankroll cap); only 26 are on offer.
    book = Orderbook("T", yes=[OrderbookLevel(30, 26)], no=[OrderbookLevel(30, 26)])
    d = rm().size_order(intent(), state(), book)
    assert d.approved and d.order.contracts == 26
    assert any("sized down" in r for r in d.order.notes)


def test_default_config_sizes_to_two_percent_of_bankroll():
    d = rm().size_order(intent(), state(), deep_book())
    assert d.approved and d.order.contracts == 28      # $20 / $0.70
    assert d.order.est_cost_usd <= 0.02 * CFG["risk"]["bankroll_usd"]


def test_blocks_averaging_down():
    pos = Position(ticker="T", contracts=50, total_cost_cents=50 * 80)   # avg 80c
    d = rm().size_order(intent(price=70), state(positions={"T": pos}), deep_book())
    assert not d.approved and "average down" in " ".join(d.reasons)


def test_blocks_opposite_side():
    pos = Position(ticker="T", contracts=-50, total_cost_cents=50 * 30)
    d = rm().size_order(intent(side="yes"), state(positions={"T": pos}), deep_book())
    assert not d.approved and "opposite side" in " ".join(d.reasons)


def test_per_market_exposure_cap():
    pos = Position(ticker="T", contracts=340, total_cost_cents=24000)  # $240 already
    st = state(positions={"T": pos}, open_exposure_usd=240.0)
    cfg = {**CFG, "risk": {**CFG["risk"], "allow_averaging_down": True}}
    m = RiskManager(cfg, FeeModel({"safety_multiplier": 1.0}))
    d = m.size_order(intent(price=70), st, deep_book())
    # only $10 of room left in that market -> 14 contracts
    assert d.approved and d.order.contracts == 14


def test_hourly_order_limit():
    now = datetime.now(timezone.utc)
    st = state(order_timestamps=[now - timedelta(minutes=i) for i in range(20)])
    d = rm().size_order(intent(), st, deep_book())
    assert not d.approved and "orders in the last hour" in d.reasons[0]


def test_max_open_positions():
    st = state(positions={f"M{i}": Position(f"M{i}", contracts=1) for i in range(10)})
    d = rm().size_order(intent(ticker="NEW"), st, deep_book())
    assert not d.approved and "positions" in d.reasons[0]


def test_cash_cap_respects_minimum_balance():
    st = state(balance_usd=60.0)   # only $10 usable above the $50 floor
    d = rm().size_order(intent(), st, deep_book())
    assert d.approved and d.order.contracts == 14


def test_approved_order_has_positive_ev():
    d = rm().size_order(intent(), state(), deep_book())
    assert d.approved and d.order.est_ev_usd > 0
