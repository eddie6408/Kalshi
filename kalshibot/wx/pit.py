"""WX-2: point-in-time weather data.

Four feeds, all append-only into the research store, all behind the quota
guard:

  forecasts     Open-Meteo, every model the blend uses, hourly out to 16
                days, aggregated per LOCAL day into max / min / hourly mean.
                One row per snapshot: what was known at retrieved_at.
  history       Open-Meteo's previous-runs API: the same models as issued 1-7
                days before each hour - point in time by construction, which
                is what the WX-3 calibration is fitted on.
  observations  The Weather Company's Kalshi climate portal (the source the
                daily and weekly rules name): max, min, HOURLY mean with its
                hour count, the rounded daily mean, HDD, precipitation - with
                PRELIMINARY / FINAL / REVISED kept as separate rows and a
                quality flag (VALID / MISSING / SUSPECT / STALE).
  climatology   NOAA ACIS (the NWS climate archive): 30 years of official
                daily max / min / average per station, for horizons past the
                forecast.

Why hourly aggregates, not the models' own daily max: the weekly markets
settle on the MEAN OF HOURLY VALUES (KXAVGT rules), which no daily field
gives, and live inputs must be built exactly the way the calibration inputs
were (previous-runs is hourly only).
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from .. import sources as _sources
from .store import utcnow

log = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
TWC_PRIMARY = "https://weather.com/kalshi/api/climate/primary"
TWC_METAR = "https://weather.com/kalshi/api/metar"
ACIS_URL = "https://data.rcc-acis.org/StnData"

#: The blend's models (weather_blend.MODELS). HRRR is kept: it simply has no
#: values beyond ~48h.
MODELS = ("ncep_nbm_conus", "gfs_global", "ecmwf_ifs025", "ecmwf_aifs025_single",
          "ncep_hrrr_conus", "gem_seamless", "icon_seamless")
VARIABLES = ("temp_max", "temp_min", "temp_mean_hourly")
MIN_HOURS = 18

STATUS = {"official": "FINAL", "revised": "REVISED", "preliminary": "PRELIMINARY"}


def om_weight(n_vars: int, n_days: int) -> int:
    """Open-Meteo counts a request with many variables or a long range as
    several calls. Budget it as ceil(vars/10) * ceil(days/14)."""
    return max(1, math.ceil(n_vars / 10) * math.ceil(max(1, n_days) / 14))


def local_day_aggregates(times: list[str], values: list, min_hours: int = MIN_HOURS
                         ) -> dict[str, dict]:
    """{local date: {temp_max, temp_min, temp_mean_hourly, hours}} from an
    hourly series already in local time ("2026-09-29T13:00")."""
    by: dict[str, list[float]] = defaultdict(list)
    for t, v in zip(times or [], values or []):
        if v is None:
            continue
        by[t[:10]].append(float(v))
    out = {}
    for d, vs in by.items():
        row = {"hours": len(vs)}
        if len(vs) >= min_hours:
            row.update(temp_max=max(vs), temp_min=min(vs), temp_mean_hourly=sum(vs) / len(vs))
        out[d] = row
    return out


def parse_twc_day(payload: dict, retrieved_at: str, today: date | None = None) -> list[tuple]:
    """wx_observations rows from one weather.com/kalshi primary response."""
    rows = []
    obs_date = payload.get("date")
    today = today or datetime.now(timezone.utc).date()
    for r in payload.get("results") or []:
        st = ((r.get("station") or {}).get("icao") or "").upper()
        data = r.get("data") or {}
        status = STATUS.get(r.get("status"), (r.get("status") or "NONE").upper())
        hours = r.get("avgHours")
        old = obs_date and (today - date.fromisoformat(obs_date)).days >= 2

        def q(value, extra_bad=False):
            if value is None:
                return "MISSING"
            if extra_bad:
                return "SUSPECT"
            if status == "PRELIMINARY" and old:
                return "STALE"
            return "VALID"
        mx, mn = data.get("maxTemp"), data.get("minTemp")
        bad_pair = isinstance(mx, (int, float)) and isinstance(mn, (int, float)) and mx < mn
        vals = [
            ("temp_max", mx, q(mx, bad_pair), None),
            ("temp_min", mn, q(mn, bad_pair), None),
            ("temp_mean_hourly", r.get("avgTemp"),
             q(r.get("avgTemp"), hours is not None and hours < MIN_HOURS), hours),
            ("temp_mean", r.get("dailyMean"), q(r.get("dailyMean")), None),
            ("hdd", r.get("hdd"), q(r.get("hdd")), None),
        ]
        p = data.get("precipitation")
        vals.append(("precip", 0.0001 if p == "T" else p, q(p), None))   # "T" = trace
        for var, value, quality, h in vals:
            v = value if isinstance(value, (int, float)) else None
            rows.append(("twc_kalshi", st, obs_date, var, v, status, quality, h, retrieved_at))
    return rows


class PointInTime:
    def __init__(self, store, quota, stations: dict[str, dict], user_agent: str = "",
                 monitor=None, timeout: float = 30.0, models=MODELS):
        """stations: {ICAO: {latitude, longitude, timezone, cli}}"""
        self.store = store
        self.quota = quota
        self.stations = stations
        self.models = tuple(models)
        self.timeout = timeout
        self.monitor = monitor or _sources.MONITOR
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent or "kalshibot/1.0 (research)"})

    def _get(self, source_id: str, url: str, params: dict | None = None, method: str = "GET",
             json_body: dict | None = None) -> dict | None:
        try:
            with self.monitor.track(source_id) as call:
                if method == "POST":
                    r = self.session.post(url, json=json_body, timeout=self.timeout)
                else:
                    r = self.session.get(url, params=params, timeout=self.timeout)
                call.status = r.status_code
                r.raise_for_status()
                j = r.json()
                if isinstance(j, dict) and j.get("error"):
                    call.ok = False
                    call.error = str(j.get("reason") or "error")
                    return None
                return j
        except Exception as exc:  # noqa: BLE001
            log.warning("WX DATA  %s failed: %s", source_id, exc)
            return None

    # ------------------------------------------------------------ forecasts
    def snapshot_forecasts(self, icaos: list[str] | None = None, days: int = 16) -> int:
        """One multi-model hourly request per station; per-local-day
        aggregates stored with this snapshot's retrieved_at."""
        n = 0
        now = datetime.now(timezone.utc)
        issued = now.replace(minute=0, second=0, microsecond=0).isoformat()
        for icao in icaos or sorted(self.stations):
            st = self.stations.get(icao)
            if not st or "latitude" not in st:
                continue
            if not self.quota.allow("open_meteo", om_weight(len(self.models), days)):
                break
            j = self._get("wx_open_meteo", FORECAST_URL, {
                "latitude": st["latitude"], "longitude": st["longitude"],
                "hourly": "temperature_2m", "models": ",".join(self.models),
                "temperature_unit": "fahrenheit", "timezone": st["timezone"],
                "forecast_days": days, "past_days": 1})
            if not j:
                continue
            h = j.get("hourly") or {}
            times = h.get("time") or []
            today_local = now.astimezone(ZoneInfo(st["timezone"])).date()
            rows = []
            for m in self.models:
                col = h.get(f"temperature_2m_{m}") or (h.get("temperature_2m")
                                                       if len(self.models) == 1 else None)
                for d, agg in local_day_aggregates(times, col).items():
                    lead = (date.fromisoformat(d) - today_local).days
                    for var in VARIABLES:
                        if var in agg:
                            rows.append(("open_meteo", m, icao, d, var, round(agg[var], 2),
                                         issued, lead, now.isoformat(timespec="seconds"), 1))
            n += self.store.executemany(
                "INSERT OR IGNORE INTO wx_forecasts (source, model, station, valid_date, "
                "variable, value, issued_at, lead_days, retrieved_at, approx_issue) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        return n

    def latest_forecast(self, icao: str, variable: str, valid_date: str) -> dict[str, float]:
        """{model: value} from the most recent snapshot covering this day."""
        rows = self.store.query(
            "SELECT model, value, issued_at FROM wx_forecasts WHERE station=? AND variable=? "
            "AND valid_date=? AND source='open_meteo' AND issued_at = (SELECT MAX(issued_at) "
            "FROM wx_forecasts WHERE station=? AND variable=? AND valid_date=? "
            "AND source='open_meteo')", (icao, variable, valid_date, icao, variable, valid_date))
        return {r["model"]: r["value"] for r in rows}

    # ------------------------------------------------------ previous runs
    def backfill_previous_runs(self, icao: str, start: date, end: date,
                               leads=range(1, 8), models=None) -> int:
        """History for calibration: each model as issued `lead` days before."""
        st = self.stations.get(icao)
        models = tuple(models or self.models)
        if not st:
            return 0
        vars_ = [f"temperature_2m_previous_day{d}" for d in leads]
        n_days = (end - start).days + 1
        if not self.quota.allow("open_meteo", om_weight(len(vars_) * len(models), n_days)):
            return -1
        j = self._get("wx_open_meteo_previous", PREVIOUS_RUNS_URL, {
            "latitude": st["latitude"], "longitude": st["longitude"],
            "hourly": ",".join(vars_), "models": ",".join(models),
            "temperature_unit": "fahrenheit", "timezone": st["timezone"],
            "start_date": start.isoformat(), "end_date": end.isoformat()})
        if not j:
            return 0
        h = j.get("hourly") or {}
        times = h.get("time") or []
        now = utcnow()
        rows = []
        for m in models:
            for d in leads:
                col = h.get(f"temperature_2m_previous_day{d}_{m}")
                if col is None and len(models) == 1:
                    col = h.get(f"temperature_2m_previous_day{d}")
                for day, agg in local_day_aggregates(times, col).items():
                    issued = (date.fromisoformat(day) - timedelta(days=d)).isoformat() + f"T00:00~D{d}"
                    for var in VARIABLES:
                        if var in agg:
                            rows.append(("open_meteo_previous_runs", m, icao, day, var,
                                         round(agg[var], 2), issued, d, now, 1))
        return self.store.executemany(
            "INSERT OR IGNORE INTO wx_forecasts (source, model, station, valid_date, variable, "
            "value, issued_at, lead_days, retrieved_at, approx_issue) VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows)

    # --------------------------------------------------------- observations
    def record_observations(self, days_back: int = 3) -> int:
        """TWC's Kalshi portal for today and the last few days (39 stations
        per call). Preliminary and final both kept."""
        n = 0
        today = datetime.now(timezone.utc).date()
        for k in range(days_back + 1):
            d = today - timedelta(days=k)
            if not self.quota.allow("twc_climate"):
                break
            j = self._get("twc_settlement", TWC_PRIMARY, {"date": d.isoformat()})
            if not j:
                continue
            n += self.store.executemany(
                "INSERT OR IGNORE INTO wx_observations (source, station, obs_date, variable, "
                "value, status, quality, hours, retrieved_at) VALUES (?,?,?,?,?,?,?,?,?)",
                parse_twc_day(j, utcnow(), today))
        return n

    def record_hourly(self, week_start: date | None = None) -> int:
        """TWC's hourly observations for the primary stations, one week per
        call (what KXSJCLOWT-style 'any hourly value' markets settle on)."""
        today = datetime.now(timezone.utc).date()
        week_start = week_start or (today - timedelta(days=today.weekday()))
        if not self.quota.allow("twc_climate"):
            return 0
        j = self._get("twc_settlement", TWC_METAR,
                      {"primary": "true", "weekStart": week_start.isoformat()})
        if not j:
            return 0
        now = utcnow()
        rows = []
        for st in j.get("stations") or []:
            for o in st.get("observations") or []:
                if o.get("tempF") is None:
                    continue
                rows.append(("twc_metar", (o.get("icaoId") or st.get("icaoId") or "").upper(),
                             o.get("reportTimeUTC"), o.get("localDate"), o.get("localHour"),
                             float(o["tempF"]), o.get("status"), now))
        return self.store.executemany(
            "INSERT OR REPLACE INTO wx_hourly_obs (source, station, ts_utc, local_date, "
            "local_hour, temp_f, status, retrieved_at) VALUES (?,?,?,?,?,?,?,?)", rows)

    def latest_observation(self, icao: str, variable: str, obs_date: str) -> dict | None:
        """Best current value for one station-day: FINAL/REVISED over
        PRELIMINARY, newest retrieval first."""
        return self.store.one(
            "SELECT value, status, quality, hours, retrieved_at FROM wx_observations "
            "WHERE station=? AND variable=? AND obs_date=? AND value IS NOT NULL "
            "ORDER BY CASE status WHEN 'REVISED' THEN 0 WHEN 'FINAL' THEN 1 ELSE 2 END, "
            "retrieved_at DESC LIMIT 1", (icao, variable, obs_date))

    # ---------------------------------------------------------- climatology
    def backfill_climatology(self, icao: str, years: int = 30) -> int:
        st = self.stations.get(icao) or {}
        sid = st.get("cli") or icao[1:]
        have = self.store.one("SELECT COUNT(*) n, MAX(obs_date) last FROM wx_climatology "
                              "WHERE station=?", (icao,))
        today = datetime.now(timezone.utc).date()
        if have and have["n"] > 300 * years and have["last"] and \
                (today - date.fromisoformat(have["last"])).days < 20:
            return 0
        if not self.quota.allow("acis"):
            return -1
        j = self._get("noaa_acis", ACIS_URL, method="POST", json_body={
            "sid": sid, "sdate": date(today.year - years, 1, 1).isoformat(),
            "edate": (today - timedelta(days=1)).isoformat(),
            "elems": [{"name": "maxt"}, {"name": "mint"}, {"name": "avgt"}]})
        if not j:
            return 0

        def num(x):
            try:
                return float(x)
            except (TypeError, ValueError):
                return None       # "M" missing, "T" trace
        rows = [(icao, d, num(mx), num(mn), num(av)) for d, mx, mn, av in j.get("data") or []]
        return self.store.executemany(
            "INSERT OR REPLACE INTO wx_climatology (station, obs_date, maxt, mint, avgt) "
            "VALUES (?,?,?,?,?)", rows)

    def prune(self, keep_days: int = 400) -> int:
        cut = (datetime.now(timezone.utc).date() - timedelta(days=keep_days)).isoformat()
        return self.store.execute("DELETE FROM wx_forecasts WHERE source='open_meteo' "
                                  "AND valid_date < ?", (cut,)).rowcount
