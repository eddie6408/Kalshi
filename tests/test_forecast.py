"""Forecast-derived edges.

A structural trade is guaranteed by the contracts. A forecast trade is an
opinion, and these tests exist to prove the bot treats it as one: never
labelled guaranteed, sized small, refused when the opinion is weak, and
refused when the market has already priced it.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.depth import DepthBook, Level
from kalshibot.estimators.base import Estimate
from kalshibot.fees import FeeModel
from kalshibot.gate import ProfitabilityGate, Reject
from kalshibot.models import Market
from kalshibot.opportunity import ExecutionRisk
from kalshibot.strategies.forecast import ForecastConfig, ForecastStrategy

FEES = FeeModel({"safety_multiplier": 1.0})
SOON = datetime.now(timezone.utc) + timedelta(hours=6)


class FakeEstimator:
    """Returns a fixed probability. Knows nothing about money - by design."""
    name = "fake"

    def __init__(self, probability=0.72, confidence=0.9, skip=None):
        self.probability, self.confidence, self.skip_reason = (
            probability, confidence, skip)

    def candidate_series(self):
        return ["KXFAKE"]

    def estimate(self, market):
        if self.skip_reason:
            return Estimate.skip(self.skip_reason, self.name)
        return Estimate(self.probability, self.confidence, self.name,
                        {"note": "test"})


def mkt(ticker="KXFAKE-1", event="EV"):
    return Market(ticker=ticker, event_ticker=event, series_ticker="KXFAKE",
                  status="open", close_time=SOON, subtitle="a thing happens")


def book(yes_ask=61, qty=500):
    """YES offered at yes_ask == a NO bid at 100-yes_ask."""
    return DepthBook("KXFAKE-1",
                     yes_bids=[Level(yes_ask - 2, qty)],
                     no_bids=[Level(100 - yes_ask, qty)])


def cfg(**kw):
    base = dict(kelly_fraction=0.25, max_contracts_per_trade=25,
                max_bankroll_fraction=0.02, min_probability_edge=0.05,
                min_confidence=0.50, min_book_depth=10)
    base.update(kw)
    return ForecastConfig.from_dict(base)


def find(estimator=None, bk=500.0, b=None, c=None):
    s = ForecastStrategy(estimator or FakeEstimator(), FEES, c or cfg())
    return s, s.find([mkt()], {"KXFAKE-1": b or book()}, bk)


# ===================== the worked example from the brief =====================
def test_the_brief_example_produces_a_trade():
    """Claude estimates 72%, Kalshi YES price 61c -> an 11-point discrepancy."""
    s, ops = find()
    assert len(ops) == 1, s.skips
    op = ops[0]
    assert op.legs[0].side == "yes"
    assert round(op.probability_yes, 2) == 0.72
    assert 0.60 <= op.implied_probability <= 0.62
    assert round(abs(op.probability_edge) * 100) == 11
    assert op.ev_usd > 0


def test_a_forecast_is_never_labelled_guaranteed():
    """The single most important property in this file."""
    _, ops = find()
    assert ops[0].guaranteed is False
    assert ops[0].guaranteed_min_payout == 0.0
    assert "forecast" in ops[0].strategy_type


def test_it_buys_no_when_the_market_is_too_high():
    """Our 48% against a 61c YES price means NO is the cheap side: we think
    NO pays 52% of the time and it costs 39c."""
    s, ops = find(FakeEstimator(probability=0.48))
    assert len(ops) == 1, s.skips
    assert ops[0].legs[0].side == "no"
    assert ops[0].ev_usd > 0


# ===================== the cases that must NOT trade =====================
def test_no_trade_when_the_market_already_agrees():
    s, ops = find(FakeEstimator(probability=0.62))
    assert ops == []
    assert any(sk.reason == "edge too small" for sk in s.skips), s.skips


def test_no_trade_when_confidence_is_low():
    s, ops = find(FakeEstimator(probability=0.72, confidence=0.20))
    assert ops == []
    assert any(sk.reason == "low confidence" for sk in s.skips), s.skips


def test_no_trade_when_the_estimator_declines():
    s, ops = find(FakeEstimator(skip="no forecast data for this city"))
    assert ops == []
    assert any(sk.reason == "estimator declined" for sk in s.skips), s.skips
    assert "no forecast data" in s.skips[0].detail


def test_no_trade_when_the_book_is_too_thin_to_exit():
    s, ops = find(b=book(qty=3))
    assert ops == []
    assert any(sk.reason == "book too thin to exit" for sk in s.skips), s.skips


def test_no_trade_when_there_is_no_book_at_all():
    s = ForecastStrategy(FakeEstimator(), FEES, cfg())
    assert s.find([mkt()], {"KXFAKE-1": DepthBook.empty("KXFAKE-1")}, 500.0) == []


def test_a_tiny_edge_that_fees_eat_is_refused():
    """6 points of edge on a 1c-a-side market is real before fees and gone
    after them. The bot must say so rather than trade it."""
    s, ops = find(FakeEstimator(probability=0.55),
                  c=cfg(min_probability_edge=0.01))
    for op in ops:
        assert op.ev_usd > 0, "anything returned must still have positive EV"


# ===================== sizing =====================
def test_size_is_capped_by_the_bankroll_fraction():
    """2% of a $500 bankroll at 61c is 16 contracts, not 25."""
    _, ops = find(bk=500.0)
    assert ops[0].quantity <= 16


def test_size_is_capped_by_the_hard_per_trade_limit():
    _, ops = find(bk=100_000.0, c=cfg(max_contracts_per_trade=25,
                                      max_bankroll_fraction=1.0))
    assert ops[0].quantity == 25


def test_size_is_capped_by_what_the_book_can_fill():
    _, ops = find(bk=100_000.0, b=book(qty=12),
                  c=cfg(max_bankroll_fraction=1.0, min_book_depth=10))
    assert ops[0].quantity <= 12


def test_lower_confidence_buys_less():
    """A shakier estimate gets a smaller bet at the same nominal edge. The
    book has to be deep enough that depth is not what is doing the capping."""
    deep = book(qty=50_000)
    wide = cfg(max_contracts_per_trade=100_000, max_bankroll_fraction=1.0,
               max_cost_per_trade_usd=1_000_000.0)
    _, big = find(FakeEstimator(confidence=0.95), bk=100_000.0, b=deep, c=wide)
    _, small = find(FakeEstimator(confidence=0.55), bk=100_000.0, b=deep, c=wide)
    assert small[0].quantity < big[0].quantity
    assert big[0].quantity > 0


def test_zero_bankroll_trades_nothing():
    _, ops = find(bk=0.0)
    assert ops == []


# ===================== the gate agrees =====================
def test_a_good_forecast_passes_the_gate_once_both_switches_are_on():
    _, ops = find()
    g = ProfitabilityGate({"require_guaranteed": False, "allow_forecast": True,
                           "min_probability_edge": 0.05, "min_confidence": 0.5,
                           "min_ev_usd": 0.01, "max_opportunity_age_seconds": 600})
    d = g.evaluate(ops[0])
    assert d.accepted, d
    assert "NOT guaranteed" in d.detail


def test_the_same_forecast_is_refused_by_the_default_gate():
    """Shipping default is structural-only. Nothing changes until it is
    deliberately turned on."""
    _, ops = find()
    assert ProfitabilityGate({}).evaluate(ops[0]).reason is Reject.FORECASTS_DISABLED


def test_explain_never_claims_a_payout_floor():
    _, ops = find()
    text = ops[0].explain()
    assert "NO GUARANTEED PAYOUT" in text
    assert "guaranteed payout   :" not in text
    assert "our probability" in text and "market implies" in text


# ===================== the LAX incident =====================
# A live scan proposed four trades on KXHIGHLAX-26SEP14 worth up to +212%
# return, from a distribution that contradicted itself. These are the guards
# that would have refused every one of them.

class LadderEstimator:
    """Returns a fixed probability per ticker, and can describe its strikes -
    which is what makes the coherence check possible."""
    name = "ladder_fake"

    def __init__(self, probs, strikes):
        self.probs, self.strikes = probs, strikes

    def candidate_series(self):
        return ["KXHIGHLAX"]

    def parse_strike(self, market):
        lo, hi = self.strikes[market.ticker]
        return lo, hi, f"{lo}-{hi}"

    def estimate(self, market):
        return Estimate(self.probs[market.ticker], 0.56, self.name, {})


def _lax_markets():
    return [Market(ticker=t, event_ticker="KXHIGHLAX-26SEP14",
                   series_ticker="KXHIGHLAX", status="open", close_time=SOON)
            for t in ("T82", "B81.5", "B79.5", "B77.5")]


def test_the_real_incoherent_lax_event_is_refused_entirely():
    """P(>=82) = 44.6% while the bucket 81-82 that contains it is 7.4%.
    Both cannot be true. No market in this event may trade."""
    probs = {"T82": 0.446, "B81.5": 0.074, "B79.5": 0.219, "B77.5": 0.210}
    strikes = {"T82": (82, None), "B81.5": (81, 82),
               "B79.5": (79, 80), "B77.5": (77, 78)}
    ms = _lax_markets()
    books = {m.ticker: book(yes_ask=14, qty=500) for m in ms}
    s = ForecastStrategy(LadderEstimator(probs, strikes), FEES, cfg())
    assert s.find(ms, books, 492.0) == []
    assert all(sk.reason == "event distribution is incoherent" for sk in s.skips)
    assert "44.6%" in s.skips[0].detail


def test_the_same_event_trades_once_the_distribution_is_coherent():
    """The guard must refuse broken distributions, not forecasting itself."""
    probs = {"T82": 0.05, "B81.5": 0.12, "B79.5": 0.30, "B77.5": 0.25}
    strikes = {"T82": (82, None), "B81.5": (81, 82),
               "B79.5": (79, 80), "B77.5": (77, 78)}
    ms = _lax_markets()
    books = {m.ticker: book(yes_ask=14, qty=500) for m in ms}
    s = ForecastStrategy(LadderEstimator(probs, strikes), FEES, cfg())
    ops = s.find(ms, books, 492.0)
    assert ops, s.skips
    assert all(not op.guaranteed for op in ops)


def test_a_monotonicity_violation_is_caught_too():
    """The other reading of those tickers - thresholds rather than buckets -
    is incoherent in a different way, and must also be refused."""
    probs = {"T82": 0.446, "B81.5": 0.074, "B79.5": 0.219, "B77.5": 0.210}
    strikes = {"T82": (82, None), "B81.5": (81.5, None),
               "B79.5": (79.5, None), "B77.5": (77.5, None)}
    ms = _lax_markets()
    books = {m.ticker: book(yes_ask=14, qty=500) for m in ms}
    s = ForecastStrategy(LadderEstimator(probs, strikes), FEES, cfg())
    assert s.find(ms, books, 492.0) == []


# ===================== trust-weighted disagreement =====================
def test_a_huge_disagreement_at_low_confidence_is_refused():
    """30 points of claimed mispricing at 56% confidence describes our own
    error, not the market's."""
    s, ops = find(FakeEstimator(probability=0.95, confidence=0.56))
    assert ops == []
    assert any(sk.reason == "disagreement too large to trust" for sk in s.skips), s.skips


