import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.estimators.weather import WeatherTemperatureEstimator
from kalshibot.models import Market
from kalshibot.weatherdata import DailyTempForecast

CFG = {
    "series": [{"ticker_prefix": "KXHIGHNY", "label": "NYC", "station": "KNYC",
                "latitude": 40.78, "longitude": -73.97, "kind": "high",
                "timezone": "America/New_York"}],
    "station_representativeness_sigma_f": 1.6,
    "min_sigma_f": 1.0,
    "fallback_sigma_by_lead_hours": {6: 1.6, 24: 2.8, 72: 5.0},
}


def est():
    return WeatherTemperatureEstimator(CFG, data=None)


def fc(members, kind="high", obs=None, lead=10.0):
    f = DailyTempForecast(dt.date(2026, 9, 12), kind, members, "test", obs, lead)
    return f


# ---------------- ticker / strike parsing ----------------
def test_parses_event_date():
    m = Market(ticker="KXHIGHNY-26SEP12-B72.5", event_ticker="KXHIGHNY-26SEP12")
    assert est().parse_event_date(m, "America/New_York") == dt.date(2026, 9, 12)


def test_parses_range_from_api_fields():
    m = Market(ticker="X", strike_type="between", floor_strike=72, cap_strike=73)
    lo, hi, label = est().parse_strike(m)
    assert (lo, hi) == (72, 73) and "72 to 73" in label


def test_parses_or_above_from_text():
    m = Market(ticker="X", subtitle="80° or above")
    lo, hi, _ = est().parse_strike(m)
    assert lo == 80 and hi is None


def test_parses_or_below_from_text():
    m = Market(ticker="X", subtitle="65 or below")
    lo, hi, _ = est().parse_strike(m)
    assert lo is None and hi == 65


def test_parses_dash_range_from_text():
    m = Market(ticker="X", subtitle="72-73")
    lo, hi, _ = est().parse_strike(m)
    assert (lo, hi) == (72.0, 73.0)


# ---------------- probability ----------------
def test_probabilities_across_the_board_sum_to_one():
    e = est()
    f = fc([70, 71, 72, 72, 73, 74, 71, 72, 73, 72])
    total = sum(e._probability_in_range(f, t, t) for t in range(50, 95))
    assert abs(total - 1.0) < 0.01, total


def test_bucket_at_the_mean_is_most_likely():
    e = est()
    f = fc([72] * 10)
    assert e._probability_in_range(f, 72, 72) > e._probability_in_range(f, 75, 75)


def test_or_above_and_or_below_are_complements():
    e = est()
    f = fc([70, 72, 74, 71, 73, 72, 72, 71, 73, 74])
    above = e._probability_in_range(f, 73, None)
    below = e._probability_in_range(f, None, 72)
    assert abs(above + below - 1.0) < 1e-6


def test_wider_spread_flattens_the_distribution():
    e = est()
    tight = e._probability_in_range(fc([72] * 10), 72, 72)
    wide = e._probability_in_range(fc([66, 68, 70, 72, 74, 76, 78, 70, 74, 72]), 72, 72)
    assert wide < tight


def test_observed_high_rules_out_lower_buckets():
    e = est()
    f = fc([70] * 10, obs=78.0)          # already 78F today
    assert e._probability_in_range(f, 70, 70) < 0.01
    assert e._probability_in_range(f, 78, None) > 0.5


def test_sigma_never_below_floor():
    e = est()
    assert e._sigma(fc([72] * 30)) >= CFG["min_sigma_f"]


def test_single_member_uses_lead_time_sigma():
    e = est()
    assert e._sigma(fc([72], lead=48)) > e._sigma(fc([72], lead=3))


# ---------------- confidence ----------------
def test_more_members_more_confidence():
    e = est()
    assert e._confidence(fc([72] * 30), 0.5) > e._confidence(fc([72], lead=10), 0.5)


def test_longer_lead_less_confidence():
    e = est()
    near = fc([71, 72, 73] * 10, lead=6)
    far = fc([71, 72, 73] * 10, lead=55)
    assert e._confidence(far, 0.5) < e._confidence(near, 0.5)


def test_model_disagreement_lowers_confidence():
    e = est()
    agree = fc([72, 72, 73, 72, 71, 72, 72, 73, 72, 72], lead=10)
    disagree = fc([62, 68, 72, 78, 84, 66, 74, 80, 70, 76], lead=10)
    assert e._confidence(disagree, 0.5) < e._confidence(agree, 0.5)


