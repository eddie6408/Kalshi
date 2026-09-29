"""The inflation strategy: Cleveland Fed archive parsing, the model, the
estimator, and the engine rules added with it.

Pinned: the archive is read exactly (dates, markers, actuals); a contract is
YES only when the published one-decimal figure is strictly above the strike;
the measured error distribution is used when fitted; far months fall back to
the trend at lower confidence; released months, unknown series and other
strike types are declined with a reason; a regime shift cuts confidence; a
disagreement with FRED blocks; the engine refuses a position that cannot win
alongside one already held on the same ladder, and anything from a strategy
the operator paused.
"""
import json
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest
from kalshibot import inflation_model as im
from kalshibot.econdata import MeasureHistory, parse_cleveland, parse_fred_csv
from kalshibot.estimators.inflation import InflationNowcastEstimator, target_month
from kalshibot.models import Market


def chart(sub, labels, series):
    """A Cleveland webchart: labels may include ("CPI Aug", "vline")."""
    cats = [{"label": l, "vline": "true"} if isinstance(l, tuple) else {"label": l}
            for l in labels]
    return {"chart": {"subcaption": sub}, "categories": [{"category": cats}],
            "dataset": [{"seriesname": n, "data": [{"value": v} for v in vals]}
                        for n, vals in series.items()]}


# ------------------------------------------------------------ parsing
def test_the_archive_is_read_with_markers_skipped_and_the_year_rolled():
    c = chart("2026-12", ["12/30", ("CPI Nov", "v"), "12/31", "01/05", "01/13"], {
        "CPI Inflation": ["0.21", "0.22", "0.25", ""],
        "Actual CPI Inflation": ["", "", "", "0.3123"],
        "Core CPI Inflation": ["0.3", "", "", ""],
        "Something else": ["9"]})
    a = parse_cleveland([c])
    h = a["2026-12"]["CPI"]
    assert h.nowcasts == [(date(2026, 12, 30), 0.21), (date(2026, 12, 31), 0.22),
                          (date(2027, 1, 5), 0.25)]
    assert h.actual == pytest.approx(0.3123) and h.released == date(2027, 1, 13)
    assert a["2026-12"]["CORE_CPI"].nowcasts == [(date(2026, 12, 30), 0.3)]
    assert set(a["2026-12"]) == {"CPI", "CORE_CPI"}


def test_fred_csv():
    rows = parse_fred_csv("observation_date,CPIAUCSL\n2026-07-01,331.5\n2026-08-01,332.1\nbad,1\n")
    assert rows == [(date(2026, 7, 1), 331.5), (date(2026, 8, 1), 332.1)]


# ------------------------------------------------------------ the maths
def test_yes_needs_the_published_figure_strictly_above_the_strike():
    # 0.3 strike: the published figure must be 0.4+, i.e. unrounded >= 0.35
    assert im.prob_above(0.3, 0.35, 0.0) == 1.0
    assert im.prob_above(0.3, 0.3499, 0.0) == 0.0
    assert im.prob_above(0.3, 0.35, 0.1) == pytest.approx(0.5)
    assert im.prob_above(-0.1, 0.0, 0.1) > 0.5


def test_the_measured_distribution_is_used_when_fitted():
    q = im.quantiles_of([x / 100 for x in range(-20, 21)])      # uniform -0.2..0.2
    # mu 0.40, strike 0.3 -> threshold 0.35, residual <= 0.05 -> 62.5%
    assert im.prob_yes(0.3, 0.40, 0.1, q) == pytest.approx(0.625, abs=0.01)
    assert 0 < im.prob_yes(0.3, 0.0, 0.1, q) < 0.01          # far tail, never 0
    assert im.prob_yes(0.3, 0.40, 0.1, None) == pytest.approx(im.prob_above(0.3, 0.40, 0.1))


def _archive():
    a = {}
    for i, m in enumerate(["2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08"]):
        a[m] = {"CPI": MeasureHistory(actual=0.1 + 0.05 * i, released=date(2026, 4 + i, 5))}
    a["2026-09"] = {"CPI": MeasureHistory(nowcasts=[(date(2026, 9, d), 0.30 + 0.01 * k)
                                                    for k, d in enumerate([1, 2, 3, 4, 8, 9])])}
    return a


def test_forecast_blends_the_nowcast_with_the_trend():
    p = im.Params(blend_w=0.8, bias=0.0)
    fc = im.forecast(_archive(), "CPI", "mom", "2026-09", date(2026, 9, 9), p, 20.0)
    trend = sum(0.1 + 0.05 * i for i in range(6)) / 6
    assert fc.source == "blend" and fc.cleveland == pytest.approx(0.35)
    assert fc.mu == pytest.approx(0.8 * 0.35 + 0.2 * trend)
    # no peeking: a nowcast dated after "today" is not used
    early = im.forecast(_archive(), "CPI", "mom", "2026-09", date(2026, 9, 2), p, 30.0)
    assert early.cleveland == pytest.approx(0.31)


