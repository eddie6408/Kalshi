"""Volatility engine, market states, scanner/eligibility, scoring, fees, parsing."""
import math

from conftest import T0, feed, ramp, snap

from klvb.data.parsing import classify_sport, parse_market_meta, parse_market_snapshot, parse_orderbook
from klvb.engine.fees import FeeModel
from klvb.engine.scanner import build_candidates, eligibility, prefilter, select_watchlist
from klvb.engine.scoring import quality_score, volatility_component
from klvb.engine.states import classify
from klvb.engine.volatility import VolatilityEngine, realized_vol, slope_per_min
from klvb.models import MarketMeta


def test_velocity_is_cents_per_minute():
    pts = [(T0 + i * 2, 50 + 0.1 * i) for i in range(31)]  # +0.1c every 2s = +3c/min
    assert math.isclose(slope_per_min(pts), 3.0, rel_tol=1e-9)


def test_realized_vol_zero_for_flat_and_positive_for_moves():
    assert realized_vol([(T0 + i, 50.0) for i in range(10)]) == 0.0
    assert realized_vol([(T0 + i * 6, 50.0 + (i % 2)) for i in range(11)]) > 0


def test_features_high_low_velocity_acceleration(cfg):
    eng = VolatilityEngine(cfg.volatility)
    prices = [50.0] * 60 + ramp(50, 44, 30)  # flat then accelerating decline
    now = feed(eng, "M", prices)
    f = eng.features("M", now)
    assert f.recent_high == 50.0 and f.recent_low == 44.0
    assert f.velocity_short < -5
    assert f.acceleration < 0          # was flat, now falling
    assert f.spread == 1.0 and f.data_age == 0.0
    assert f.samples == 90


def test_features_never_use_future_observations(cfg):
    eng = VolatilityEngine(cfg.volatility)
    now = feed(eng, "M", [50.0] * 30 + [70.0] * 30)
    f_past = eng.features("M", T0 + 29 * 2)
    assert f_past.recent_high == 50.0 and f_past.price == 50.0
    assert eng.features("M", now).recent_high == 70.0


def test_volume_acceleration(cfg):
    eng = VolatilityEngine(cfg.volatility)
    now = feed(eng, "M", [50.0] * 150, vol_boost_from=140)
    f = eng.features("M", now)
    assert f.volume_accel > 1.5


def test_classifier_states_have_explanations(cfg):
    eng = VolatilityEngine(cfg.volatility)
    now = feed(eng, "M", [50.0] * 40 + ramp(50, 62, 40))
    f = eng.features("M", now)
    st = classify(f, cfg.volatility, cfg.eligibility, cfg.volatility.min_samples)
    assert st.trend in ("STRONG_UPTREND", "WEAK_UPTREND")
    assert "MOMENTUM" in st.states or "BREAKOUT" in st.states
    assert all(st.explanations.get(s) for s in st.states)


def test_classifier_blocks_stale_wide_thin(cfg):
    eng = VolatilityEngine(cfg.volatility)
    now = feed(eng, "M", [50.0] * 20, spread=6, size=5)
    f = eng.features("M", now + 60)  # 60s without data
    st = classify(f, cfg.volatility, cfg.eligibility, cfg.volatility.min_samples)
    assert {"STALE_DATA", "WIDE_SPREAD", "LOW_LIQUIDITY", "NO_TRADE"} <= set(st.states)
    assert not st.tradable


def test_eligibility_rules(cfg):
    eng = VolatilityEngine(cfg.volatility)
    now = feed(eng, "M", [50 + 4 * math.sin(i / 5) for i in range(200)])
    ok, why = eligibility(eng.features("M", now), cfg.eligibility, cfg.volatility)
    assert ok, why
    eng2 = VolatilityEngine(cfg.volatility)
    now2 = feed(eng2, "Q", [50.0] * 200, spread=5)
    ok2, why2 = eligibility(eng2.features("Q", now2), cfg.eligibility, cfg.volatility)
    assert not ok2
    joined = " ".join(why2)
    assert "spread" in joined and "volatility" in joined and "movement" in joined


def test_liquidity_filter(cfg):
    eng = VolatilityEngine(cfg.volatility)
    now = feed(eng, "M", [50 + 4 * math.sin(i / 5) for i in range(200)], size=10)
    ok, why = eligibility(eng.features("M", now), cfg.eligibility, cfg.volatility)
    assert not ok and any("liquidity" in w for w in why)


