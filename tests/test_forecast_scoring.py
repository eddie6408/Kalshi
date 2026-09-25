"""Scoring forecasts against the temperature that actually happened.

The table this fills sat empty for eight days while Miami was forecast four
degrees too cool, so the rules worth pinning are the ones that decide whether
a number gets written down at all.
"""
import datetime as dt
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.forecast_scoring import (SETTLE_AFTER_HOURS, bias_by_station,
                                        observed_daily_max, score_pending)
from kalshibot.journal import Journal

TZ = {"KXHIGHMIA": "America/New_York", "KXHIGHLAX": "America/Los_Angeles"}


class FakeResponse:
    def __init__(self, temps_c): self.temps_c = temps_c
    def raise_for_status(self): pass
    def json(self):
        return {"features": [{"properties": {"temperature": {"value": t}}}
                             for t in self.temps_c]}


class FakeSession:
    """Returns a fixed set of observations, and records what was asked for."""

    def __init__(self, temps_c=(30.0, 32.2, 31.5, 29.0), fail=False):
        self.temps_c, self.fail = list(temps_c), fail
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if self.fail:
            raise RuntimeError("api.weather.gov is down")
        return FakeResponse(self.temps_c)


def _journal_with(tmp, rows):
    j = Journal(Path(tmp) / "j.db")
    for series, station, target, mean in rows:
        j.log_forecast(series=series, station=station, target_date=target,
                       mean_f=mean, sigma_f=1.6, members=122, lead_hours=12.0)
    return j


# ---------------- reading an observation ----------------
def test_celsius_becomes_fahrenheit_and_the_max_wins():
    s = FakeSession([30.0, 32.2, 31.5, 29.0])          # 32.2C = 89.96F
    got = observed_daily_max(s, "KMIA", "America/New_York", dt.date(2026, 9, 20))
    assert got is not None and abs(got - 89.96) < 0.05


def test_a_lone_spike_is_not_recorded_as_the_days_high():
    # 38C among a day of 30C is a sensor fault, not weather. Writing it down
    # as fact would poison every bias number computed afterwards.
    s = FakeSession([30.0, 30.2, 29.8, 30.1, 38.0])
    assert observed_daily_max(s, "KMIA", "America/New_York",
                              dt.date(2026, 9, 20)) is None


def test_a_normal_afternoon_peak_is_kept():
    """The guard must not throw away good days. Hourly readings around a real
    maximum sit a degree or two apart, which is not a spike."""
    s = FakeSession([30.0, 30.2, 29.8, 30.1, 31.5])    # 31.5C = 88.7F
    got = observed_daily_max(s, "KMIA", "America/New_York", dt.date(2026, 9, 20))
    assert got is not None and abs(got - 88.7) < 0.1


def test_a_short_series_of_readings_is_trusted_as_it_stands():
    """With four observations there is not enough shape to call anything an
    outlier, and refusing would leave the day unscored forever."""
    s = FakeSession([30.0, 38.0])
    got = observed_daily_max(s, "KMIA", "America/New_York", dt.date(2026, 9, 20))
    assert got is not None and abs(got - 100.4) < 0.1


def test_a_failed_lookup_returns_nothing_rather_than_guessing():
    assert observed_daily_max(FakeSession(fail=True), "KMIA",
                              "America/New_York", dt.date(2026, 9, 20)) is None


# ---------------- deciding what is ready to score ----------------
def test_a_day_that_is_not_over_yet_is_left_alone():
    with tempfile.TemporaryDirectory() as tmp:
        today = dt.date(2026, 9, 22)
        j = _journal_with(tmp, [("KXHIGHMIA", "KMIA", today.isoformat(), 85.0)])
        s = FakeSession()
        now = dt.datetime(2026, 9, 22, 18, 0, tzinfo=dt.timezone.utc)
        updated, looked = score_pending(j.conn, s, TZ, now=now)
        assert (updated, looked) == (0, 0)
        assert s.calls == []            # not even asked for


