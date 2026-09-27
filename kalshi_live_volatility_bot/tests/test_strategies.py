"""Dip / momentum / reversal / downtrend detection, falling knives, chasing."""
from conftest import feed, make_ctx, ramp

from klvb.engine.volatility import VolatilityEngine
from klvb.models import Direction
from klvb.strategies import DipStrategy, MomentumStrategy


def _dip_path():
    # flat at 60, fall to 46 (-14c), stabilize, begin rebounding to 49
    return [60.0] * 60 + ramp(60, 46, 45) + [46.0] * 15 + ramp(46, 48, 12)


def test_dip_buys_after_stabilization_and_reversal(cfg, fees):
    eng = VolatilityEngine(cfg.volatility)
    now = feed(eng, "M", _dip_path(), vol_boost_from=100)
    sig = DipStrategy(cfg.strategy_params("DIP"), Direction.UP).evaluate(make_ctx(cfg, fees, eng, "M", now))
    assert sig is not None and sig.action == "BUY", sig and sig.reject_reasons
    assert sig.details["recent_high"] == 60.0 and sig.details["recent_low"] == 46.0
    assert sig.target > sig.entry_ref > sig.stop
    assert sig.expected_net >= cfg.costs.min_net_edge_cents
    assert 0 <= sig.score <= 100
    text = sig.explanation("TEST")
    assert "STRATEGY: DIP" in text and "REASON:" in text


def test_dip_refuses_falling_knife(cfg, fees):
    eng = VolatilityEngine(cfg.volatility)
    now = feed(eng, "M", [65.0] * 60 + ramp(65, 40, 60))  # still collapsing
    sig = DipStrategy(cfg.strategy_params("DIP"), Direction.UP).evaluate(make_ctx(cfg, fees, eng, "M", now))
    assert sig is not None and sig.action == "NO_TRADE"
    assert any("falling knife" in r or "no stabilization" in r for r in sig.reject_reasons)


def test_dip_refuses_when_already_recovered(cfg, fees):
    eng = VolatilityEngine(cfg.volatility)
    now = feed(eng, "M", [60.0] * 60 + ramp(60, 46, 45) + [46.0] * 15 + ramp(46, 57, 25))
    sig = DipStrategy(cfg.strategy_params("DIP"), Direction.UP).evaluate(make_ctx(cfg, fees, eng, "M", now))
    assert sig is None or sig.action == "NO_TRADE"


def test_small_moves_do_not_trigger(cfg, fees):
    eng = VolatilityEngine(cfg.volatility)
    now = feed(eng, "M", [50.0] * 60 + ramp(50, 48, 20) + [48.0] * 20)
    assert DipStrategy(cfg.strategy_params("DIP"), Direction.UP).evaluate(make_ctx(cfg, fees, eng, "M", now)) is None


def test_momentum_entry_requires_confirmation(cfg, fees):
    eng = VolatilityEngine(cfg.volatility)
    now = feed(eng, "M", [45.0] * 90 + ramp(45, 52, 40), vol_boost_from=90)
    sig = MomentumStrategy(cfg.strategy_params("MOMENTUM"), Direction.UP).evaluate(make_ctx(cfg, fees, eng, "M", now))
    assert sig is not None and sig.action == "BUY", sig and sig.reject_reasons
    assert sig.details["persistence"] >= 0.6


def test_momentum_single_tick_is_not_enough(cfg, fees):
    eng = VolatilityEngine(cfg.volatility)
    now = feed(eng, "M", [45.0] * 120 + [51.0])
    sig = MomentumStrategy(cfg.strategy_params("MOMENTUM"), Direction.UP).evaluate(make_ctx(cfg, fees, eng, "M", now))
    assert sig is None or sig.action == "NO_TRADE"


def test_momentum_does_not_chase_exhausted_move(cfg, fees):
    eng = VolatilityEngine(cfg.volatility)
    now = feed(eng, "M", [45.0] * 40 + ramp(45, 66, 80), vol_boost_from=40)
    sig = MomentumStrategy(cfg.strategy_params("MOMENTUM"), Direction.UP).evaluate(make_ctx(cfg, fees, eng, "M", now))
    assert sig is not None and sig.action == "NO_TRADE"
    assert any("exhausted" in r for r in sig.reject_reasons)


def test_momentum_needs_volume(cfg, fees):
    eng = VolatilityEngine(cfg.volatility)
    now = feed(eng, "M", [45.0] * 90 + ramp(45, 52, 40))  # flat volume
    sig = MomentumStrategy(cfg.strategy_params("MOMENTUM"), Direction.UP).evaluate(make_ctx(cfg, fees, eng, "M", now))
    assert sig is not None and any("volume" in r for r in sig.reject_reasons)


def test_reversal_buys_no_after_yes_spike_rolls_over(cfg, fees):
    eng = VolatilityEngine(cfg.volatility)
    # YES spikes 40 -> 54 then rolls over to 51 = NO dips 60 -> 46 then rebounds to 49
    now = feed(eng, "M", [40.0] * 60 + ramp(40, 54, 45) + [54.0] * 15 + ramp(54, 52, 12), vol_boost_from=100)
    sig = DipStrategy(cfg.strategy_params("REVERSAL"), Direction.DOWN).evaluate(make_ctx(cfg, fees, eng, "M", now))
    assert sig is not None and sig.strategy == "REVERSAL" and sig.side == "no"
    assert sig.action == "BUY", sig.reject_reasons
    # entry is the NO ask = 100 - YES bid
    assert sig.entry_ref == 100 - eng.features("M", now).bid


def test_downtrend_buys_no_on_persistent_decline(cfg, fees):
    eng = VolatilityEngine(cfg.volatility)
    now = feed(eng, "M", [55.0] * 90 + ramp(55, 48, 40), vol_boost_from=90)
    sig = MomentumStrategy(cfg.strategy_params("DOWNTREND"), Direction.DOWN).evaluate(make_ctx(cfg, fees, eng, "M", now))
    assert sig is not None and sig.strategy == "DOWNTREND" and sig.side == "no"
    assert sig.action == "BUY", sig.reject_reasons


def test_insufficient_edge_is_rejected(cfg, fees):
    eng = VolatilityEngine(cfg.volatility)
    now = feed(eng, "M", _dip_path(), vol_boost_from=100, spread=3)
    params = cfg.strategy_params("DIP") | {"target_retrace_fraction": 0.2}
    sig = DipStrategy(params, Direction.UP).evaluate(make_ctx(cfg, fees, eng, "M", now))
    assert sig is not None and sig.action == "NO_TRADE"
    assert any("edge" in r for r in sig.reject_reasons)