def test_far_months_use_the_trend_and_released_months_are_final():
    p = im.Params()
    far = im.forecast(_archive(), "CPI", "mom", "2026-12", date(2026, 9, 9), p, 100.0)
    assert far.source == "trend" and far.months_ahead == 4 and far.sigma > 0.1
    done = im.forecast(_archive(), "CPI", "mom", "2026-08", date(2026, 9, 20), p, 1.0)
    assert done.source == "released" and done.mu == pytest.approx(0.35)


def test_a_moving_nowcast_is_a_regime_shift():
    a = _archive()
    a["2026-09"]["CPI"].nowcasts[-1] = (date(2026, 9, 9), 0.60)
    fc = im.forecast(a, "CPI", "mom", "2026-09", date(2026, 9, 9), im.Params(), 20.0,
                     regime_threshold=0.1)
    assert fc.regime_shift and fc.regime_delta == pytest.approx(0.30)


def test_fitting_measures_error_by_distance_from_release():
    a = {}
    for y in range(2016, 2020):
        for m in range(1, 13):
            key = f"{y}-{m:02d}"
            rel = date(y + (m == 12), m % 12 + 1, 12)
            nc = [(rel - timedelta(days=d), 0.2 + (0.2 if d > 20 else 0.05) * (-1) ** d)
                  for d in range(1, 40)]
            a[key] = {"CPI": MeasureHistory(nowcasts=nc, actual=0.2, released=rel)}
    params, rep = im.fit(a, "CPI", "mom", since="2016-06")
    assert params["fitted"] and rep["n"] > 1000
    assert params["sigma"][3] < params["sigma"][31]        # closer is sharper
    assert len(params["quantiles"][31]) == 101


# ------------------------------------------------------------ the estimator
class Data:
    def __init__(self, archive, fred=None, fail=False):
        self.archive, self.fred_rows, self.fail = archive, fred, fail

    def cleveland(self, kind="month"):
        if self.fail:
            raise RuntimeError("503")
        return self.archive

    def fred(self, series):
        if self.fred_rows is None:
            raise RuntimeError("no fred")
        return self.fred_rows


def mkt(ticker="KXCPI-26SEP-T0.3", strike=0.3, strike_type="greater",
        title="Will CPI rise more than 0.3% in September 2026?", close_days=12):
    return Market(ticker=ticker, event_ticker=ticker.rsplit("-", 1)[0],
                  series_ticker=ticker.split("-")[0], status="open", title=title,
                  close_time=datetime(2026, 9, 9, 12, tzinfo=timezone.utc) + timedelta(days=close_days),
                  raw={"strike_type": strike_type, "floor_strike": strike})


NOW = datetime(2026, 9, 9, 12, tzinfo=timezone.utc).timestamp()
STATUS = str(Path(tempfile.gettempdir()) / "kalshibot-test-inflation-status.json")


def est(archive=None, fred=None):
    return InflationNowcastEstimator({"params_path": "/nonexistent.json", "status_path": STATUS},
                                     Data(archive or _archive(), fred), clock=lambda: NOW)


def test_target_months():
    assert target_month(mkt()) == "2026-09"
    assert target_month(mkt(ticker="KXCPI-26DEC-T0.3", title="")) == "2026-12"
    assert target_month(mkt(ticker="KXPCEHEAD-26SEP30-T0.0", title="")) == "2026-08"
    assert target_month(mkt(ticker="KXCPI-XYZ-T0.3", title="")) is None


def test_an_estimate_carries_its_evidence():
    e = est()
    r = e.estimate(mkt())
    assert r.usable and 0.01 <= r.probability_yes <= 0.99
    ev = r.evidence
    assert ev["measure"] == "CPI" and ev["freq"] == "mom" and ev["target_date"] == "2026-09"
    assert ev["model"] == "blend" and ev["lead_hours"] == pytest.approx(12 * 24)
    assert ev["model_version"] == im.MODEL_VERSION and "alt_probability" in ev
    # a higher bar is less likely
    lo = e.estimate(mkt(strike=0.1)).probability_yes
    hi = e.estimate(mkt(strike=0.5)).probability_yes
    assert lo > r.probability_yes > hi


def test_declines_say_why():
    e = est()
    assert "not an inflation series" in e.estimate(mkt(ticker="KXHIGHNY-26SEP-T70")).skip_reason
    assert "strike type" in e.estimate(mkt(strike_type="between")).skip_reason
    assert "already released" in e.estimate(mkt(ticker="KXCPI-26AUG-T0.3",
                                               title="CPI in August 2026")).skip_reason
    bad = InflationNowcastEstimator({"params_path": "/x.json", "status_path": STATUS},
                                    Data({}, fail=True), clock=lambda: NOW)
    assert "unavailable" in bad.estimate(mkt()).skip_reason