def test_the_same_disagreement_is_allowed_at_high_confidence():
    s_low, low = find(FakeEstimator(probability=0.80, confidence=0.55))
    s_high, high = find(FakeEstimator(probability=0.80, confidence=0.99))
    assert low == [], s_low.skips
    assert high, s_high.skips


def test_a_modest_disagreement_at_modest_confidence_still_trades():
    """The guard must not turn into a blanket ban on forecasting."""
    s, ops = find(FakeEstimator(probability=0.70, confidence=0.60))
    assert ops, s.skips


def test_the_allowance_scales_with_confidence():
    c = cfg(max_probability_edge=0.25)
    assert c.max_probability_edge == 0.25
    # 0.25 x 0.56 confidence = a 14-point allowance; the LAX trades claimed
    # 25 to 31 points.
    assert 0.25 * 0.56 < 0.25
    assert 0.25 * 0.85 > 0.25 * 0.56


class CountingEstimator(LadderEstimator):
    """Counts how many times it is asked."""
    def __init__(self, probs, strikes):
        super().__init__(probs, strikes)
        self.calls = 0

    def estimate(self, market):
        self.calls += 1
        return super().estimate(market)


def test_each_market_is_estimated_exactly_once_per_scan():
    """The coherence pass and the pricing pass must share one estimate.
    Estimating twice doubles every data call the estimator makes."""
    probs = {"T82": 0.05, "B81.5": 0.12, "B79.5": 0.30, "B77.5": 0.25}
    strikes = {"T82": (82, None), "B81.5": (81, 82),
               "B79.5": (79, 80), "B77.5": (77, 78)}
    ms = _lax_markets()
    books = {m.ticker: book(yes_ask=14, qty=500) for m in ms}
    est = CountingEstimator(probs, strikes)
    ForecastStrategy(est, FEES, cfg()).find(ms, books, 492.0)
    assert est.calls == len(ms), f"{est.calls} estimates for {len(ms)} markets"


