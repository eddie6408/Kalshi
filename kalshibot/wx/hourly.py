"""WX-5: hourly temperature contracts - the Kalshi Weather Index.

Verified 2026-09-29: today's hourly markets (KXTEMPNYCHS, KXTEMPMIAH,
KXTEMPLAXHS, KXTEMPCHIHS) settle on the Kalshi Weather Index published via
Synoptic Data - an equal-weight mean of 5-8 ASOS stations per city
(config/kalshi_weather_index.json, from the contract certifications). With
every member reporting, the index IS the plain mean of member temperatures:
the static offsets cancel (B_c = sum w_i b_i).

Model, per contract (index value at one clock minute):
    centre = mean over members of HRRR at that hour
             + (current observed index - HRRR at the last observation) * decay(lead)
    sd     = f(lead hours), a conservative table until fitted
    P(index > T) = 1 - Phi((T - centre) / sd)          (0.01 F precision)

Honest limits, which is why every hourly contract stays RESEARCH_ONLY:
  * observations are hourly METARs (aviationweather.gov), not the one-minute
    HF-ASOS feed through Synoptic that the index uses (that needs a Synoptic
    account, which is the operator's call);
  * the index's QC and fallback rules are not reproduced;
  * the sd table is a prior. Validation is against KALSHI'S OWN settled
    results for these markets, which need no index data.
Markets whose rules name a different source (the April-2026 AccuWeather
series) are not priced here.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from .. import sources as _sources
from .calibration import norm_cdf
from .extended import stable_seed
from .store import utcnow
from .taxonomy import parse_local_hour

log = logging.getLogger(__name__)

MODEL_VERSION = "wx-hourly-index@1"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
METAR_URL = "https://aviationweather.gov/api/data/metar"
#: lead hours -> sd (F). Prior, conservative; replaced by a fit once the
#: logged runs have settled outcomes.
SD_PRIOR = ((1, 1.0), (3, 1.6), (6, 2.1), (12, 2.6), (24, 3.2), (48, 4.0))
BIAS_EFOLD_HOURS = 6.0


def sd_for_lead(hours: float) -> float:
    for h, s in SD_PRIOR:
        if hours <= h:
            return s
    return SD_PRIOR[-1][1] + 0.05 * (hours - SD_PRIOR[-1][0])


def load_indexes(path: str | Path) -> dict:
    try:
        return json.loads(Path(path).read_text()).get("indexes") or {}
    except (OSError, ValueError):
        return {}


def target_utc(c: dict, tz_name: str) -> datetime | None:
    hm = parse_local_hour(c.get("at_time") or "")
    if not hm or not c.get("window_start"):
        return None
    d = datetime.fromisoformat(c["window_start"])
    local = d.replace(hour=hm[0], minute=hm[1], tzinfo=ZoneInfo(tz_name))
    return local.astimezone(timezone.utc)


class HourlyIndexModel:
    def __init__(self, store, quota, indexes: dict, user_agent: str = "", monitor=None,
                 timeout: float = 20.0, cache_minutes: int = 50):
        self.store = store
        self.quota = quota
        self.indexes = indexes
        self.series_to_index = {s.upper(): k for k, v in indexes.items() for s in v.get("series", [])}
        self.monitor = monitor or _sources.MONITOR
        self.timeout = timeout
        self.cache_minutes = cache_minutes
        self._fc: dict = {}
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent or "kalshibot/1.0 (research)"})

    def _hrrr(self, key: str) -> dict | None:
        """{utc hour iso: index mean} from HRRR at every member (one call)."""
        hit = self._fc.get(key)
        if hit and (datetime.now(timezone.utc) - hit[0]).total_seconds() < self.cache_minutes * 60:
            return hit[1]
        idx = self.indexes[key]
        mem = idx["members"]
        if not self.quota.allow("open_meteo", len(mem)):
            return hit[1] if hit else None
        try:
            with self.monitor.track("wx_open_meteo") as call:
                r = self.session.get(FORECAST_URL, params={
                    "latitude": ",".join(str(m["latitude"]) for m in mem),
                    "longitude": ",".join(str(m["longitude"]) for m in mem),
                    "hourly": "temperature_2m", "models": "ncep_hrrr_conus",
                    "temperature_unit": "fahrenheit", "timezone": "GMT",
                    "forecast_hours": 48, "past_hours": 6}, timeout=self.timeout)
                call.status = r.status_code
                r.raise_for_status()
                j = r.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("WX HOURLY  forecast for %s unavailable: %s", key, exc)
            return hit[1] if hit else None
        locs = j if isinstance(j, list) else [j]
        series: dict[str, list] = {}
        for loc in locs:
            h = loc.get("hourly") or {}
            for t, v in zip(h.get("time") or [], h.get("temperature_2m") or []):
                if v is not None:
                    series.setdefault(t, []).append(float(v))
        out = {t + ":00+00:00": sum(v) / len(v) for t, v in series.items()
               if len(v) >= idx.get("quorum_members", len(mem))}
        self._fc[key] = (datetime.now(timezone.utc), out)
        return out

    def _observed(self, key: str) -> tuple[datetime, float] | None:
        """Latest hour where the quorum of members reported: (time, index)."""
        idx = self.indexes[key]
        if not self.quota.allow("aviationweather"):
            return None
        try:
            with self.monitor.track("aviationweather_metar") as call:
                r = self.session.get(METAR_URL, params={
                    "ids": ",".join(m["icao"] for m in idx["members"]), "format": "json",
                    "hours": 3}, timeout=self.timeout)
                call.status = r.status_code
                r.raise_for_status()
                rows = r.json() or []
        except Exception as exc:  # noqa: BLE001
            log.warning("WX HOURLY  METARs for %s unavailable: %s", key, exc)
            return None
        by_hour: dict[str, dict] = {}
        for o in rows:
            if o.get("temp") is None or not o.get("reportTime"):
                continue
            hour = o["reportTime"][:13]
            by_hour.setdefault(hour, {})[o["icaoId"]] = o["temp"] * 9 / 5 + 32
        for hour in sorted(by_hour, reverse=True):
            vals = by_hour[hour]
            if len(vals) >= idx.get("quorum_members", len(idx["members"])):
                t = datetime.fromisoformat(hour + ":00:00+00:00")
                return t, sum(vals.values()) / len(vals)
        return None

    def price(self, c: dict) -> dict | None:
        key = self.series_to_index.get((c.get("series") or "").upper())
        if not key or c.get("source") != "SYNOPTIC_KALSHI_INDEX":
            return None
        idx = self.indexes[key]
        when = target_utc(c, idx["timezone"])
        now = datetime.now(timezone.utc)
        if when is None or when < now - timedelta(minutes=5):
            return None
        fc = self._hrrr(key)
        if not fc:
            return None
        hour_key = when.replace(minute=0, second=0, microsecond=0).isoformat()
        base = fc.get(hour_key)
        if base is None:
            return None
        obs = self._observed(key)
        bias, obs_note = 0.0, None
        if obs:
            ot, ov = obs
            m = fc.get(ot.replace(minute=0, second=0, microsecond=0).isoformat())
            if m is not None:
                lead_from_obs = (when - ot).total_seconds() / 3600
                bias = (ov - m) * math.exp(-max(0.0, lead_from_obs) / BIAS_EFOLD_HOURS)
                obs_note = {"at": ot.isoformat(), "index_proxy_f": round(ov, 2),
                            "model_at_obs_f": round(m, 2)}
        lead_h = (when - now).total_seconds() / 3600
        centre, sd = base + bias, sd_for_lead(lead_h)
        op, thr, hi = c["operator"], c["threshold"], c.get("threshold_hi")
        if op in (">", ">="):
            p = 1 - norm_cdf((thr - centre) / sd)
        elif op in ("<", "<="):
            p = norm_cdf(((hi if hi is not None else thr) - centre) / sd)
        elif op == "between":
            p = norm_cdf((hi - centre) / sd) - norm_cdf((thr - centre) / sd)
        else:
            return None
        band = []
        for f in (0.85, 1.15):
            s = sd * f
            band.append(1 - norm_cdf((thr - centre) / s) if op in (">", ">=") else
                        norm_cdf(((hi if hi is not None else thr) - centre) / s) if op in ("<", "<=")
                        else norm_cdf((hi - centre) / s) - norm_cdf((thr - centre) / s))
        return {"prob": p, "lo": min([p] + band), "hi": max([p] + band), "centre": centre,
                "sd": sd, "lead_h": lead_h, "index": key, "target_utc": when.isoformat(),
                "hrrr_index_f": round(base, 2), "bias_f": round(bias, 2), "obs": obs_note}

    def run(self, contracts: list[dict], quotes: dict | None = None) -> list[int]:
        ids = []
        quotes = quotes or {}
        for c in contracts:
            try:
                r = self.price(c)
            except Exception as exc:  # noqa: BLE001
                log.warning("WX HOURLY  %s not priced: %s", c.get("ticker"), exc)
                continue
            if not r:
                continue
            q = quotes.get(c["ticker"]) or {}
            cur = self.store.execute(
                "INSERT INTO wx_model_runs (at, ticker, cls, method, model_version, calib_version, "
                "rules_version, seed, n_paths, prob, mc_se, prob_lo, prob_hi, stat_hist, inputs, "
                "market_yes_bid, market_yes_ask) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (utcnow(), c["ticker"], c["cls"], "index_hrrr_obs", MODEL_VERSION, "sd-prior@1",
                 c.get("rules_version"), stable_seed(c["ticker"]), 0, round(r["prob"], 5), 0.0,
                 round(r["lo"], 5), round(r["hi"], 5), "{}", json.dumps(r, default=str),
                 q.get("yes_bid", c.get("yes_bid")), q.get("yes_ask", c.get("yes_ask"))))
            ids.append(cur.lastrowid)
        return ids
