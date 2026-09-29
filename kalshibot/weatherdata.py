"""Free weather data sources. No API keys required.

Primary   : Open-Meteo ensemble API (many model members -> a real distribution)
Secondary : NWS api.weather.gov gridpoint forecast (single deterministic value)
Reality   : NWS latest observations, so we never predict a daily high that is
            lower than a temperature already recorded today.

Every fetch is recorded against its registered source (package 14A,
kalshibot/sources.py): health on the monitor, and one provenance row in the
journal saying who published the number, when it was produced, and when we
retrieved it. Recording never changes what is returned.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import time
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import requests

from . import sources as _sources
from .data_gates import BAD_QC_FLAGS

#: Same rule scoring uses (forecast_scoring.SPIKE_GAP_F): a lone maximum this
#: far clear of every other reading is a sensor fault, not weather.
SPIKE_GAP_F = 5.0

log = logging.getLogger(__name__)

ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
MODEL_META_URL = "https://ensemble-api.open-meteo.com/data/{model}/static/meta.json"
NWS_POINTS = "https://api.weather.gov/points/{lat},{lon}"
NWS_OBS = "https://api.weather.gov/stations/{station}/observations"
HEADERS = {"User-Agent": "kalshibot/1.0 (personal trading research)", "Accept": "application/json"}

#: The Open-Meteo model name we request -> the data set whose run metadata
#: says which model run we got. Verified against meta.json on 2026-09-25.
#: "seamless" blends resolutions; the near-term member set is the one listed.
MODEL_RUN_METADATA = {
    "gfs_seamless": "ncep_gefs025",
    "icon_seamless": "dwd_icon_eps",
    "ecmwf_ifs025": "ecmwf_ifs025_ensemble",
}
MODEL_RUN_CACHE_SECONDS = 600

#: NOAA HRRR, 3 km, re-run every hour - the model the same-day nowcast
#: (kalshibot/nowcast.py) is built on. Through Open-Meteo, which also serves
#: the past hours, so the model can be checked against today's readings.
HRRR_URL = "https://api.open-meteo.com/v1/forecast"
HRRR_MODEL = "gfs_hrrr"
HRRR_META_URL = "https://api.open-meteo.com/data/ncep_hrrr_conus/static/meta.json"
HRRR_CACHE_SECONDS = 600

#: Station readings are re-read this often, independently of the forecast
#: cache. They used to ride along with a 30-minute forecast, so the live
#: "high so far" could be half an hour old at exactly the moment it matters.
OBS_REFRESH_SECONDS = 300


@dataclass
class HourlyModel:
    """Hourly model temperatures for a point, in the station's time zone."""
    hours: list = field(default_factory=list)      # [(aware datetime, deg F)]
    run: str | None = None                         # initialisation time
    released: str | None = None                    # when it was published
    provenance_id: int | None = None


@dataclass
class _Obs:
    extreme: float | None = None
    latest: dt.datetime | None = None
    provenance_id: int | None = None
    suspect: str = ""
    #: readings fit to model from: bad-QC readings and a lone spike removed
    clean: list = field(default_factory=list)       # [(aware datetime, deg F)]


