"""Phase B: the same-day nowcast.

Near the end of the day the high is mostly already decided by what the
thermometer has recorded; what is left is a question about the next few
hours. These pin the model that answers it that way, and the plumbing that
feeds it fresh readings instead of half-hour-old ones.
"""
import datetime as dt
import math
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kalshibot import nowcast
from kalshibot.nowcast import NowcastConfig, compute
from kalshibot.sources import SourceMonitor

NY = ZoneInfo("America/New_York")
DAY = dt.date(2026, 9, 26)


def at(h, m=0, day=DAY):
    return dt.datetime.combine(day, dt.time(h, m), tzinfo=NY)


def model(values_by_hour, day=DAY):
    return [(at(h, day=day), v) for h, v in values_by_hour.items()]


FLAT = NowcastConfig(sigma_base_f=1.0, sigma_per_hour_f=0.0, observation_margin_f=0.0)


# ------------------------------------------------------------- the maths
def test_after_the_peak_the_high_is_what_was_recorded():
    """At 21:00 with 80F already recorded and the evening cooling, the high
    is ~80 - nearly all the probability sits in the 80 bucket."""
    obs = [(at(15), 80.0), (at(18), 76.0), (at(20), 72.0)]
    m = model({h: 70.0 - (h - 21) for h in range(21, 24)})
    nc = compute(at(21), DAY, "high", obs, m, FLAT)
    assert nc.observed_extreme == 80.0 and nc.bound == 80.0
    assert nc.probability_between(80, 80) > 0.99
    assert nc.probability_between(None, 79) == 0.0        # already ruled out


def test_before_the_peak_the_remaining_hours_decide():
    obs = [(at(9), 64.0), (at(10), 66.0)]
    m = model({10: 66.0, 12: 71.0, 14: 74.0, 16: 73.0, 18: 69.0})
    nc = compute(at(10, 30), DAY, "high", obs, m, NowcastConfig(observation_margin_f=0.0))
    assert abs(nc.mean - 74.0) < 1e-9                     # model peak, no bias
    assert nc.hours_to_peak > 3
    assert nc.probability_between(None, 70) < 0.05


def test_todays_model_error_shifts_the_rest_of_the_day():
    """The model has run 2F cold all morning: its afternoon peak moves up 2F."""
    obs = [(at(9), 66.0), (at(10), 68.0), (at(11), 70.0)]
    m = model({9: 64.0, 10: 66.0, 11: 68.0, 14: 74.0})
    nc = compute(at(11, 10), DAY, "high", obs, m, FLAT)
    assert abs(nc.bias_f - 2.0) < 1e-9 and nc.bias_samples == 3
    assert abs(nc.mean - 76.0) < 1e-9


def test_a_wild_model_error_is_clipped():
    obs = [(at(10), 90.0), (at(11), 90.0)]
    m = model({10: 70.0, 11: 70.0, 14: 72.0})
    nc = compute(at(11, 10), DAY, "high", obs, m, NowcastConfig(max_bias_correction_f=4.0))
    assert nc.bias_f == 4.0 and any("clipped" in n for n in nc.notes)


def test_too_few_recent_readings_means_no_correction():
    nc = compute(at(11), DAY, "high", [(at(4), 60.0)], model({4: 50.0, 14: 70.0}), FLAT)
    assert nc.bias_f == 0.0 and nc.bias_samples == 0


def test_uncertainty_grows_with_time_to_the_peak():
    cfg = NowcastConfig(sigma_base_f=0.8, sigma_per_hour_f=0.5, sigma_max_f=3.0)
    near = compute(at(13), DAY, "high", [], model({14: 70.0}), cfg)
    far = compute(at(6), DAY, "high", [], model({14: 70.0}), cfg)
    assert near.sigma < far.sigma <= 3.0


def test_the_probabilities_of_every_bucket_sum_to_one():
    obs = [(at(10), 66.0), (at(11), 68.0)]
    nc = compute(at(11, 5), DAY, "high", obs, model({11: 67.0, 15: 73.0}), NowcastConfig())
    total = nc.probability_between(None, 60) + sum(
        nc.probability_between(t, t + 1) for t in range(61, 90, 2)) + \
        nc.probability_between(91, None)
    assert abs(total - 1.0) < 1e-6


def test_daily_lows_are_the_mirror_image():
    obs = [(at(5), 52.0)]
    m = model({6: 55.0, 22: 50.0})
    nc = compute(at(6), DAY, "low", obs, m, FLAT)
    assert nc.probability_between(53, None) == 0.0        # already went below 53
    assert 0.0 < nc.probability_between(None, 50) < 1.0


