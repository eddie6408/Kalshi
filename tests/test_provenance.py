"""Package 14A phase 2: every number that reaches a decision can be traced.

"Where did this number come from?" has to have an answer in the journal:
who published it, when they produced it, when we fetched it, what it said.
And scoring has to use what Kalshi actually settles on - the final NWS Daily
Climate Report - not an approximation of it.
"""
import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.forecast_scoring import (official_daily_extreme,
                                        parse_climate_report, score_pending)
from kalshibot.journal import Journal
from kalshibot.sources import (DEGRADED, HEALTHY, SourceMonitor,
                               SourceRegistry)
from kalshibot.weatherdata import WeatherData

FINAL_NYC = """
000
CDUS41 KOKX 250633
CLINYC

CLIMATE REPORT
NATIONAL WEATHER SERVICE NEW YORK, NY
233 AM EDT FRI SEP 25 2026

...THE CENTRAL PARK NY CLIMATE SUMMARY FOR SEPTEMBER 24 2026...

WEATHER ITEM   OBSERVED TIME   RECORD YEAR NORMAL DEPARTURE LAST
TEMPERATURE (F)
 YESTERDAY
  MAXIMUM         66    305 PM  91    2017  73     -7       80
  MINIMUM         53    644 AM  40    1963  60     -7       70
  AVERAGE         60                        66     -6       75

PRECIPITATION (IN)
  YESTERDAY        0.00          2.26 1975   0.14  -0.14     0.08
"""

PRELIM_NYC = FINAL_NYC.replace(
    "SUMMARY FOR SEPTEMBER 24 2026...",
    "SUMMARY FOR SEPTEMBER 24 2026...\nVALID TODAY AS OF 0400 PM LOCAL TIME.",
).replace("  MAXIMUM         66", "  MAXIMUM         64")


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self._p