def test_scanner_prefilter_and_watchlist(cfg):
    good = MarketMeta(ticker="G", event_ticker="E", category="Sports", close_ts=T0 + 3 * 3600)
    s_good = snap("G", T0, 50)
    assert prefilter(good, s_good, T0, cfg.scanner, cfg.eligibility) == []
    closing = MarketMeta(ticker="C", category="Sports", close_ts=T0 + 60)
    assert any("until close" in w for w in prefilter(closing, snap("C", T0, 50), T0, cfg.scanner, cfg.eligibility))
    tail = MarketMeta(ticker="X", category="Sports", close_ts=T0 + 3600)
    assert any("band" in w for w in prefilter(tail, snap("X", T0, 98, spread=1), T0, cfg.scanner, cfg.eligibility))
    combo = MarketMeta(ticker="MV", category="Sports", close_ts=T0 + 3600, is_multivariate=True)
    assert prefilter(combo, snap("MV", T0, 50), T0, cfg.scanner, cfg.eligibility)
    cands, rej = build_candidates([(good, s_good), (closing, snap("C", T0, 50))], T0, cfg.scanner,
                                  cfg.eligibility, live_events={"E"})
    assert [c.meta.ticker for c in cands] == ["G"] and cands[0].live_context
    assert select_watchlist(cands, {"P"}, 1) == ["P", "G"][:max(1, 1)] or "P" in select_watchlist(cands, {"P"}, 1)


def test_quality_score_bounds():
    assert quality_score({"magnitude": 5, "edge": -3}) <= 100
    assert quality_score({"magnitude": 1, "velocity": 1, "confirmation": 1}) == 100.0
    assert quality_score({"magnitude": 0}) == 0.0
    assert volatility_component(10, 0.6, 1.5, 4.0) < volatility_component(2.0, 0.6, 1.5, 4.0)


def test_kalshi_fee_formula():
    fm = FeeModel()
    assert fm.fee(50, 1, True) == 2.0          # 0.07*0.25 = 1.75c -> 2c
    assert fm.fee(50, 100, True) == 175.0      # $1.75
    assert fm.fee(50, 10, True) == 18.0        # 17.5 -> 18
    assert fm.fee(10, 10, True) == 7.0         # 6.3 -> 7
    meta_maker = MarketMeta(ticker="A", fee_type="quadratic_with_maker_fees", fee_multiplier=1.0)
    meta_plain = MarketMeta(ticker="A", fee_type="quadratic", fee_multiplier=1.0)
    assert fm.fee(50, 10, False, meta_maker) == 5.0   # 4.375 -> 5
    assert fm.fee(50, 10, False, meta_plain) == 0.0
    assert fm.fee(50, 10, True, MarketMeta(ticker="A", fee_multiplier=0.5, fee_type="quadratic")) == 9.0
    waived = MarketMeta(ticker="A", fee_waiver_until=T0 + 100)
    assert fm.fee(50, 10, True, waived, at_ts=T0) == 0.0


def test_parse_fixed_point_and_legacy_payloads():
    m = {"ticker": "KXNBAGAME-X-A", "event_ticker": "KXNBAGAME-X", "yes_bid_dollars": "0.4300",
         "yes_ask_dollars": "0.4500", "last_price_dollars": "0.4400", "volume_fp": "1234.00",
         "volume_24h_fp": "999.00", "open_interest_fp": "50.00", "status": "active",
         "close_time": "2026-10-01T00:00:00Z", "expected_expiration_time": "2026-09-30T03:00:00Z"}
    s = parse_market_snapshot(m, T0)
    assert (s.yes_bid, s.yes_ask, s.last_price, s.volume) == (43.0, 45.0, 44.0, 1234.0)
    legacy = parse_market_snapshot({"ticker": "L", "yes_bid": 0, "yes_ask": 100, "volume": 5}, T0)
    assert legacy.yes_bid is None and legacy.yes_ask is None and legacy.volume == 5
    meta = parse_market_meta(m, {"KXNBAGAME": {"category": "Sports", "title": "Pro Basketball Game",
                                               "fee_type": "quadratic", "fee_multiplier": 1}}, classify_sport)
    assert meta.series_ticker == "KXNBAGAME" and meta.sport == "BASKETBALL"
    assert meta.effective_end_ts() < meta.close_ts


def test_parse_orderbook_bids_only():
    ob = parse_orderbook({"orderbook_fp": {"yes_dollars": [["0.4000", "10.00"], ["0.4200", "5.00"]],
                                           "no_dollars": [["0.5500", "7.00"]]}}, T0)
    assert ob.best_yes_bid().price == 42.0 and ob.best_yes_bid().size == 5.0
    assert ob.best_yes_ask().price == 45.0 and ob.best_yes_ask().size == 7.0
    assert ob.asks_for("no")[0].price == 58.0   # buying NO lifts YES bids


def test_sport_classifier_does_not_misfire():
    assert classify_sport("KXCPIINFL", "CPI inflation", [], "Will inflation exceed", "Economics") == "OTHER"
    assert classify_sport("KXATPMATCH", "ATP Tennis Match", [], "Sinner vs Alcaraz", "Sports") == "TENNIS"
    assert classify_sport("KXNFLGAME", "", [], "", "Sports") == "FOOTBALL"