def test_confidence_is_bounded():
    e = est()
    c = e._confidence(fc([72] * 30, obs=71.0, lead=2), 0.5)
    assert 0.0 <= c <= 1.0


# ===================== the strike convention (audit, 2026-09-14) =====================
# A live audit showed Kalshi's own subtitle disagreeing with how we read its
# structured fields on EVERY threshold market:
#
#   T82  strike_type=greater floor=82  subtitle "83 or above"   we said "82 or above"
#   T74  strike_type=less    cap=74    subtitle "73 or below"   we said "74 or below"
#
# `between` is inclusive; `greater` and `less` are strict. Reading a strict
# bound as inclusive put P(>=82) where P(>=83) belonged, which placed a
# threshold inside a bucket it should have sat above and made the whole
# distribution contradict itself.

def _m(ticker, strike_type, floor=None, cap=None, subtitle=""):
    return Market(ticker=ticker, strike_type=strike_type, floor_strike=floor,
                  cap_strike=cap, subtitle=subtitle)


def test_greater_is_strictly_greater():
    lo, hi, label = WeatherTemperatureEstimator.parse_strike(
        _m("T82", "greater", floor=82, subtitle="83° or above"))
    assert (lo, hi) == (83, None), label
    assert label == "83 or above"


def test_less_is_strictly_less():
    lo, hi, label = WeatherTemperatureEstimator.parse_strike(
        _m("T74", "less", cap=74, subtitle="73° or below"))
    assert (lo, hi) == (None, 73), label


def test_greater_or_equal_and_less_or_equal_stay_inclusive():
    lo, _, _ = WeatherTemperatureEstimator.parse_strike(
        _m("X", "greater_or_equal", floor=82, subtitle="82° or above"))
    assert lo == 82
    _, hi, _ = WeatherTemperatureEstimator.parse_strike(
        _m("Y", "less_or_equal", cap=74, subtitle="74° or below"))
    assert hi == 74


def test_between_is_inclusive_on_both_ends():
    lo, hi, label = WeatherTemperatureEstimator.parse_strike(
        _m("B81.5", "between", floor=81, cap=82, subtitle="81° to 82°"))
    assert (lo, hi) == (81, 82) and label == "81 to 82"


def test_every_threshold_from_the_live_audit_matches_its_subtitle():
    """The audit's own rows, as Kalshi served them."""
    for ticker, st, floor, cap, subtitle, expected in [
            ("KXHIGHNY-T74", "less", None, 74, "73° or below", (None, 73)),
            ("KXHIGHNY-T81", "greater", 81, None, "82° or above", (82, None)),
            ("KXHIGHCHI-T72", "less", None, 72, "71° or below", (None, 71)),
            ("KXHIGHCHI-T79", "greater", 79, None, "80° or above", (80, None)),
            ("KXHIGHAUS-T97", "less", None, 97, "96° or below", (None, 96)),
            ("KXHIGHAUS-T104", "greater", 104, None, "105° or above", (105, None)),
            ("KXHIGHLAX-T82", "greater", 82, None, "83° or above", (83, None)),
            ("KXHIGHDEN-T90", "greater", 90, None, "91° or above", (91, None))]:
        lo, hi, label = WeatherTemperatureEstimator.parse_strike(
            _m(ticker, st, floor, cap, subtitle))
        assert (lo, hi) == expected, f"{ticker}: got {label}"


def test_a_field_subtitle_conflict_declines_the_market():
    """If Kalshi's two descriptions disagree we have misread the convention
    again. Guessing which one is right is how the first bug happened."""
    lo, hi, label = WeatherTemperatureEstimator.parse_strike(
        _m("X", "greater", floor=82, subtitle="90° or above"))
    assert (lo, hi) == (None, None)
    assert label.startswith("CONFLICT"), label
    assert "83 or above" in label and "90 or above" in label


# ===================== an observed extreme is a hard bound =====================
def _fc(mean, spread, n=122, observed=None, kind="high", lead=12.0):
    import datetime as _dt
    import random as _r
    _r.seed(7)
    return DailyTempForecast(
        date=_dt.date(2026, 9, 14), kind=kind,
        members=[_r.gauss(mean, spread) for _ in range(n)],
        source="test", observed_extreme_so_far=observed, lead_hours=lead)


def test_an_already_observed_high_rules_out_everything_clearly_below_it():
    """NY on 26SEP14: the high was already 75.02F and the bot still put 30.4%
    on "74 or below". Clamping the ensemble members is not enough - each one
    carries a Gaussian kernel that leaks probability below the observation."""
    est = WeatherTemperatureEstimator({"series": []})
    fc = _fc(74.21, 1.97, observed=75.02)
    assert est._probability_in_range(fc, None, 73) == 0.0
    assert est._probability_in_range(fc, None, 70) == 0.0