class RouteSession:
    """Answers GETs from a {url-substring: payload} table, first match wins."""

    def __init__(self, routes, headers=None):
        self.routes = routes
        self.headers = headers or {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        for key, payload in self.routes.items():
            if key in url:
                if isinstance(payload, Exception):
                    raise payload
                if isinstance(payload, _Resp):
                    return payload
                return _Resp(payload)
        return _Resp({}, 404)


def _cli_routes(products):
    """products: [(id, issuanceTime, text)] newest first."""
    routes = {"/products/types/CLI/locations/": {"@graph": [
        {"id": pid, "issuanceTime": t, "issuingOffice": "KOKX"}
        for pid, t, _ in products]}}
    for pid, _t, text in products:
        routes[f"/products/{pid}"] = {"productText": text}
    return routes


class _Sink:
    def __init__(self):
        self.rows = []

    def __call__(self, **row):
        self.rows.append(row)
        return len(self.rows)


# ------------------------------------------------------ climate report parser
def test_the_final_report_parses():
    rep = parse_climate_report(FINAL_NYC)
    assert rep == {"date": dt.date(2026, 9, 24), "preliminary": False,
                   "max_f": 66, "min_f": 53}


def test_the_afternoon_report_is_marked_preliminary():
    rep = parse_climate_report(PRELIM_NYC)
    assert rep["preliminary"] is True and rep["max_f"] == 64


def test_missing_and_record_values():
    assert parse_climate_report(FINAL_NYC.replace("MAXIMUM         66",
                                                  "MAXIMUM         MM"))["max_f"] is None
    assert parse_climate_report(FINAL_NYC.replace("MAXIMUM         66",
                                                  "MAXIMUM         95R"))["max_f"] == 95
    assert parse_climate_report("not a climate report") is None


def test_official_extreme_skips_the_preliminary_and_records_provenance():
    sink, mon = _Sink(), SourceMonitor()
    mon.set_sink(sink)
    s = RouteSession(_cli_routes([
        ("p2", "2026-09-25T06:33:00+00:00", FINAL_NYC),
        ("p1", "2026-09-24T20:37:00+00:00", PRELIM_NYC),
    ]))
    value, oid = official_daily_extreme(s, "NYC", dt.date(2026, 9, 24), "high", mon)
    assert value == 66.0 and oid == 1
    row = sink.rows[0]
    assert row["source_id"] == "nws_climate_report"
    assert row["data_time"] == "2026-09-25T06:33:00+00:00"
    assert row["value_json"]["final"] is True
    assert mon.state("nws_climate_report") == HEALTHY


def test_only_a_preliminary_report_means_no_value_yet():
    s = RouteSession(_cli_routes([("p1", "2026-09-24T20:37:00+00:00", PRELIM_NYC)]))
    assert official_daily_extreme(s, "NYC", dt.date(2026, 9, 24), "high",
                                  SourceMonitor()) == (None, None)


# ------------------------------------------------------------- scoring
def _journal_with_forecast(tmp, date="2026-09-24"):
    j = Journal(Path(tmp) / "j.db")
    j.log_forecast(series="KXHIGHNY", station="KNYC", target_date=date,
                   mean_f=67.0, sigma_f=1.6, members=122, lead_hours=5.0,
                   source="open-meteo-ensemble", provenance=[4, 9])
    return j


TZ = {"KXHIGHNY": "America/New_York"}
CLI = {"KNYC": "NYC"}


def test_scoring_uses_the_climate_report_and_says_so():
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal_with_forecast(tmp)
        s = RouteSession(_cli_routes([("p2", "2026-09-25T06:33:00+00:00", FINAL_NYC)]))
        now = dt.datetime(2026, 9, 25, 12, 0, tzinfo=dt.timezone.utc)
        assert score_pending(j.conn, s, TZ, now=now, cli_by_station=CLI,
                             monitor=SourceMonitor())[0] == 1
        row = j.conn.execute("SELECT actual_f, actual_source, source, provenance "
                             "FROM forecasts").fetchone()
        assert tuple(row) == (66.0, "nws_climate_report", "open-meteo-ensemble", "4,9")
        assert not any("/stations/" in c for c in s.calls)


def test_scoring_waits_for_the_report_before_falling_back():
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal_with_forecast(tmp)
        routes = _cli_routes([])
        routes["/stations/KNYC/observations"] = {"features": [
            {"properties": {"temperature": {"value": v}}}
            for v in (18.0, 18.3, 18.9, 17.2, 16.0)]}
        s = RouteSession(routes)
        early = dt.datetime(2026, 9, 25, 12, 0, tzinfo=dt.timezone.utc)
        assert score_pending(j.conn, s, TZ, now=early, cli_by_station=CLI,
                             monitor=SourceMonitor())[0] == 0
        late = dt.datetime(2026, 9, 26, 12, 0, tzinfo=dt.timezone.utc)
        assert score_pending(j.conn, s, TZ, now=late, cli_by_station=CLI,
                             monitor=SourceMonitor())[0] == 1
        row = j.conn.execute("SELECT actual_f, actual_source FROM forecasts").fetchone()
        assert row["actual_source"] == "nws_observations"
        assert abs(row["actual_f"] - (18.9 * 9 / 5 + 32)) < 1e-6


# --------------------------------------------------- weather provenance
ENSEMBLE = {"hourly": {
    "time": [f"2026-09-25T{h:02d}:00" for h in range(24)],
    **{f"temperature_2m_member{i:02d}": [70.0 + i * 0.1 + (h == 15) * 5 for h in range(24)]
       for i in range(10)},
}}
META = {"last_run_initialisation_time": 1790316000,       # 2026-09-25T06:00Z
        "last_run_availability_time": 1790336408}
GRID = {"properties": {"updateTime": "2026-09-25T06:36:45+00:00",
                       "maxTemperature": {"values": [
                           {"validTime": "2026-09-25T10:00:00+00:00/PT13H", "value": 24.0}]}}}
POINT = {"properties": {"forecastGridData": "https://api.weather.gov/gridpoints/OKX/34,45"}}


def _weather(routes, **kw):
    sink, mon = _Sink(), SourceMonitor()
    mon.set_sink(sink)
    wd = WeatherData(monitor=mon, **kw)
    wd._session = RouteSession(routes, wd._session.headers)
    return wd, mon, sink


def test_an_ensemble_forecast_carries_its_provenance():
    wd, mon, sink = _weather({"/v1/ensemble": ENSEMBLE, "meta.json": META})
    fc = wd.daily_extreme_forecast(40.78, -73.97, "America/New_York",
                                   dt.date(2026, 9, 25), "high")
    assert fc.source == "open-meteo-ensemble" and fc.n_members == 10
    assert fc.provenance == [1]
    assert fc.retrieved_at is not None and fc.data_time is not None
    assert fc.vintage["gfs_seamless"]["run"].startswith("2026-09-25T06:00")
    row = sink.rows[0]
    assert row["source_id"] == "open_meteo_ensemble"
    assert row["value_json"]["members"] == 10
    assert mon.state("open_meteo_ensemble") == HEALTHY


def test_the_cross_check_is_recorded_but_does_not_change_the_forecast():
    routes = {"/v1/ensemble": ENSEMBLE, "meta.json": META,
              "/points/": POINT, "/gridpoints/": GRID}
    wd, _mon, sink = _weather(routes, cross_check=True)
    fc = wd.daily_extreme_forecast(40.78, -73.97, "America/New_York",
                                   dt.date(2026, 9, 25), "high")
    assert fc.source == "open-meteo-ensemble" and fc.n_members == 10
    assert abs(fc.cross_check_f - (24.0 * 9 / 5 + 32)) < 1e-6
    assert fc.cross_check_source == "nws-gridpoint"
    assert [r["source_id"] for r in sink.rows] == ["open_meteo_ensemble", "nws_gridpoint"]
    assert fc.provenance == [1, 2]


def test_a_200_with_no_usable_members_counts_as_a_failed_source():
    empty = {"hourly": {"time": [], "temperature_2m": []}}
    wd, mon, sink = _weather({"/v1/ensemble": empty, "meta.json": META})
    assert wd._from_ensemble(40.78, -73.97, "America/New_York",
                             dt.date(2026, 9, 25), "high") is None
    assert mon.state("open_meteo_ensemble") == DEGRADED
    assert sink.rows == []


def test_model_run_metadata_failing_never_breaks_the_forecast():
    wd, mon, _sink = _weather({"/v1/ensemble": ENSEMBLE,
                               "meta.json": RuntimeError("meta down")})
    fc = wd.daily_extreme_forecast(40.78, -73.97, "America/New_York",
                                   dt.date(2026, 9, 25), "high")
    assert fc is not None and fc.vintage == {} and fc.data_time is None
    assert mon.state("open_meteo_model_runs") != HEALTHY


def test_the_estimate_evidence_states_what_was_known_and_when():
    from kalshibot.estimators.weather import WeatherTemperatureEstimator
    from kalshibot.models import Market
    routes = {"/v1/ensemble": ENSEMBLE, "meta.json": META,
              "/points/": POINT, "/gridpoints/": GRID}
    wd, _mon, _sink = _weather(routes, cross_check=True)
    est = WeatherTemperatureEstimator(
        {"series": [{"ticker_prefix": "KXHIGHNY", "latitude": 40.78,
                     "longitude": -73.97, "timezone": "America/New_York",
                     "kind": "high"}]}, data=wd)
    m = Market(ticker="KXHIGHNY-26SEP25-B75.5", event_ticker="KXHIGHNY-26SEP25",
               series_ticker="KXHIGHNY", title="", subtitle="75 to 76",
               strike_type="between", floor_strike=75, cap_strike=76)
    ev = est.estimate(m).evidence
    for key in ("provenance_ids", "data_retrieved_at", "fetch_age_seconds",
                "forecast_published_at", "forecast_age_seconds",
                "forecast_vintage", "cross_check_f", "cross_check_diff_f"):
        assert key in ev, key
    assert ev["provenance_ids"] == [1, 2]
    json.dumps(ev, default=str)            # the journal must be able to store it


# ------------------------------------------------------------- journal
def test_journal_records_and_links_source_observations():
    with tempfile.TemporaryDirectory() as tmp:
        j = Journal(Path(tmp) / "j.db")
        mon = SourceMonitor(SourceRegistry.from_config({"registry": {
            "nws_observations": {"publisher": "NOAA NWS", "tier": 1,
                                 "roles": ["observation"], "endpoint": "x"}}}))
        mon.set_sink(j.log_source_observation)
        oid = mon.observe("nws_observations", data_type="observed_high_so_far_f",
                          subject="KNYC", value=71.2,
                          value_json={"readings": 9}, data_time="2026-09-25T13:51:00+00:00")
        row = j.conn.execute("SELECT * FROM source_observations WHERE id=?", (oid,)).fetchone()
        assert row["publisher"] == "NOAA NWS" and row["tier"] == 1
        assert json.loads(row["value_json"]) == {"readings": 9}
        assert row["retrieved_at"]


def test_opening_an_old_journal_adds_the_14a_columns_without_touching_rows():
    import sqlite3
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "old.db"
        c = sqlite3.connect(path)
        c.execute("""CREATE TABLE forecasts (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     ts TEXT, series TEXT, station TEXT, target_date TEXT,
                     predicted_mean_f REAL, predicted_sigma_f REAL, members INTEGER,
                     lead_hours REAL, actual_f REAL)""")
        c.execute("INSERT INTO forecasts (ts, series, station, target_date, "
                  "predicted_mean_f, actual_f) VALUES ('t','KXHIGHNY','KNYC',"
                  "'2026-09-20',70,71)")
        c.commit()
        c.close()
        j = Journal(path)
        Journal(path)                                  # idempotent
        cols = {r[1] for r in j.conn.execute("PRAGMA table_info(forecasts)")}
        assert {"source", "provenance", "actual_source"} <= cols
        assert j.conn.execute("SELECT actual_f FROM forecasts").fetchone()[0] == 71


def test_source_health_is_saved_for_the_monitor_to_read():
    with tempfile.TemporaryDirectory() as tmp:
        j = Journal(Path(tmp) / "j.db")
        mon = SourceMonitor()
        mon.record("nws_observations", True, status=200, latency_ms=120.0)
        j.save_source_health(mon.snapshot())
        mon.record("nws_observations", False, error="timeout")
        j.save_source_health(mon.snapshot())
        rows = j.conn.execute("SELECT * FROM source_health").fetchall()
        assert len(rows) == 1 and rows[0]["state"] == DEGRADED
        assert rows[0]["calls"] == 2


def test_a_changed_settlement_source_is_reported():
    with tempfile.TemporaryDirectory() as tmp:
        j = Journal(Path(tmp) / "j.db")
        twc = [{"name": "The Weather Company", "url": "https://weather.com/kalshi"}]
        assert j.record_settlement_sources("KXHIGHNY", twc) == ([], [])   # first sighting
        assert j.record_settlement_sources("KXHIGHNY", twc) == ([], [])   # unchanged
        added, dropped = j.record_settlement_sources(
            "KXHIGHNY", [{"name": "Someone Else", "url": "https://x"}])
        assert added == [("Someone Else", "https://x")]
        assert dropped == [("The Weather Company", "https://weather.com/kalshi")]


# ------------------------------------------------------------- dashboard
def test_the_dashboard_shows_sources_and_what_was_behind_a_decision():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import sqlite3
    import dashboard
    with tempfile.TemporaryDirectory() as tmp:
        # A journal from before 14A: the section is empty, not an error.
        old = Path(tmp) / "old.db"
        sqlite3.connect(old).execute("CREATE TABLE runs (id INTEGER)").connection.commit()
        conn = sqlite3.connect(old)
        conn.row_factory = sqlite3.Row
        assert dashboard._sources(conn) == {"sources": [], "settlement": [],
                                            "behind_latest": None}

        j = Journal(Path(tmp) / "j.db")
        mon = SourceMonitor()
        mon.set_sink(j.log_source_observation)
        mon.record("nws_observations", True)
        a = mon.observe("nws_observations", value=71.0, data_type="observed_high_so_far_f")
        j.save_source_health(mon.snapshot())
        j.conn.execute(
            "INSERT INTO opportunities (ts, strategy, tickers, decision, evidence) "
            "VALUES ('t', 'forecast:weather_temperature', 'KXHIGHNY-X', 'REJECT', ?)",
            (json.dumps({"provenance_ids": [a], "probability_yes": 0.4}),))
        j.conn.commit()
        got = dashboard._sources(j.conn)
        assert got["sources"][0]["state"] == HEALTHY
        assert [r["id"] for r in got["behind_latest"]["rows"]] == [a]