def test_trend_only_months_are_less_trusted():
    e = est()
    near = e.estimate(mkt())
    far = e.estimate(mkt(ticker="KXCPI-26DEC-T0.3", title="CPI in December 2026", close_days=100))
    assert far.evidence["model"] == "trend" and far.confidence < near.confidence


def test_a_disagreement_with_fred_blocks_and_agreement_does_not():
    a = _archive()                                   # August actual 0.35
    agree = [(date(2026, 7, 1), 100.0), (date(2026, 8, 1), 100.35)]
    disagree = [(date(2026, 7, 1), 100.0), (date(2026, 8, 1), 101.0)]
    e = est(a, agree)
    assert e.estimate(mkt()).usable and "agree" in e.estimate(mkt()).evidence["cross_check"]
    e2 = est(a, disagree)
    assert "data conflict" in e2.estimate(mkt()).skip_reason


def test_the_coherence_ladder_is_described():
    e = est()
    lo, hi, label = e.parse_strike(mkt(strike=0.3))
    assert lo == pytest.approx(0.4) and hi is None and "0.3" in label


# ------------------------------------------------------------ engine rules
class _Pos:
    def __init__(self, contracts):
        self.contracts = contracts


class _Markets:
    def get_market(self, t):
        k = float(t.rsplit("-T", 1)[1])
        return Market(ticker=t, event_ticker=t.rsplit("-", 1)[0], series_ticker="KXCPI",
                      status="open", raw={"strike_type": "greater", "floor_strike": k})


def _engine(positions, paused=None, tmp=None):
    from kalshibot.arb_engine import ArbitrageEngine
    e = ArbitrageEngine.__new__(ArbitrageEngine)
    e._positions = positions
    e.market = _Markets()
    if tmp:
        class S:
            def path(self, dotted, default):
                return Path(tmp)
        e.s = S()
        if paused is not None:
            (Path(tmp) / "paused.json").write_text(json.dumps({"paused": paused}))
    return e


class _Leg:
    def __init__(self, ticker, side):
        self.ticker, self.side = ticker, side


class _Op:
    def __init__(self, ticker, side, strategy="forecast:inflation_nowcast"):
        self.legs = [_Leg(ticker, side)]
        self.strategy_type = strategy


def test_a_position_that_cannot_win_alongside_one_held_is_refused():
    from kalshibot.inflation_model import split_threshold
    assert split_threshold("KXCPI-26DEC-T-0.1") == ("KXCPI-26DEC", -0.1)
    e3 = _engine({"KXCPI-26DEC-T-0.1": _Pos(-3)})          # NO: value <= -0.1
    assert not e3._conflict_ok(_Op("KXCPI-26DEC-T0.2", "yes"))[0]
    e = _engine({"KXCPI-26SEP-T0.4": _Pos(10)})            # YES: value > 0.4
    ok, why = e._conflict_ok(_Op("KXCPI-26SEP-T0.3", "no"))   # NO: value <= 0.3
    assert not ok and "cannot win" in why
    assert e._conflict_ok(_Op("KXCPI-26SEP-T0.5", "no"))[0]   # 0.4 < v <= 0.5 is possible
    assert e._conflict_ok(_Op("KXCPI-26OCT-T0.3", "no"))[0]   # another event
    e2 = _engine({"KXCPI-26SEP-T0.2": _Pos(-5)})           # NO: value <= 0.2
    assert not e2._conflict_ok(_Op("KXCPI-26SEP-T0.3", "yes"))[0]   # > 0.3 and <= 0.2
    assert e2._conflict_ok(_Op("KXCPI-26SEP-T0.1", "yes"))[0]       # 0.1 < v <= 0.2


def test_a_paused_strategy_opens_nothing():
    from kalshibot.arb_engine import strategy_name
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine({}, paused={"inflation_nowcast": True, "weather_temperature": False}, tmp=tmp)
        assert e.paused_strategies() == {"inflation_nowcast"}
    assert strategy_name(_Op("x", "yes")) == "inflation_nowcast"
    assert strategy_name(_Op("x", "yes", strategy="ladder")) == "arbitrage"


def test_the_shipped_config_registers_the_inflation_strategy():
    import yaml
    from kalshibot.config import _StrictLoader
    from kalshibot.estimators.registry import ESTIMATORS
    raw = yaml.load(open(Path(__file__).resolve().parents[1] / "config.yaml"), Loader=_StrictLoader)
    assert "inflation_nowcast" in raw["forecast"]["estimators"]
    assert "inflation_nowcast" in ESTIMATORS
    per = raw["forecast"]["per_estimator"]["inflation_nowcast"]
    assert per["max_lead_hours"] >= 24
    reg = raw["sources"]["registry"]
    assert "inflation_nowcast" in reg["cleveland_fed_nowcast"]["required_by"]
