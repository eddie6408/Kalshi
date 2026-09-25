"""Free weather data sources. No API keys required.

Primary   : Open-Meteo ensemble API (many model members -> a real distribution)
Secondary : NWS api.weather.gov gridpoint forecast (single deterministic value)
Reality   : NWS latest observations, so we never predict a daily high that is
            lower than a temperature already recorded today.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger(__name__)

ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
NWS_POINTS = "https://api.weather.gov/points/{lat},{lon}"
NWS_OBS = "https://api.weather.gov/stations/{station}/observations"
HEADERS = {"User-Agent": "kalshibot/1.0 (personal trading research)", "Accept": "application/json"}


@dataclass
class DailyTempForecast:
    date: dt.date
    kind: str                      # "high" | "low"
    members: list[float] = field(default_factory=list)   # one value per model member, deg F
    source: str = ""
    observed_extreme_so_far: float | None = None
    lead_hours: float = 0.0

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


class WeatherData:
    def __init__(self, ensemble_models: list[str] | None = None, cache_seconds: int = 1800,
                 timeout: float = 25.0, forecast_days: int = 7):
        self.models = ensemble_models or ["gfs_seamless", "icon_seamless", "ecmwf_ifs025"]
        #: How far ahead to ask for. Kalshi lists temperature markets several
        #: days out, and a window that stops short reads downstream as "0
        #: usable members" rather than as "we did not ask about that day".
        self.forecast_days = max(1, min(16, int(forecast_days)))
        self.cache = _Cache(cache_seconds)
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(HEADERS)
        self._grid_cache: dict = {}

    # ------------------------------------------------------------------
    def daily_extreme_forecast(
        self, latitude: float, longitude: float, tz_name: str,
        target_date: dt.date, kind: str = "high", station: str | None = None,
    ) -> DailyTempForecast | None:
        key = (round(latitude, 3), round(longitude, 3), tz_name, target_date.isoformat(), kind)
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        fc = self._from_ensemble(latitude, longitude, tz_name, target_date, kind)
        if fc is None:
            fc = self._from_nws(latitude, longitude, tz_name, target_date, kind)
        if fc is None:
            return None

        if station:
            obs = self.observed_extreme_today(station, tz_name, target_date, kind)
            fc.observed_extreme_so_far = obs

        tz = ZoneInfo(tz_name)
        end_of_day = dt.datetime.combine(target_date, dt.time(23, 59), tzinfo=tz)
        fc.lead_hours = max(0.0, (end_of_day - dt.datetime.now(tz)).total_seconds() / 3600.0)

        self.cache.put(key, fc)
        return fc

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
        try:
            r = self._session.get(ENSEMBLE_URL, params=params, timeout=self.timeout)
            r.raise_for_status()
            payload = r.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("Open-Meteo ensemble unavailable: %s", exc)
            return None

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
            return None
        return DailyTempForecast(target_date, kind, members, source="open-meteo-ensemble")

    # ------------------------------------------------------------------
    def _nws_grid(self, lat, lon):
        key = (round(lat, 4), round(lon, 4))
        if key in self._grid_cache:
            return self._grid_cache[key]
        try:
            r = self._session.get(NWS_POINTS.format(lat=lat, lon=lon), timeout=self.timeout)
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
        try:
            r = self._session.get(grid_url, timeout=self.timeout)
            r.raise_for_status()
            values = r.json()["properties"][field_name]["values"]
        except Exception as exc:  # noqa: BLE001
            log.warning("NWS gridpoint fetch failed: %s", exc)
            return None

        tz = ZoneInfo(tz_name)
        for entry in values:
            start = entry["validTime"].split("/")[0].replace("Z", "+00:00")
            when = dt.datetime.fromisoformat(start).astimezone(tz).date()
            if when == target_date:
                celsius = float(entry["value"])
                return DailyTempForecast(
                    target_date, kind, [celsius * 9 / 5 + 32], source="nws-gridpoint"
                )
        return None

    # ------------------------------------------------------------------
    def observed_extreme_today(self, station: str, tz_name: str,
                               target_date: dt.date, kind: str) -> float | None:
        """Highest/lowest temperature already observed at the station today."""
        tz = ZoneInfo(tz_name)
        now_local = dt.datetime.now(tz)
        if now_local.date() != target_date:
            return None
        start = dt.datetime.combine(target_date, dt.time(0, 0), tzinfo=tz)
        try:
            r = self._session.get(
                NWS_OBS.format(station=station),
                params={"start": start.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
                timeout=self.timeout,
            )
            r.raise_for_status()
            feats = r.json().get("features", [])
        except Exception as exc:  # noqa: BLE001
            log.warning("NWS observations for %s failed: %s", station, exc)
            return None

        temps = []
        for f in feats:
            v = ((f.get("properties") or {}).get("temperature") or {}).get("value")
            if v is not None:
                temps.append(float(v) * 9 / 5 + 32)
        if not temps:
            return None
        return max(temps) if kind == "high" else min(temps)