# ===================== the Miami lottery ticket =====================
# The bot's first real fill was 25 contracts of "Miami 94F or above" at 2c:
#
#   our probability : 9.5% YES (confidence 86%)
#   market implies  : 2.0% YES
#   discrepancy     : 7.5 probability points      <- under the 21pt allowance
#   return on cost  : +364.58%
#
# 7.5 points is small, so the points cap waved it through. But 9.5 against 2
# is 4.75x the market's number, at the far tail of a temperature distribution
# where the model is almost entirely its own sigma assumption. And the whole
# position was $0.50 - a 47:1 lottery ticket funded with pocket change.

def test_the_miami_two_cent_lottery_ticket_is_refused():
    s, ops = find(FakeEstimator(probability=0.095, confidence=0.86),
                  b=book(yes_ask=2, qty=264))
    assert ops == []
    assert any(sk.reason == "price is in the tail" for sk in s.skips), s.skips


def test_the_other_tail_is_refused_too():
    """A 97c market is the same problem seen from the other end: our 99.9%
    against a 97% market is a real edge in points terms and still a place we
    have no business trading."""
    s, ops = find(FakeEstimator(probability=0.999, confidence=0.9),
                  b=book(yes_ask=97, qty=500),
                  c=cfg(min_probability_edge=0.01))
    assert ops == []
    assert any(sk.reason == "price is in the tail" for sk in s.skips), s.skips


