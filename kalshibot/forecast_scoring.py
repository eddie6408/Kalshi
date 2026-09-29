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

Where "what actually happened" comes from (package 14A)
--------------------------------------------------------
Kalshi's rules settle each market on a named NWS Daily Climate Report -
"the maximum temperature recorded at New York City (CLINYC)" - as published
by The Weather Company. So the truth is that report, read at its origin:

    1. NWS Daily Climate Report (CLI), the FINAL one for the day. Not the
       "VALID TODAY AS OF 4 PM" preliminary: that is a partial day.
    2. Only if no final report exists FALLBACK_AFTER_HOURS after the day
       ends: the maximum of the station's hourly observations, and the row
       says so (forecasts.actual_source).

The report's day runs midnight to midnight local STANDARD time, so during
daylight saving it ends at 1 AM - which the hourly-observation fallback, on
the local calendar day, does not match exactly. One more reason it is the
fallback.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import sqlite3
from zoneinfo import ZoneInfo

from . import sources as _sources

log = logging.getLogger(__name__)

#: A local day is only safely "over" once the station has had time to report
#: its last observations. Scoring at 00:01 local gets a truncated maximum and
#: writes it down as fact, which is worse than waiting.
SETTLE_AFTER_HOURS = 6.0

#: A maximum this far clear of every other reading is a sensor fault.
SPIKE_GAP_F = 5.0

OBS_URL = "https://api.weather.gov/stations/{station}/observations"
CLI_LIST_URL = "https://api.weather.gov/products/types/CLI/locations/{location}"
CLI_PRODUCT_URL = "https://api.weather.gov/products/{product_id}"

#: How long after the day ends to wait for the final climate report before
#: settling for hourly observations. Reports normally arrive by morning.
FALLBACK_AFTER_HOURS = 30.0

#: At most this many report texts are fetched per lookup. The list is newest
#: first and there are two or three a day, so a week back is well inside it.
MAX_REPORTS_READ = 12

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY",
     "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"])}
_SUMMARY_RE = re.compile(r"CLIMATE SUMMARY FOR\s+([A-Z]+)\s+(\d{1,2})\s+(\d{4})")


def parse_climate_report(text: str) -> dict | None:
    """Read the date, finality and temperature extremes from a CLI product.

    Returns {"date", "preliminary", "max_f", "min_f"} or None if the text is
    not recognisably a daily climate report. "MM" (missing) reads as None.
    """
    if not text:
        return None
    up = text.upper()
    m = _SUMMARY_RE.search(up)
    if not m or m.group(1) not in _MONTHS:
        return None
    try:
        day = dt.date(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)))
    except ValueError:
        return None
    start = up.find("TEMPERATURE (F)")
    if start < 0:
        return None
    end = up.find("PRECIPITATION", start)
    block = up[start:end if end > 0 else None]

    def _first(label: str) -> int | None:
        hit = re.search(rf"^\s*{label}\s+(-?\d+|MM)R?\b", block, re.M)
        if not hit or hit.group(1) == "MM":
            return None
        return int(hit.group(1))

    return {"date": day,
            "preliminary": "VALID TODAY AS OF" in up or "VALID AS OF" in up,
            "max_f": _first("MAXIMUM"),
            "min_f": _first("MINIMUM")}


def official_daily_extreme(session, location: str, target: dt.date,
                           kind: str = "high",
                           monitor: _sources.SourceMonitor | None = None
                           ) -> tuple[float | None, int | None]:
    """The day's official max (or min) from the FINAL NWS climate report.

    Returns (value, provenance id). (None, None) when there is no final
    report for `target` yet - which is normal until the morning after.
    """
    mon = monitor or _sources.MONITOR
    list_url = CLI_LIST_URL.format(location=location)
    try:
        with mon.track("nws_climate_report") as call:
            resp = session.get(list_url, params=None, timeout=30)
            call.status = getattr(resp, "status_code", None)
            resp.raise_for_status()
            products = (resp.json() or {}).get("@graph") or []
    except Exception as exc:  # noqa: BLE001
        log.debug("climate report list failed for %s: %s", location, exc)
        return None, None

    for item in products[:MAX_REPORTS_READ]:
        issued = _sources.to_utc(item.get("issuanceTime"))
        # Newest first; a report issued before the target day began cannot
        # be about it, and neither can anything older.
        if issued is not None and issued.date() < target:
            break
        pid = item.get("id")
        if not pid:
            continue
        try:
            with mon.track("nws_climate_report") as call:
                r = session.get(CLI_PRODUCT_URL.format(product_id=pid),
                                params=None, timeout=30)
                call.status = getattr(r, "status_code", None)
                r.raise_for_status()
                text = (r.json() or {}).get("productText") or ""
                call.data_time = item.get("issuanceTime")
        except Exception as exc:  # noqa: BLE001
            log.debug("climate report %s failed: %s", pid, exc)
            continue
        rep = parse_climate_report(text)
        if not rep or rep["date"] != target or rep["preliminary"]:
            continue
        value = rep["max_f"] if kind == "high" else rep["min_f"]
        if value is None:
            continue
        oid = mon.observe(
            "nws_climate_report", endpoint=CLI_PRODUCT_URL.format(product_id=pid),
            data_type=f"official_daily_{'max' if kind == 'high' else 'min'}_f",
            subject=f"CLI{location}", target_date=target.isoformat(),
            value=float(value),
            value_json={"product_id": pid, "office": item.get("issuingOffice"),
                        "final": True},
            data_time=item.get("issuanceTime"))
        return float(value), oid
    return None, None