def test_a_near_boundary_observation_is_not_treated_as_certain():
    """Kalshi settles from The Weather Company; we observe from NWS. Same
    station, different provider. 75.02F observed is only half a degree clear
    of the 74.5 boundary, so "74 or below" must stay possible - false
    certainty is the most expensive kind of wrong."""
    est = WeatherTemperatureEstimator({"series": []})
    fc = _fc(74.21, 1.97, observed=75.02)
    assert est._probability_in_range(fc, None, 74) > 0.0


def test_the_margin_is_configurable_and_can_be_switched_off():
    fc = _fc(74.21, 1.97, observed=75.02)
    strict = WeatherTemperatureEstimator(
        {"series": [], "observation_margin_f": 0.0})
    assert strict._probability_in_range(fc, None, 74) == 0.0
    loose = WeatherTemperatureEstimator(
        {"series": [], "observation_margin_f": 2.0})
    assert loose._probability_in_range(fc, None, 74) > 0.1
    assert WeatherTemperatureEstimator({"series": []}).observation_margin == 0.5


def test_the_bucket_containing_the_observation_still_has_probability():
    """The bound rules out what is below it, not the bucket it sits in."""
    est = WeatherTemperatureEstimator({"series": []})
    fc = _fc(74.21, 1.97, observed=75.02)
    assert est._probability_in_range(fc, 74, 75) > 0.05
    assert est._probability_in_range(fc, 76, 77) > 0.05


def test_an_already_observed_low_rules_out_everything_above_it():
    est = WeatherTemperatureEstimator({"series": []})
    fc = _fc(40.0, 2.0, observed=38.0, kind="low")
    assert est._probability_in_range(fc, 40, 41) == 0.0
    assert est._probability_in_range(fc, None, 38) > 0.0


def test_without_an_observation_nothing_is_ruled_out():
    est = WeatherTemperatureEstimator({"series": []})
    fc = _fc(74.21, 1.97, observed=None)
    assert est._probability_in_range(fc, None, 74) > 0.0


def test_the_lax_event_is_coherent_once_both_bugs_are_fixed():
    """The full 2026-09-14 LAX board, end to end: parse every strike the way
    Kalshi describes it, price it, and check the distribution against itself."""
    from kalshibot.coherence import Claim, check
    est = WeatherTemperatureEstimator({"series": []})
    fc = _fc(82.38, 4.64, observed=71.96, lead=15.6)
    board = [("B75.5", "between", 75, 76, "75° to 76°"),
             ("B77.5", "between", 77, 78, "77° to 78°"),
             ("B79.5", "between", 79, 80, "79° to 80°"),
             ("B81.5", "between", 81, 82, "81° to 82°"),
             ("T75", "less", None, 75, "74° or below"),
             ("T82", "greater", 82, None, "83° or above")]
    claims = []
    for ticker, st, floor, cap, subtitle in board:
        lo, hi, _ = WeatherTemperatureEstimator.parse_strike(
            _m(ticker, st, floor, cap, subtitle))
        claims.append(Claim(ticker, lo, hi,
                            est._probability_in_range(fc, lo, hi)))
    assert check(claims) == [], check(claims)

    # and the shape is sensible: rising toward the 82.4F mean, not the
    # 21.9% -> 7.4% -> 44.6% sawtooth the double-counting produced
    by_ticker = {c.ticker: c.probability for c in claims}
    assert by_ticker["B79.5"] < by_ticker["B81.5"]
    assert by_ticker["T82"] > by_ticker["B81.5"]


def test_the_series_is_derived_when_the_payload_omits_it():
    """Live Kalshi market payloads carry ticker and event_ticker but no
    series_ticker. Taking the field at face value wrote an empty series on
    every forecast logged, which collapsed calibration-by-series - the report
    that would catch a city priced off the wrong station - into one nameless
    bucket."""
    from kalshibot.models import Market
    m = Market.from_api({"ticker": "KXHIGHCHI-26SEP17-B74.5",
                         "event_ticker": "KXHIGHCHI-26SEP17"})
    assert m.series_ticker == "KXHIGHCHI"
    # An explicit field still wins.
    m2 = Market.from_api({"ticker": "KXHIGHCHI-26SEP17-B74.5",
                          "series_ticker": "KXSOMETHINGELSE"})
    assert m2.series_ticker == "KXSOMETHINGELSE"
    assert Market.from_api({"ticker": ""}).series_ticker == ""