def test_a_small_points_gap_that_is_a_huge_ratio_is_refused():
    """Inside the allowed price band, the ratio guard still has to bite:
    18% against a 6% market is 12 points but three times the market."""
    s, ops = find(FakeEstimator(probability=0.18, confidence=0.9),
                  b=book(yes_ask=6, qty=500),
                  c=cfg(min_price_cents=1, max_relative_disagreement=2.5))
    assert ops == []
    assert any(sk.reason == "disagreement too large in relative terms"
               for sk in s.skips), s.skips


def test_the_real_new_york_trade_still_passes():
    """20.2% against a 15c market - 5.1 points, 1.33x. The guards must catch
    lottery tickets without banning ordinary forecasting."""
    s, ops = find(FakeEstimator(probability=0.202, confidence=0.83),
                  b=book(yes_ask=15, qty=282))
    assert ops, s.skips
    assert ops[0].ev_usd > 0


# ===================== a contract cap is not a risk limit =====================
def test_position_cost_is_capped_in_dollars_not_just_contracts():
    """25 contracts is $0.50 at 2c and $24.75 at 99c. The dollar cap is what
    makes risk comparable across price levels."""
    s, ops = find(FakeEstimator(probability=0.60, confidence=0.9),
                  bk=100_000.0, b=book(yes_ask=40, qty=100_000),
                  c=cfg(max_contracts_per_trade=100_000,
                        max_bankroll_fraction=1.0,
                        max_cost_per_trade_usd=25.0))
    assert ops, s.skips
    assert ops[0].gross_cost <= 25.0 + 0.40, ops[0].gross_cost


def test_the_dollar_cap_binds_at_every_price_in_the_band():
    for ask in (5, 20, 50, 80, 95):
        _, ops = find(FakeEstimator(probability=min(0.99, ask / 100 * 1.6),
                                    confidence=0.9),
                      bk=100_000.0, b=book(yes_ask=ask, qty=100_000),
                      c=cfg(max_contracts_per_trade=100_000,
                            max_bankroll_fraction=1.0,
                            max_cost_per_trade_usd=25.0,
                            max_relative_disagreement=5.0))
        for op in ops:
            assert op.gross_cost <= 25.0 + 1.0, f"{ask}c -> ${op.gross_cost:.2f}"


# ============ the data source behind the number ============
def test_a_fallback_forecast_is_marked_degraded():
    """The NWS gridpoint fallback returns ONE number and the estimator invents
    a spread for it. That invented spread then flows into the edge, the size
    and the Brier score as if it had been measured across thirty members.

    On 18 September the ensemble came back unusable five times and nothing
    downstream knew."""
    from kalshibot.weatherdata import DailyTempForecast
    import datetime as _dt
    good = DailyTempForecast(_dt.date(2026, 9, 20), "high",
                             [80.0, 81.0, 79.0, 82.0, 80.5, 81.5],
                             source="open-meteo-ensemble")
    assert good.degraded is False

    fallback = DailyTempForecast(_dt.date(2026, 9, 20), "high", [80.0],
                                 source="nws-gridpoint")
    assert fallback.degraded is True

    # An ensemble that only half answered is degraded too.
    thin = DailyTempForecast(_dt.date(2026, 9, 20), "high", [80.0, 81.0],
                             source="open-meteo-ensemble")
    assert thin.degraded is True


def test_a_degraded_source_is_skipped_by_default():
    from kalshibot.strategies.forecast import ForecastConfig
    cfg = ForecastConfig.from_dict({})
    assert cfg.require_primary_source is True
    # and can be turned off deliberately
    assert ForecastConfig.from_dict(
        {"require_primary_source": False}).require_primary_source is False