def test_no_model_hours_for_the_day_means_no_nowcast():
    assert compute(at(10), DAY, "high", [(at(9), 60.0)],
                   model({10: 60.0}, day=DAY + dt.timedelta(days=1))) is None


def test_no_readings_and_no_hours_left_means_no_nowcast():
    assert compute(at(23, 30), DAY, "high", [], model({10: 60.0})) is None


def test_the_shipped_config_parses():
    import yaml
    from kalshibot.config import _StrictLoader
    raw = yaml.load(open(Path(__file__).resolve().parents[1] / "config.yaml"),
                    Loader=_StrictLoader)
    c = NowcastConfig.from_dict(raw["weather"]["nowcast"])
    assert c.enabled and c.max_lead_hours == 12 and c.observation_margin_f == 1.0


# ------------------------------------------------------- data plumbing
from test_provenance import ENSEMBLE, META, RouteSession, _Sink  # noqa: E402


def _now_ny():
    return dt.datetime.now(NY)


def _hrrr_payload(hours_back=6, hours_fwd=12, base=70.0):
    now = _now_ny().replace(minute=0, second=0, microsecond=0)
    times = [now + dt.timedelta(hours=h) for h in range(-hours_back, hours_fwd)]
    return {"hourly": {"time": [t.strftime("%Y-%m-%dT%H:%M") for t in times],
                       "temperature_2m": [base + (h % 5) for h in range(len(times))]}}


def _obs_payload(n=4):
    now = dt.datetime.now(dt.timezone.utc)
    return {"features": [{"properties": {
        "timestamp": (now - dt.timedelta(hours=i)).isoformat(),
        "temperature": {"value": 20.0 + i * 0.1, "qualityControl": "V"}}}
        for i in range(n)]}


def _wd(routes):
    from kalshibot.weatherdata import WeatherData
    mon = SourceMonitor()
    sink = _Sink()
    mon.set_sink(sink)
    wd = WeatherData(monitor=mon)
    wd._session = RouteSession(routes, wd._session.headers)
    return wd, mon, sink


def test_hrrr_is_fetched_parsed_and_recorded():
    wd, mon, sink = _wd({"/v1/forecast": _hrrr_payload(), "ncep_hrrr_conus": META})
    hm = wd.hourly_model(40.78, -73.97, "America/New_York")
    assert len(hm.hours) == 18 and hm.hours[0][0].tzinfo is not None
    assert hm.run and hm.provenance_id == 1
    assert sink.rows[0]["source_id"] == "open_meteo_hrrr"
    calls = len(wd._session.calls)
    wd.hourly_model(40.78, -73.97, "America/New_York")
    assert len(wd._session.calls) == calls                 # cached


def test_readings_refresh_on_their_own_clock():
    today = _now_ny().date()
    routes = {"/v1/ensemble": _ens_today(), "meta.json": META, "/observations": _obs_payload()}
    wd, _mon, _sink = _wd(routes)
    wd.obs_refresh_seconds = 0.05
    fc1 = wd.daily_extreme_forecast(40.78, -73.97, "America/New_York", today, "high",
                                    station="KNYC")
    obs_calls = lambda: sum("/observations" in c for c in wd._session.calls)  # noqa: E731
    ens_calls = lambda: sum("/v1/ensemble" in c for c in wd._session.calls)  # noqa: E731
    assert obs_calls() == 1 and fc1.observations
    wd.daily_extreme_forecast(40.78, -73.97, "America/New_York", today, "high", station="KNYC")
    assert obs_calls() == 1                                 # still fresh
    time.sleep(0.06)
    wd.daily_extreme_forecast(40.78, -73.97, "America/New_York", today, "high", station="KNYC")
    assert obs_calls() == 2 and ens_calls() == 1            # readings re-read, forecast not


def test_lead_time_is_never_served_stale_from_the_cache():
    today = _now_ny().date()
    wd, _m, _s = _wd({"/v1/ensemble": _ens_today(), "meta.json": META})
    a = wd.daily_extreme_forecast(40.78, -73.97, "America/New_York", today, "high")
    time.sleep(0.02)
    b = wd.daily_extreme_forecast(40.78, -73.97, "America/New_York", today, "high")
    assert b.lead_hours < a.lead_hours


# ------------------------------------------------------- the estimator
def _estimator(routes, **nc):
    from kalshibot.estimators.weather import WeatherTemperatureEstimator
    wd, _m, _s = _wd(routes)
    cfg = {"series": [{"ticker_prefix": "KXHIGHNY", "latitude": 40.78, "longitude": -73.97,
                       "timezone": "America/New_York", "kind": "high", "station": "KNYC"}],
           "nowcast": {"max_lead_hours": 30, **nc}}
    return WeatherTemperatureEstimator(cfg, data=wd)