def _epoch_iso(value) -> str | None:
    try:
        return dt.datetime.fromtimestamp(float(value), dt.timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


@dataclass
class DailyTempForecast:
    date: dt.date
    kind: str                      # "high" | "low"
    members: list[float] = field(default_factory=list)   # one value per model member, deg F
    source: str = ""
    observed_extreme_so_far: float | None = None
    lead_hours: float = 0.0
    # ---- provenance (14A). None/empty means "not known", never "fresh". ----
    #: journal source_observations ids behind this forecast
    provenance: list[int] = field(default_factory=list)
    #: when WE fetched it
    retrieved_at: dt.datetime | None = None
    #: when the PUBLISHER produced it: the oldest model run's release time for
    #: the ensemble, NWS updateTime for the gridpoint
    data_time: dt.datetime | None = None
    #: which model runs, {model: initialisation time}
    vintage: dict = field(default_factory=dict)
    #: newest station observation behind observed_extreme_so_far
    observation_time: dt.datetime | None = None
    #: why that observed extreme should not be trusted ("" = no reason found)
    observation_suspect: str = ""
    #: today's usable station readings, [(aware local datetime, deg F)]
    observations: list = field(default_factory=list)
    #: provenance id of the observation fetch (kept apart from the forecast's)
    observation_provenance_id: int | None = None
    #: time.monotonic() of the last observation read; None = never
    observations_read_at: float | None = None
    #: independent second opinion (14A redundancy). Recorded, not acted on.
    cross_check_f: float | None = None
    cross_check_source: str = ""
    cross_check_data_time: dt.datetime | None = None

    @property
    def n_members(self) -> int:
        return len(self.members)

    @property
    def mean(self) -> float:
        return sum(self.members) / len(self.members) if self.members else float("nan")

    @property
    def degraded(self) -> bool:
        """Did this come from the fallback rather than the ensemble?

        The NWS gridpoint fallback returns ONE number. The estimator then
        invents a spread for it from lead time, and everything downstream -
        the edge, the size, the Brier score - treats that invented spread
        exactly like one measured across thirty ensemble members.

        That is not obviously wrong, but it is obviously DIFFERENT, and the
        difference was invisible: on 18 September the ensemble came back
        unusable five times and nothing downstream knew.
        """
        return self.source != "open-meteo-ensemble" or self.n_members < 5

    @property
    def spread(self) -> float:
        n = len(self.members)
        if n < 2:
            return 0.0
        m = self.mean
        return (sum((x - m) ** 2 for x in self.members) / (n - 1)) ** 0.5


class _Cache:
    def __init__(self, ttl_seconds: int):
        self.ttl = ttl_seconds
        self._store: dict = {}

    def get(self, key):
        hit = self._store.get(key)
        if hit and (time.time() - hit[0]) < self.ttl:
            return hit[1]
        return None

    def put(self, key, value):
        self._store[key] = (time.time(), value)

    def replace(self, key, value):
        """Swap the value but keep its age, so refreshing the observations
        never extends how long the forecast itself is served."""
        hit = self._store.get(key)
        self._store[key] = ((hit[0] if hit else time.time()), value)


class WeatherData:
    def __init__(self, ensemble_models: list[str] | None = None, cache_seconds: int = 1800,
                 timeout: float = 25.0, forecast_days: int = 7,
                 monitor: _sources.SourceMonitor | None = None,
                 cross_check: bool = False, user_agent: str | None = None):
        self.models = ensemble_models or ["gfs_seamless", "icon_seamless", "ecmwf_ifs025"]
        #: How far ahead to ask for. Kalshi lists temperature markets several
        #: days out, and a window that stops short reads downstream as "0
        #: usable members" rather than as "we did not ask about that day".
        self.forecast_days = max(1, min(16, int(forecast_days)))
        self.cache = _Cache(cache_seconds)
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(HEADERS)
        if user_agent:
            self._session.headers["User-Agent"] = user_agent
        self._grid_cache: dict = {}
        self.monitor = monitor or _sources.MONITOR
        #: Fetch the NWS gridpoint alongside a good ensemble, as an independent
        #: second opinion (14A "source redundancy"). Recorded only; nothing
        #: acts on a disagreement until 14A phase 4.
        self.cross_check = bool(cross_check)
        self._model_runs: tuple[float, dict] | None = None
        self.obs_refresh_seconds = OBS_REFRESH_SECONDS
        self._hrrr: dict = {}

    # ------------------------------------------------------------------
    def daily_extreme_forecast(
        self, latitude: float, longitude: float, tz_name: str,
        target_date: dt.date, kind: str = "high", station: str | None = None,
    ) -> DailyTempForecast | None:
        key = (round(latitude, 3), round(longitude, 3), tz_name, target_date.isoformat(), kind)
        fc = self.cache.get(key)
        if fc is None:
            fc = self._from_ensemble(latitude, longitude, tz_name, target_date, kind)
            if fc is None:
                fc = self._from_nws(latitude, longitude, tz_name, target_date, kind)
            elif self.cross_check:
                second = self._from_nws(latitude, longitude, tz_name, target_date, kind)
                if second is not None:
                    fc.cross_check_f = second.mean
                    fc.cross_check_source = second.source
                    fc.cross_check_data_time = second.data_time
                    fc.provenance.extend(second.provenance)
            if fc is None:
                return None
            self.cache.put(key, fc)

        # Readings on their own, shorter clock. A copy, so a caller holding
        # the previous forecast never sees it change underneath it.
        if station and (fc.observations_read_at is None or time.monotonic()
                        - fc.observations_read_at >= self.obs_refresh_seconds):
            fc = dataclasses.replace(fc, provenance=list(fc.provenance))
            obs = self._observations(station, tz_name, target_date, kind)
            fc.observed_extreme_so_far = obs.extreme
            fc.observation_time = obs.latest
            fc.observation_suspect = obs.suspect
            fc.observations = obs.clean
            fc.observation_provenance_id = obs.provenance_id
            fc.observations_read_at = time.monotonic()
            self.cache.replace(key, fc)

        # Lead time is relative to NOW; a cached value is wrong by the cache age.
        tz = ZoneInfo(tz_name)
        end_of_day = dt.datetime.combine(target_date, dt.time(23, 59), tzinfo=tz)
        lead = max(0.0, (end_of_day - dt.datetime.now(tz)).total_seconds() / 3600.0)
        return dataclasses.replace(fc, lead_hours=lead)

    # ------------------------------------------------------------------
    def model_values(self, lat: float, lon: float, tz_name: str, target_date: dt.date,
                     kind: str, models) -> dict[str, float]:
        """Each named model's daily high (or low) for `target_date` at this
        point: NBM, GFS, ECMWF IFS / AIFS, HRRR, GEM, ICON (weather_blend.py).
        One request per location for all models and three days, cached like
        the ensemble. {} when unavailable - the blend then simply does not
        apply and the ensemble prices the market as before."""
        key = ("models", round(lat, 3), round(lon, 3), tz_name)
        daily = self.cache.get(key)
        if daily is None:
            params = {"latitude": lat, "longitude": lon,
                      "daily": "temperature_2m_max,temperature_2m_min",
                      "models": ",".join(models), "temperature_unit": "fahrenheit",
                      "timezone": tz_name, "forecast_days": 3, "past_days": 1}
            try:
                with self.monitor.track("open_meteo_models") as call:
                    r = self._session.get(HRRR_URL, params=params, timeout=self.timeout)
                    call.status = r.status_code
                    r.raise_for_status()
                    payload = r.json() or {}
                    if payload.get("error"):
                        call.ok = False
                        call.error = str(payload.get("reason") or "error")
                        return {}
                    daily = payload.get("daily") or {}
                    call.data_time = dt.datetime.now(dt.timezone.utc).isoformat()
            except Exception as exc:  # noqa: BLE001
                log.warning("multi-model forecast unavailable: %s", exc)
                return {}
            self.cache.put(key, daily)
        days = daily.get("time") or []
        try:
            i = days.index(target_date.isoformat())
        except ValueError:
            return {}
        var = "temperature_2m_max" if kind == "high" else "temperature_2m_min"
        out = {}
        for m in models:
            col = daily.get(f"{var}_{m}") or daily.get(var if len(models) == 1 else "") or []
            if i < len(col) and col[i] is not None:
                out[m] = float(col[i])
        return out

    def hourly_model(self, lat: float, lon: float, tz_name: str) -> HourlyModel | None:
        """HRRR hourly temperatures around now: 12 hours back, 30 ahead."""
        key = (round(lat, 3), round(lon, 3), tz_name)
        hit = self._hrrr.get(key)
        if hit and time.monotonic() - hit[0] < HRRR_CACHE_SECONDS:
            return hit[1]
        params = {"latitude": lat, "longitude": lon, "hourly": "temperature_2m",
                  "models": HRRR_MODEL, "temperature_unit": "fahrenheit",
                  "timezone": tz_name, "past_hours": 12, "forecast_hours": 30}
        retrieved_at = dt.datetime.now(dt.timezone.utc)
        try:
            with self.monitor.track("open_meteo_hrrr") as call:
                r = self._session.get(HRRR_URL, params=params, timeout=self.timeout)
                call.status = r.status_code
                r.raise_for_status()
                payload = r.json()
                if isinstance(payload, dict) and payload.get("error"):
                    call.ok = False
                    call.error = str(payload.get("reason") or "error")
                    return None
                hourly = payload.get("hourly") or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("HRRR unavailable: %s", exc)
            return None
        tz = ZoneInfo(tz_name)
        hours = []
        for t, v in zip(hourly.get("time") or [], hourly.get("temperature_2m") or []):
            if v is None:
                continue
            try:
                hours.append((dt.datetime.fromisoformat(t).replace(tzinfo=tz), float(v)))
            except ValueError:
                continue
        if not hours:
            self.monitor.record("open_meteo_hrrr", False, error="no usable hours")
            return None
        run = released = None
        try:
            with self.monitor.track("open_meteo_model_runs") as call:
                m = self._session.get(HRRR_META_URL, timeout=self.timeout)
                call.status = m.status_code
                m.raise_for_status()
                meta = m.json()
                run = _epoch_iso(meta.get("last_run_initialisation_time"))
                released = _epoch_iso(meta.get("last_run_availability_time"))
                call.data_time = released
        except Exception as exc:  # noqa: BLE001
            log.debug("HRRR run metadata unavailable: %s", exc)
        oid = self.monitor.observe(
            "open_meteo_hrrr", data_type="hourly_forecast_f", subject=f"{lat},{lon}",
            value_json={"model": HRRR_MODEL, "first": hours[0][0].isoformat(),
                        "last": hours[-1][0].isoformat(),
                        "values_f": [round(v, 1) for _t, v in hours]},
            data_time=released, data_vintage={HRRR_MODEL: {"run": run, "released": released}},
            retrieved_at=retrieved_at.isoformat())
        model = HourlyModel(hours, run, released, oid)
        self._hrrr[key] = (time.monotonic(), model)
        return model

    # ------------------------------------------------------------------
    def _from_ensemble(self, lat, lon, tz_name, target_date, kind) -> DailyTempForecast | None:
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m",
            "models": ",".join(self.models),
            "temperature_unit": "fahrenheit",
            "timezone": tz_name,
            # Kalshi lists temperature markets several days ahead. A window
            # that stops short does not fail loudly - it returns a payload
            # with no matching date, which reads as "0 usable members".
            "forecast_days": self.forecast_days,
        }
        src = "open_meteo_ensemble"
        retrieved_at = dt.datetime.now(dt.timezone.utc)
        t0 = time.monotonic()
        status = None
        try:
            r = self._session.get(ENSEMBLE_URL, params=params, timeout=self.timeout)
            status = r.status_code
            r.raise_for_status()
            payload = r.json()
        except Exception as exc:  # noqa: BLE001
            self.monitor.record(src, False, status=status,
                                latency_ms=(time.monotonic() - t0) * 1000,
                                error=f"{type(exc).__name__}: {exc}")
            log.warning("Open-Meteo ensemble unavailable: %s", exc)
            return None
        latency_ms = (time.monotonic() - t0) * 1000

        blocks = payload if isinstance(payload, list) else [payload]
        members: list[float] = []
        # Counted so a failure can say WHY. "Ensemble returned only 0 usable
        # members" appeared five times in one live day and named no cause: it
        # cannot distinguish an API error from a date outside the window from
        # a series that is mostly nulls. Each has a different fix.
        seen = {"blocks": len(blocks), "series": 0, "no_time_axis": 0,
                "date_not_in_range": 0, "too_few_hours": 0}
        for block in blocks:
            if isinstance(block, dict) and block.get("error"):
                log.warning("Open-Meteo returned an error: %s",
                            block.get("reason") or block.get("error"))
                self.monitor.record(src, False, status=status, latency_ms=latency_ms,
                                    error=str(block.get("reason") or block.get("error")))
                return None
            hourly = block.get("hourly") or {}
            times = hourly.get("time") or []
            if not times:
                seen["no_time_axis"] += 1
                continue
            idx = [i for i, t in enumerate(times) if t[:10] == target_date.isoformat()]
            if not idx:
                seen["date_not_in_range"] += 1
                continue
            for name, series in hourly.items():
                if not name.startswith("temperature_2m"):
                    continue
                seen["series"] += 1
                vals = [series[i] for i in idx if i < len(series) and series[i] is not None]
                if len(vals) < 12:          # need most of the day to trust a daily extreme
                    seen["too_few_hours"] += 1
                    continue
                members.append(max(vals) if kind == "high" else min(vals))

        if len(members) < 5:
            log.warning(
                "Ensemble unusable for %s %s: %d members from %d blocks / %d "
                "series (%d had no time axis, %d did not cover the date, %d "
                "had under 12 hours). Window requested: %d days.",
                target_date, kind, len(members), seen["blocks"], seen["series"],
                seen["no_time_axis"], seen["date_not_in_range"],
                seen["too_few_hours"], params["forecast_days"])
            if seen["date_not_in_range"] and not seen["series"]:
                log.warning("  -> every block came back without %s in it; the "
                            "forecast window is probably too short for a "
                            "market this far out.", target_date)
            # A 200 carrying no usable forecast is a failed source, not a
            # healthy one - this is the 18 September failure, counted.
            self.monitor.record(src, False, status=status, latency_ms=latency_ms,
                                error=f"unusable: {len(members)} members")
            return None

        data_time, vintage = self._model_run_vintage()
        self.monitor.record(src, True, status=status, latency_ms=latency_ms,
                            data_time=data_time)
        fc = DailyTempForecast(target_date, kind, members, source="open-meteo-ensemble",
                               retrieved_at=retrieved_at, data_time=data_time,
                               vintage=vintage)
        mean = sum(members) / len(members)
        oid = self.monitor.observe(
            src, data_type=f"forecast_daily_{kind}_f", subject=f"{lat},{lon}",
            target_date=target_date.isoformat(), value=round(mean, 3),
            value_json={"members": len(members), "models": self.models,
                        "spread_f": round(fc.spread, 3),
                        "min_f": round(min(members), 2), "max_f": round(max(members), 2),
                        "values_f": [round(m, 1) for m in members]},
            data_time=data_time.isoformat() if data_time else None,
            data_vintage=vintage or None,
            retrieved_at=retrieved_at.isoformat(), latency_ms=round(latency_ms, 1))
        if oid is not None:
            fc.provenance.append(oid)
        return fc

    def _model_run_vintage(self) -> tuple[dt.datetime | None, dict]:
        """Which model runs Open-Meteo is serving: (oldest release time,
        {model: run initialisation time}). Cached; a failure returns
        (None, {}) and never affects the forecast itself."""
        now = time.monotonic()
        if self._model_runs and now - self._model_runs[0] < MODEL_RUN_CACHE_SECONDS:
            return self._model_runs[1]
        vintage: dict = {}
        released: list[dt.datetime] = []
        for model in self.models:
            meta_name = MODEL_RUN_METADATA.get(model)
            if not meta_name:
                continue
            try:
                with self.monitor.track("open_meteo_model_runs") as call:
                    r = self._session.get(MODEL_META_URL.format(model=meta_name),
                                          timeout=self.timeout)
                    call.status = r.status_code
                    r.raise_for_status()
                    meta = r.json()
                    if meta.get("error"):
                        call.ok = False
                        call.error = str(meta.get("reason") or "error")
                        continue
                    init = _epoch_iso(meta.get("last_run_initialisation_time"))
                    avail = _epoch_iso(meta.get("last_run_availability_time"))
                    call.data_time = avail
            except Exception as exc:  # noqa: BLE001
                log.debug("model run metadata for %s unavailable: %s", model, exc)
                continue
            vintage[model] = {"run": init, "released": avail}
            if avail:
                released.append(_sources.to_utc(avail))
        result = (min(released) if released else None, vintage)
        self._model_runs = (now, result)
        return result

    # ------------------------------------------------------------------
    def _nws_grid(self, lat, lon):
        key = (round(lat, 4), round(lon, 4))
        if key in self._grid_cache:
            return self._grid_cache[key]
        try:
            with self.monitor.track("nws_gridpoint") as call:
                r = self._session.get(NWS_POINTS.format(lat=lat, lon=lon), timeout=self.timeout)
                call.status = r.status_code
                r.raise_for_status()
                url = r.json()["properties"]["forecastGridData"]
        except Exception as exc:  # noqa: BLE001
            log.warning("NWS point lookup failed: %s", exc)
            return None
        self._grid_cache[key] = url
        return url

    def _from_nws(self, lat, lon, tz_name, target_date, kind) -> DailyTempForecast | None:
        grid_url = self._nws_grid(lat, lon)
        if not grid_url:
            return None
        field_name = "maxTemperature" if kind == "high" else "minTemperature"
        retrieved_at = dt.datetime.now(dt.timezone.utc)
        try:
            with self.monitor.track("nws_gridpoint") as call:
                r = self._session.get(grid_url, timeout=self.timeout)
                call.status = r.status_code
                r.raise_for_status()
                props = r.json()["properties"]
                values = props[field_name]["values"]
                update_time = props.get("updateTime")
                call.data_time = update_time
        except Exception as exc:  # noqa: BLE001
            log.warning("NWS gridpoint fetch failed: %s", exc)
            return None

        tz = ZoneInfo(tz_name)
        for entry in values:
            start = entry["validTime"].split("/")[0].replace("Z", "+00:00")
            when = dt.datetime.fromisoformat(start).astimezone(tz).date()
            if when == target_date:
                value_f = float(entry["value"]) * 9 / 5 + 32
                data_time = _sources.to_utc(update_time)
                fc = DailyTempForecast(
                    target_date, kind, [value_f], source="nws-gridpoint",
                    retrieved_at=retrieved_at, data_time=data_time,
                    vintage={"nws_gridpoint": {"updated": update_time}})
                oid = self.monitor.observe(
                    "nws_gridpoint", endpoint=grid_url,
                    data_type=f"forecast_daily_{kind}_f", subject=f"{lat},{lon}",
                    target_date=target_date.isoformat(), value=round(value_f, 2),
                    value_json={"valid_time": entry["validTime"]},
                    data_time=update_time, retrieved_at=retrieved_at.isoformat(),
                    latency_ms=round(call.latency_ms or 0.0, 1))
                if oid is not None:
                    fc.provenance.append(oid)
                return fc
        return None

    # ------------------------------------------------------------------
    def observed_extreme_today(self, station: str, tz_name: str,
                               target_date: dt.date, kind: str) -> float | None:
        """Highest/lowest temperature already observed at the station today."""
        return self._observed_extreme(station, tz_name, target_date, kind)[0]

    def _observed_extreme(self, station: str, tz_name: str, target_date: dt.date,
                          kind: str) -> tuple[float | None, dt.datetime | None,
                                              int | None, str]:
        o = self._observations(station, tz_name, target_date, kind)
        return o.extreme, o.latest, o.provenance_id, o.suspect

    def _observations(self, station: str, tz_name: str, target_date: dt.date,
                      kind: str) -> _Obs:
        """(extreme so far, newest observation time, provenance id, suspect).

        `suspect` names a reason not to trust the extreme - an NWS quality
        flag of X/Q, or a lone reading more than SPIKE_GAP_F clear of the
        next - and is "" when there is none. Reported, not acted on here:
        the OBSERVATION_QC gate decides (kalshibot/data_gates.py)."""
        tz = ZoneInfo(tz_name)
        now_local = dt.datetime.now(tz)
        if now_local.date() != target_date:
            return _Obs()
        start = dt.datetime.combine(target_date, dt.time(0, 0), tzinfo=tz)
        url = NWS_OBS.format(station=station)
        retrieved_at = dt.datetime.now(dt.timezone.utc)
        try:
            with self.monitor.track("nws_observations") as call:
                r = self._session.get(
                    url,
                    params={"start": start.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
                    timeout=self.timeout,
                )
                call.status = r.status_code
                r.raise_for_status()
                feats = r.json().get("features", [])
                stamps = [((f.get("properties") or {}).get("timestamp")) for f in feats]
                stamps = [x for x in stamps if x]
                call.data_time = max(stamps) if stamps else None
        except Exception as exc:  # noqa: BLE001
            log.warning("NWS observations for %s failed: %s", station, exc)
            return _Obs()

        readings = []           # (deg F, timestamp, qc flag)
        for f in feats:
            props = f.get("properties") or {}
            temp = props.get("temperature") or {}
            v = temp.get("value")
            if v is not None:
                readings.append((float(v) * 9 / 5 + 32, props.get("timestamp"),
                                 temp.get("qualityControl")))
        if not readings:
            return _Obs()
        pick = max if kind == "high" else min
        extreme = pick(readings, key=lambda x: x[0])
        latest = max((x[1] for x in readings if x[1]), default=None)
        qc: dict = {}
        for _v, _t, flag in readings:
            qc[str(flag)] = qc.get(str(flag), 0) + 1
        suspect = ""
        if str(extreme[2]) in BAD_QC_FLAGS:
            suspect = f"NWS quality flag {extreme[2]} on the {extreme[0]:.1f}F reading"
        else:
            vals = sorted((x[0] for x in readings), reverse=(kind == "high"))
            gap = abs(vals[0] - vals[1]) if len(vals) >= 2 else 0.0
            if len(vals) >= 5 and gap > SPIKE_GAP_F:
                suspect = (f"{vals[0]:.1f}F is {gap:.1f}F clear of the next "
                           f"reading ({vals[1]:.1f}F) - looks like a sensor spike")
        oid = self.monitor.observe(
            "nws_observations", endpoint=url,
            data_type=f"observed_{kind}_so_far_f", subject=station,
            target_date=target_date.isoformat(), value=round(extreme[0], 2),
            value_json={"readings": len(readings), "extreme_at": extreme[1],
                        "extreme_qc": extreme[2], "qc_flags": qc,
                        "suspect": suspect or None},
            data_time=latest, retrieved_at=retrieved_at.isoformat(),
            latency_ms=round(call.latency_ms or 0.0, 1))
        # What the nowcast fits to: nothing NWS rejected, and not a lone spike.
        clean = []
        for v, t, flag in readings:
            if str(flag) in BAD_QC_FLAGS or not t:
                continue
            when = _sources.to_utc(t)
            if when is None:
                continue
            clean.append((when.astimezone(tz), v))
        if suspect and "spike" in suspect and clean:
            worst = (max if kind == "high" else min)(clean, key=lambda x: x[1])
            clean.remove(worst)
        clean.sort()
        return _Obs(extreme[0], _sources.to_utc(latest), oid, suspect, clean)