def observed_daily_max(session, station: str, tz_name: str,
                       target: dt.date,
                       monitor: _sources.SourceMonitor | None = None) -> float | None:
    """Highest temperature observed at the station over one local day."""
    mon = monitor or _sources.MONITOR
    tz = ZoneInfo(tz_name)
    start = dt.datetime.combine(target, dt.time(0, 0), tzinfo=tz)
    end = start + dt.timedelta(days=1)
    try:
        with mon.track("nws_observations") as call:
            resp = session.get(
                OBS_URL.format(station=station),
                params={"start": start.astimezone(dt.timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "end": end.astimezone(dt.timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "limit": 500},
                timeout=30)
            call.status = getattr(resp, "status_code", None)
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
                  now: dt.datetime | None = None, limit: int = 40,
                  cli_by_station: dict | None = None,
                  monitor: _sources.SourceMonitor | None = None,
                  kind_by_series: dict | None = None) -> tuple[int, int]:
    """Fill actual_f for every forecast whose target day is safely over.

    Returns (rows updated, days looked up). Observations are fetched once per
    (station, day) however many forecasts point at it - 470 forecast rows are
    only a couple of dozen station-days.

    cli_by_station maps a station to its climate-report location (KNYC ->
    NYC). With it, the final climate report is the truth and hourly
    observations are the fallback after FALLBACK_AFTER_HOURS; without it,
    observations only (the original behaviour).
    """
    cli_by_station = cli_by_station or {}
    # A daily-LOW series is scored against the day's official minimum. Before
    # the low series existed everything was a high, which is still the default.
    kind_by_series = kind_by_series or {}
    has_source_col = "actual_source" in {
        r[1] for r in conn.execute("PRAGMA table_info(forecasts)")}
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
        kind = kind_by_series.get(r["series"], "high")
        wanted.setdefault((r["station"], tz_name, target, kind), []).append(r["id"])

    updated = looked_up = 0
    for (station, tz_name, target, kind), ids in list(wanted.items())[:limit]:
        looked_up += 1
        actual, used = None, "nws_observations"
        location = cli_by_station.get(station)
        if location:
            actual, _oid = official_daily_extreme(session, location, target,
                                                  kind, monitor)
            used = "nws_climate_report"
            if actual is None:
                day_end = dt.datetime.combine(
                    target + dt.timedelta(days=1), dt.time(0, 0),
                    tzinfo=ZoneInfo(tz_name))
                if now < day_end + dt.timedelta(hours=FALLBACK_AFTER_HOURS):
                    continue            # the final report is not out yet
                log.warning("SCORING  %s %s: no final climate report CLI%s "
                            "%.0fh after the day; falling back to hourly "
                            "observations", station, target, location,
                            FALLBACK_AFTER_HOURS)
                used = "nws_observations"
        if actual is None and kind == "high":
            actual = observed_daily_max(session, station, tz_name, target, monitor)
        if actual is None:
            continue          # a low has no hourly fallback: wait for the report
        if has_source_col:
            conn.executemany(
                "UPDATE forecasts SET actual_f=?, actual_source=? WHERE id=?",
                [(actual, used, i) for i in ids])
        else:
            conn.executemany("UPDATE forecasts SET actual_f=? WHERE id=?",
                             [(actual, i) for i in ids])
        updated += len(ids)
        log.info("SCORING  %s %s %s: %s %.1fF (%d forecast%s)",
                 station, target, kind,
                 "official" if used == "nws_climate_report" else "observed",
                 actual, len(ids), "" if len(ids) == 1 else "s")
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
