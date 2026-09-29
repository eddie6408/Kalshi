"""The weather expansion: station registry, the multi-model blend, lows.

Pinned: the registry adds every series without overriding hand-written ones;
the blend corrects each model's bias at the station, weights by measured
error, re-centres the ensemble without changing its spread, and does nothing
when unfitted or without model values; a daily LOW is scored against the
official minimum.
"""
import json
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest
from kalshibot import weather_blend as wb
from kalshibot.weather_stations import load_series, load_stations

ROOT = Path(__file__).resolve().parents[1]


def test_the_registry_adds_series_and_the_config_wins_on_a_clash():
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "st.json"
        f.write_text(json.dumps({
            "stations": [{"icao": "KATL", "cli": "ATL"}, {"icao": "KORD", "cli": "ORD"}],
            "series": [{"ticker_prefix": "KXHIGHTATL", "station": "KATL", "kind": "high"},
                       {"ticker_prefix": "KXHIGHNY", "station": "WRONG", "kind": "high"}]}))
        cfg = {"series": [{"ticker_prefix": "KXHIGHNY", "station": "KNYC",
                           "climate_report": "NYC"}], "stations_file": str(f)}
        got = {s["ticker_prefix"]: s for s in load_series(cfg)}
        assert got["KXHIGHNY"]["station"] == "KNYC" and "KXHIGHTATL" in got
        assert load_stations(cfg) == {"KATL": "ATL", "KORD": "ORD", "KNYC": "NYC"}


def test_the_shipped_registry_covers_kalshis_stations_high_and_low():
    import yaml
    from kalshibot.config import _StrictLoader
    raw = yaml.load(open(ROOT / "config.yaml"), Loader=_StrictLoader)
    series = load_series(raw["weather"])
    kinds = {}
    for s in series:
        kinds.setdefault(s["station"], set()).add(s.get("kind", "high"))
        assert s.get("latitude") and s.get("timezone") and s.get("climate_report")
    assert len(series) >= 40 and sum(k == {"high", "low"} for k in kinds.values()) >= 20
    assert len(load_stations(raw["weather"])) >= 39


def _params():
    return wb.BlendParams(
        weights={"high": {"ncep_nbm_conus": 0.5, "ncep_hrrr_conus": 0.5}},
        global_bias={"high": {"ncep_nbm_conus": 1.0, "ncep_hrrr_conus": 0.0}},
        station_bias={"high": {"KNYC": {"ncep_nbm_conus": 2.0}}}, alpha=0.25, fitted=True)


def test_the_blend_corrects_bias_and_weights_models():
    b = wb.blend(_params(), "high", "KNYC", {"ncep_nbm_conus": 74.0, "ncep_hrrr_conus": 70.0}, 68.0)
    # NBM 74-2 = 72, HRRR 70: model blend 71; centre .25*68 + .75*71
    assert b.model_blend == pytest.approx(71.0)
    assert b.centre == pytest.approx(0.25 * 68 + 0.75 * 71)
    assert b.spread == pytest.approx(4.0)
    # another station falls back to the global bias
    assert wb.blend(_params(), "high", "KAUS", {"ncep_nbm_conus": 74.0}, None).centre == pytest.approx(73.0)


def test_no_blend_when_unfitted_or_without_values():
    assert wb.blend(wb.BlendParams(), "high", "KNYC", {"ncep_nbm_conus": 70.0}, 68.0) is None
    assert wb.blend(_params(), "high", "KNYC", {}, 68.0) is None
    assert wb.blend(_params(), "low", "KNYC", {"ncep_nbm_conus": 50.0}, 48.0) is None


def test_fitting_learns_bias_and_prefers_the_better_model():
    rows = []
    for i in range(200):
        t = 60 + (i % 20)
        rows.append({"station": "KNYC" if i % 2 else "KATL", "date": f"2026-{7 + i // 100:02d}-{i % 28 + 1:02d}",
                     "kind": "high", "truth": t,
                     "models": {"ncep_nbm_conus": t + 2 + (0.3 if i % 3 else -0.3),
                                "gfs_global": t + (3 if i % 2 else -3)}})
    params, rep = wb.fit(rows, split="2026-08-15")
    w = params["weights"]["high"]
    assert w["ncep_nbm_conus"] > w["gfs_global"]
    assert params["global_bias"]["high"]["ncep_nbm_conus"] == pytest.approx(2.1, abs=0.05)   # +0.3 two days in three, -0.3 one
    assert rep["in_sample"]["high"]["blend"]["rmse"] < 1.0


def test_the_estimator_recentres_the_ensemble_but_keeps_its_spread():
    from kalshibot.estimators.weather import WeatherTemperatureEstimator
    from kalshibot.weatherdata import DailyTempForecast
    e = WeatherTemperatureEstimator({"series": [], "blend": {"enabled": True}})
    e._blend_params, e._blend_at = _params(), 1e18
    class D:
        def model_values(self, *a, **k):
            return {"ncep_nbm_conus": 74.0, "ncep_hrrr_conus": 70.0}
    e.data = D()
    fc = DailyTempForecast(date=date(2026, 9, 29), kind="high", members=[66.0, 68.0, 70.0])
    fcb, b = e._blend({"latitude": 1, "longitude": 1, "station": "KNYC"}, fc, date(2026, 9, 29), "UTC")
    assert b is not None and sum(fcb.members) / 3 == pytest.approx(b.centre)
    assert max(fcb.members) - min(fcb.members) == pytest.approx(4.0)
    assert fc.members == [66.0, 68.0, 70.0]                  # the cached forecast untouched


def test_a_daily_low_is_scored_against_the_official_minimum():
    import sqlite3
    from datetime import datetime, timezone
    from kalshibot import forecast_scoring as fs
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE forecasts (id INTEGER PRIMARY KEY, series TEXT, station TEXT, "
                 "target_date TEXT, actual_f REAL, actual_source TEXT)")
    conn.execute("INSERT INTO forecasts (series, station, target_date) VALUES "
                 "('KXLOWTNYC','KNYC','2026-09-27'), ('KXHIGHNY','KNYC','2026-09-27')")
    asked = []

    def official(session, location, target, kind, monitor=None):
        asked.append(kind)
        return (58.0 if kind == "low" else 63.0), None
    orig = fs.official_daily_extreme
    fs.official_daily_extreme = official
    try:
        fs.score_pending(conn, None, {"KXLOWTNYC": "America/New_York", "KXHIGHNY": "America/New_York"},
                         now=datetime(2026, 9, 29, 12, tzinfo=timezone.utc),
                         cli_by_station={"KNYC": "NYC"},
                         kind_by_series={"KXLOWTNYC": "low", "KXHIGHNY": "high"})
    finally:
        fs.official_daily_extreme = orig
    got = dict(conn.execute("SELECT series, actual_f FROM forecasts").fetchall())
    assert got == {"KXLOWTNYC": 58.0, "KXHIGHNY": 63.0} and sorted(asked) == ["high", "low"]