def _market(day):
    from kalshibot.models import Market
    tag = day.strftime("%y%b%d").upper()
    return Market(ticker=f"KXHIGHNY-{tag}-B70.5", event_ticker=f"KXHIGHNY-{tag}",
                  series_ticker="KXHIGHNY", title="", subtitle="70 to 71",
                  strike_type="between", floor_strike=70, cap_strike=71)


def _ens_today():
    """The ensemble fixture re-dated to today, so the ensemble path works."""
    today = _now_ny().date().isoformat()
    h = ENSEMBLE["hourly"]
    return {"hourly": {k: ([today + t[10:] for t in v] if k == "time" else v)
                       for k, v in h.items()}}


def test_a_same_day_market_is_priced_by_the_nowcast_with_the_ensemble_beside_it():
    routes = {"/v1/ensemble": _ens_today(), "/v1/forecast": _hrrr_payload(),
              "meta.json": META, "/observations": _obs_payload()}
    est = _estimator(routes)
    e = est.estimate(_market(_now_ny().date()))
    ev = e.evidence
    assert e.usable and ev["model"] == "nowcast"
    assert ev["alt_model"] == "ensemble" and 0 < ev["alt_probability"] < 1
    assert ev["nowcast"]["hrrr_run"] and ev["nowcast"]["sigma_f"] > 0
    assert ev["degraded_source"] is False


def test_a_future_day_is_still_priced_by_the_ensemble():
    tomorrow = _now_ny().date() + dt.timedelta(days=1)
    h = ENSEMBLE["hourly"]
    ens = {"hourly": {k: ([tomorrow.isoformat() + t[10:] for t in v] if k == "time" else v)
                      for k, v in h.items()}}
    est = _estimator({"/v1/ensemble": ens, "/v1/forecast": _hrrr_payload(), "meta.json": META})
    ev = est.estimate(_market(tomorrow)).evidence
    assert ev["model"] == "ensemble" and ev["alt_probability"] is None


def test_hrrr_down_falls_back_to_the_ensemble():
    routes = {"/v1/ensemble": _ens_today(), "/v1/forecast": RuntimeError("down"),
              "meta.json": META, "/observations": _obs_payload()}
    ev = _estimator(routes).estimate(_market(_now_ny().date())).evidence
    assert ev["model"] == "ensemble"


def test_the_nowcast_can_be_switched_off():
    routes = {"/v1/ensemble": _ens_today(), "/v1/forecast": _hrrr_payload(),
              "meta.json": META, "/observations": _obs_payload()}
    ev = _estimator(routes, enabled=False).estimate(_market(_now_ny().date())).evidence
    assert ev["model"] == "ensemble"


def test_the_model_and_the_alternative_are_journalled():
    import tempfile
    from kalshibot.journal import Journal
    with tempfile.TemporaryDirectory() as tmp:
        j = Journal(Path(tmp) / "j.db")
        j.log_probability(strategy="forecast:w", estimator="w", series="KXHIGHNY",
                          ticker="T", our_probability=0.3, model="nowcast",
                          alt_probability=0.45)
        r = j.conn.execute("SELECT model, alt_probability FROM probability_log").fetchone()
        assert (r["model"], r["alt_probability"]) == ("nowcast", 0.45)


def test_the_station_model_report_runs_on_an_empty_and_a_scored_journal():
    import io
    import tempfile
    from contextlib import redirect_stdout
    from kalshibot.journal import Journal
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import fit_station_model as fsm
    with tempfile.TemporaryDirectory() as tmp:
        j = Journal(Path(tmp) / "j.db")
        for i in range(5):
            j.log_forecast(series="KXHIGHNY", station="KNYC", target_date=f"2026-09-2{i}",
                           mean_f=70.0, sigma_f=1.6, members=100, lead_hours=5.0)
        j.conn.execute("UPDATE forecasts SET actual_f = 71, actual_source = 'nws_climate_report'")
        j.log_probability(strategy="f", estimator="w", series="KXHIGHNY", ticker="T",
                          our_probability=0.8, model="nowcast", alt_probability=0.4)
        j.conn.execute("UPDATE probability_log SET settled_yes = 1")
        j.conn.commit()
        out = io.StringIO()
        with redirect_stdout(out):
            sys.argv = ["fit_station_model.py", "--db", str(Path(tmp) / "j.db")]
            assert fsm.main() == 0
        text = out.getvalue()
        assert "KNYC" in text and "too few days" in text and "nowcast 0.0400" in text
