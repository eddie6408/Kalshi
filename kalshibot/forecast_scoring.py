"""Score past forecasts against what the weather actually did.

`probability_log` answers "was the probability right". This answers the
question underneath it: **was the temperature right**. They are not the same
question, and only this one can tell you that a particular station is being
forecast four degrees too cool.

It was written on 14 September, wired to nothing, and by 22 September
`forecasts.actual_f` was NULL on all 470 rows - so Miami ran a +4.06F cold
bias for eight days with the one table built to catch it sitting empty. Hence
this module: the scoring runs on the bot's own clock now, not on someone
remembering to double-click something.

Two entry points:

    score_pending()    fill actual_f for past days
    bias_by_station()  what the filled rows say, for the dashboard
"""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

#: A local day is only safely "over" once the station has had time to report
#: its last observations. Scoring at 00:01 local gets a truncated maximum and
#: writes it down as fact, which is worse than waiting.
SETTLE_AFTER_HOURS = 6.0

#: A maximum this far clear of every other reading is a sensor fault.
SPIKE_GAP_F = 5.0

OBS_URL = "https://api.weather.gov/stations/{station}/observations"


def observed_daily_max(session, station: str, tz_name: str,
                       target: dt.date) -> float | None:
    """Highest temperature observed at the station over one local day."""
    tz = ZoneInfo(tz_name)
    start = dt.datetime.combine(target, dt.time(0, 0), tzinfo=tz)
    end = start + dt.timedelta(days=1)
    try:
        resp = session.get(
            OBS_URL.format(station=station),
            params={"start": start.astimezone(dt.timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end": end.astimezone(dt.timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "limit": 500},
            timeout=30)
        resp.raise_for_status()
        feats = resp.json().get("features", [])
    except Exception as exc:  # noqa: BLE001
        log.debug("observation lookup failed for %s %s: %s", station, target, exc)
        return None

    temps = []
    for f in feats:
        v = ((f.get("properties") or {}).get("temperature") or {}).get("value")
        if v is not None:
            temps.append(float(v) * 9 / 5 + 32)
    if not temps:
        return None
    # A single spurious reading would become the day's high and never be
    # questioned again, poisoning every bias number computed afterwards.
    #
    # The guard is aimed at a sensor spike, not at a normal afternoon peak:
    # hourly observations around the real maximum sit a degree or two apart,
    # so requiring tight corroboration would throw away good days. Only an
    # isolated reading more than five degrees clear of the next highest is
    # refused.
    temps.sort(reverse=True)
    if len(temps) >= 5 and temps[0] - temps[1] > SPIKE_GAP_F:
        log.warning("SCORING  %s %s: %.1fF is %.1fF clear of the next "
                    "reading (%.1fF) - looks like a sensor spike, not "
                    "recording it", station, target, temps[0],
                    temps[0] - temps[1], temps[1])
        return None
    return temps[0]


def score_pending(conn: sqlite3.Connection, session, tz_by_series: dict,
                  now: dt.datetime | None = None, limit: int = 40) -> tuple[int, int]:
    """Fill actual_f for every forecast whose target day is safely over.

    Returns (rows updated, days looked up). Observations are fetched once per
    (station, day) however many forecasts point at it - 470 forecast rows are
    only a couple of dozen station-days.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    rows = conn.execute(
        "SELECT id, series, station, target_date FROM forecasts "
        "WHERE actual_f IS NULL").fetchall()
    if not rows:
        return 0, 0

    wanted: dict[tuple[str, str], list] = {}
    for r in rows:
        try:
            target = dt.date.fromisoformat(r["target_date"])
        except (TypeError, ValueError):
            continue
        tz_name = tz_by_series.get(r["series"], "America/New_York")
        # Is the target day over, with the reporting margin?
        tz = ZoneInfo(tz_name)
        day_end = dt.datetime.combine(
            target + dt.timedelta(days=1), dt.time(0, 0), tzinfo=tz)
        if now < day_end + dt.timedelta(hours=SETTLE_AFTER_HOURS):
            continue
        wanted.setdefault((r["station"], tz_name, target), []).append(r["id"])

    updated = looked_up = 0
    for (station, tz_name, target), ids in list(wanted.items())[:limit]:
        actual = observed_daily_max(session, station, tz_name, target)
        looked_up += 1
        if actual is None:
            continue
        conn.executemany("UPDATE forecasts SET actual_f=? WHERE id=?",
                         [(actual, i) for i in ids])
        updated += len(ids)
        log.info("SCORING  %s %s: observed %.1fF (%d forecast%s)",
                 station, target, actual, len(ids), "" if len(ids) == 1 else "s")
    if updated:
        conn.commit()
    return updated, looked_up


def bias_by_station(conn: sqlite3.Connection, since: str | None = None) -> list[dict]:
    """Predicted mean against observed high, per station.

    The number that would have made Miami obvious on day one. Positive bias
    means we forecast WARMER than it turned out; negative means cooler.

    Reports DAYS alongside rows, and computes the uncertainty across days.
    Each station is forecast about six times a day, so six rows for one day
    are one fact; using the row count put Miami's offset at twelve standard
    errors from zero when the honest figure was nearer three, and a bias
    correction built on that number failed walk-forward validation. See
    kalshibot/station_bias.py for the full account.
    """
    sql = ("SELECT series, station, predicted_mean_f, predicted_sigma_f, "
           "actual_f, lead_hours FROM forecasts WHERE actual_f IS NOT NULL")
    params: tuple = ()
    if since:
        sql += " AND ts >= ?"
        params = (since,)

    sql = sql.replace("SELECT series, station,",
                      "SELECT series, station, target_date,")
    groups: dict[tuple[str, str], list] = {}
    for r in conn.execute(sql, params):
        groups.setdefault((r["series"], r["station"]), []).append(
            (float(r["predicted_mean_f"]) - float(r["actual_f"]),
             float(r["lead_hours"] or 0), r["target_date"]))

    out = []
    for (series, station), errs in groups.items():
        e = [x[0] for x in errs]
        n = len(e)
        bias = sum(e) / n
        rmse = (sum(x * x for x in e) / n) ** 0.5
        short = [x[0] for x in errs if x[1] <= 6]

        # Clustered by target date: one day is one observation.
        by_day: dict[str, list[float]] = {}
        for err, _lead, day in errs:
            by_day.setdefault(day, []).append(err)
        daily = [sum(v) / len(v) for v in by_day.values()]
        n_days = len(daily)
        if n_days > 1:
            dm = sum(daily) / n_days
            var = sum((x - dm) ** 2 for x in daily) / (n_days - 1)
            day_sd, stderr = var ** 0.5, (var / n_days) ** 0.5
        else:
            day_sd = stderr = None

        out.append({
            "series": series, "station": station, "n": n, "n_days": n_days,
            "bias_f": bias, "rmse_f": rmse,
            "day_sd_f": day_sd, "stderr_f": stderr,
            "worst_f": max(e, key=abs),
            "n_short": len(short),
            "bias_short_f": (sum(short) / len(short)) if short else None,
            # Can this station's offset be told apart from day-to-day weather?
            "separable": bool(stderr and abs(bias) > 3 * stderr
                              and n_days >= 20),
        })
    out.sort(key=lambda d: -abs(d["bias_f"]))
    return out
