"""Measuring a station's forecast bias, clustered by day.

The correction this was built for was REJECTED by walk-forward validation
(module docstring has the numbers). What survives is the measurement, and the
one thing that measurement has to get right is the unit of observation: a day,
not a row. Each station is forecast about six times a day, so six forecasts
for one day are one fact. Treating them as six put Miami's offset at twelve
standard errors from zero when the honest figure was nearer three.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.journal import Journal
from kalshibot.station_bias import MIN_DAYS, fit_offsets


def _journal(tmp, rows):
    """rows: (station, target_date, predicted, actual) - actual may be None."""
    j = Journal(Path(tmp) / "j.db")
    for station, date, pred, actual in rows:
        j.log_forecast(series="KXTEST", station=station, target_date=date,
                       mean_f=pred, sigma_f=1.6, members=122, lead_hours=6.0)
        if actual is not None:
            j.conn.execute(
                "UPDATE forecasts SET actual_f=? WHERE id=(SELECT MAX(id) "
                "FROM forecasts)", (actual,))
    j.conn.commit()
    return j


def test_six_forecasts_for_one_day_count_as_one_day():
    """The trap, pinned. Six identical rows must not look like six facts."""
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal(tmp, [("KMIA", "2026-09-20", 85.0, 90.0)] * 6)
        b = fit_offsets(j.conn)["KMIA"]
        assert b.n_rows == 6
        assert b.n_days == 1              # <- the whole point
        assert abs(b.offset_f - (-5.0)) < 0.01


def test_the_standard_error_is_computed_across_days():
    """Two days, wildly different. The uncertainty has to show it."""
    with tempfile.TemporaryDirectory() as tmp:
        rows = ([("KMIA", "2026-09-20", 85.0, 90.0)] * 6 +
                [("KMIA", "2026-09-21", 85.0, 80.0)] * 6)
        j = _journal(tmp, rows)
        b = fit_offsets(j.conn)["KMIA"]
        assert b.n_days == 2
        assert abs(b.offset_f) < 0.01     # -5 and +5 average out
        assert b.day_sd_f > 5.0           # and the spread is enormous
        assert not b.trustworthy


def test_too_few_days_is_never_trustworthy_however_many_rows():
    with tempfile.TemporaryDirectory() as tmp:
        rows = []
        for d in range(5):                              # 5 days
            for _ in range(20):                         # 100 rows
                rows.append(("KMIA", f"2026-09-{10+d:02d}", 85.0, 88.0))
        j = _journal(tmp, rows)
        b = fit_offsets(j.conn)["KMIA"]
        assert b.n_rows == 100
        assert b.n_days == 5
        assert not b.trustworthy
        assert "5 days" in b.reason and "rows are not days" in b.reason


def test_a_consistent_offset_over_enough_days_is_trusted():
    with tempfile.TemporaryDirectory() as tmp:
        rows = []
        for d in range(MIN_DAYS + 2):
            wobble = 0.2 if d % 2 else -0.2             # small day-to-day noise
            rows.append(("KMIA", f"2026-09-{d+1:02d}", 85.0 + wobble, 88.0))
        j = _journal(tmp, rows)
        b = fit_offsets(j.conn)["KMIA"]
        assert b.n_days == MIN_DAYS + 2
        assert b.trustworthy, b.reason
        assert abs(b.offset_f - (-3.0)) < 0.1


def test_a_wild_offset_is_refused_rather_than_clipped():
    """Past the cap it is a station or data problem. Quietly subtracting five
    degrees of it would bury exactly the thing worth finding."""
    with tempfile.TemporaryDirectory() as tmp:
        rows = [("KMIA", f"2026-09-{d+1:02d}", 85.0, 100.0)
                for d in range(MIN_DAYS + 2)]
        j = _journal(tmp, rows)
        b = fit_offsets(j.conn)["KMIA"]
        assert not b.trustworthy
        assert "cap" in b.reason


def test_as_of_excludes_the_day_being_forecast_and_everything_after():
    """A bias fitted on the day you are predicting is not a correction."""
    with tempfile.TemporaryDirectory() as tmp:
        rows = [("KMIA", "2026-09-20", 85.0, 88.0),
                ("KMIA", "2026-09-21", 85.0, 88.0),
                ("KMIA", "2026-09-22", 85.0, 999.0)]   # must never be seen
        j = _journal(tmp, rows)
        b = fit_offsets(j.conn, as_of="2026-09-22")["KMIA"]
        assert b.n_days == 2
        assert abs(b.offset_f - (-3.0)) < 0.01


def test_unscored_forecasts_are_ignored():
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal(tmp, [("KMIA", "2026-09-20", 85.0, 88.0),
                           ("KMIA", "2026-09-21", 85.0, None)])
        b = fit_offsets(j.conn)["KMIA"]
        assert b.n_days == 1


def test_stations_are_measured_separately():
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal(tmp, [("KMIA", "2026-09-20", 85.0, 88.0),
                           ("KNYC", "2026-09-20", 70.0, 69.0)])
        got = fit_offsets(j.conn)
        assert abs(got["KMIA"].offset_f - (-3.0)) < 0.01
        assert abs(got["KNYC"].offset_f - (+1.0)) < 0.01


def test_no_history_is_an_empty_answer_not_a_crash():
    with tempfile.TemporaryDirectory() as tmp:
        j = Journal(Path(tmp) / "j.db")
        assert fit_offsets(j.conn) == {}