def test_the_reporting_margin_is_respected():
    """Midnight local is not "the day is over" - the station is still
    reporting. Scoring then writes down a truncated maximum as fact."""
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal_with(tmp, [("KXHIGHMIA", "KMIA", "2026-09-21", 85.0)])
        s = FakeSession()
        # 00:30 Eastern on the 22nd: the 21st has just ended
        just_after = dt.datetime(2026, 9, 22, 4, 30, tzinfo=dt.timezone.utc)
        assert score_pending(j.conn, s, TZ, now=just_after) == (0, 0)

        well_after = just_after + dt.timedelta(hours=SETTLE_AFTER_HOURS + 1)
        updated, looked = score_pending(j.conn, s, TZ, now=well_after)
        assert updated == 1 and looked == 1


def test_one_lookup_serves_every_forecast_for_that_station_day():
    """470 forecast rows are only a couple of dozen station-days. Fetching
    per row would be twenty times the requests for the same answer."""
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal_with(tmp, [("KXHIGHMIA", "KMIA", "2026-09-20", 85.0)] * 8)
        s = FakeSession()
        now = dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.timezone.utc)
        updated, looked = score_pending(j.conn, s, TZ, now=now)
        assert updated == 8            # all eight rows filled
        assert looked == 1             # from one lookup
        assert len(s.calls) == 1


def test_scoring_is_not_repeated_once_a_row_is_filled():
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal_with(tmp, [("KXHIGHMIA", "KMIA", "2026-09-20", 85.0)])
        s = FakeSession()
        now = dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.timezone.utc)
        assert score_pending(j.conn, s, TZ, now=now)[0] == 1
        assert score_pending(j.conn, s, TZ, now=now) == (0, 0)


def test_each_series_is_scored_in_its_own_timezone():
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal_with(tmp, [("KXHIGHLAX", "KLAX", "2026-09-21", 78.0)])
        s = FakeSession()
        # 08:00 UTC on the 22nd is still 01:00 in Los Angeles - the margin
        # has not elapsed there even though it has on the east coast.
        now = dt.datetime(2026, 9, 22, 8, 0, tzinfo=dt.timezone.utc)
        assert score_pending(j.conn, s, TZ, now=now) == (0, 0)


# ---------------- what the filled rows say ----------------
def test_bias_is_reported_with_the_sign_that_reads_correctly():
    """Positive = we forecast WARMER than it turned out. Miami was the
    opposite: predicted 85, observed 90, so bias -5."""
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal_with(tmp, [("KXHIGHMIA", "KMIA", "2026-09-20", 85.0)])
        s = FakeSession([32.2])        # 89.96F, corroboration not required
        now = dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.timezone.utc)
        score_pending(j.conn, s, TZ, now=now)

        rows = bias_by_station(j.conn)
        assert len(rows) == 1
        r = rows[0]
        assert r["series"] == "KXHIGHMIA" and r["station"] == "KMIA"
        assert r["n"] == 1
        assert abs(r["bias_f"] - (85.0 - 89.96)) < 0.05      # about -4.96
        assert r["bias_short_f"] is None                     # 12h lead, not 6


def test_stations_are_ranked_worst_bias_first():
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal_with(tmp, [("KXHIGHLAX", "KLAX", "2026-09-20", 86.0)])
        s = FakeSession([30.0])        # 86.0F -> LAX is exactly right
        now = dt.datetime(2026, 9, 22, 20, 0, tzinfo=dt.timezone.utc)
        score_pending(j.conn, s, TZ, now=now)
        j.log_forecast(series="KXHIGHMIA", station="KMIA",
                       target_date="2026-09-20", mean_f=85.0, sigma_f=1.6,
                       members=122, lead_hours=12.0)
        s2 = FakeSession([32.2])
        score_pending(j.conn, s2, TZ, now=now)

        rows = bias_by_station(j.conn)
        assert [r["series"] for r in rows] == ["KXHIGHMIA", "KXHIGHLAX"]


def test_nothing_scored_yet_is_an_empty_list_not_a_crash():
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal_with(tmp, [("KXHIGHMIA", "KMIA", "2026-09-20", 85.0)])
        assert bias_by_station(j.conn) == []
