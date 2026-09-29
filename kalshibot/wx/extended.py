"""WX-4: multi-day contracts by Monte Carlo with temporal dependence.

Implements the rules as Kalshi wrote them (verified 2026-09-29):

  RUN_LENGTH  (KXAVGT*)  "at least k consecutive days within <week> on which
      the daily average temperature ... is strictly greater than T". The
      day's average is the MEAN OF TWC's HOURLY values, rounded to the nearest
      whole degree; exactly T does not count; a day with fewer than 18 hourly
      values breaks a streak; data as published at expiration.
  ANY_HOURLY  (KXSJCLOWT) "any hourly temperature value ... within <month> is
      strictly less than T" - the month's lowest HOURLY reading, which sits a
      little above the climate report's minimum (fitted offset).
  WINDOW_MAX / WINDOW_MIN  the highest / lowest daily climate-report value
      in the window (whole degrees).
  CUMULATIVE HDD (KXHDDCVG) sum over days of max(0, 65 - daily mean), the
      daily mean being TWC's rounded (max+min)/2.

Each day in the window is one of:
  observed     already published (TWC, final over preliminary): FIXED - the
               past is never simulated
  forecast     calibrated blend centre, error sd by lead day (WX-3)
  climatology  past the forecast horizon: NOAA ACIS 30-year mean/sd for the
               calendar day, with the last forecast anomaly decaying into it

Errors follow an AR(1) process on standardized errors, rho fitted from how
consecutive days' forecast errors from the same run move together (WX-3).
Independence would badly misprice a run-length market; a test pins that the
simulated lag-1 correlation matches the input.

Output per contract: probability, Monte Carlo standard error, a dispersion
sensitivity band (the same paths with sd x0.85 and x1.15), and the
distribution of the contract's statistic. Everything here is RESEARCH_ONLY.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

from .calibration import Calibration
from .store import utcnow

log = logging.getLogger(__name__)

MODEL_VERSION = "wx-extended@1"
DEFAULT_PATHS = 20000
CLIMATE_RHO = 0.75          # daily persistence of anomalies past the forecast
SENSITIVITY = (0.85, 1.15)


@dataclass
class DayInput:
    day: str
    kind: str                  # observed | forecast | climatology
    mean: float
    sd: float = 0.0
    hours: int | None = None   # observed hourly count (RUN_LENGTH rule)
    lead: int | None = None
    note: str = ""


@dataclass
class SimResult:
    prob: float
    mc_se: float
    prob_lo: float
    prob_hi: float
    n_paths: int
    seed: int
    hist: dict = field(default_factory=dict)


def round_half_up(x):
    """Whole-degree rounding as a person reads it: 90.5 -> 91 (numpy's
    default rounds half to even)."""
    return np.floor(np.asarray(x) + 0.5)


def ar1_paths(n: int, days: int, rho, rng) -> np.ndarray:
    """(n, days) standard normal paths with lag-1 correlation rho[t] between
    day t-1 and t (rho may be a scalar)."""
    rho = np.broadcast_to(np.asarray(rho, dtype=float), (max(days, 1),))
    z = np.empty((n, days))
    if days == 0:
        return z
    eps = rng.standard_normal((n, days))
    z[:, 0] = eps[:, 0]
    for t in range(1, days):
        r = float(np.clip(rho[t], -0.99, 0.99))
        z[:, t] = r * z[:, t - 1] + math.sqrt(1.0 - r * r) * eps[:, t]
    return z


def longest_run(mask: np.ndarray) -> np.ndarray:
    """Longest run of True per row, vectorized."""
    run = np.zeros(mask.shape[0], dtype=int)
    best = np.zeros(mask.shape[0], dtype=int)
    for t in range(mask.shape[1]):
        run = np.where(mask[:, t], run + 1, 0)
        best = np.maximum(best, run)
    return best


def compare(values, op: str, lo, hi=None):
    if op == ">":
        return values > lo
    if op == ">=":
        return values >= lo
    if op == "<":
        return values < lo if hi is None else values < hi
    if op == "<=":
        return values <= lo if hi is None else values <= hi
    if op == "between":
        return (values >= lo) & (values <= hi)
    raise ValueError(op)


def stable_seed(*parts) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


def simulate(days: list[DayInput], statistic, n: int = DEFAULT_PATHS, seed: int = 0,
             rho_forecast: float = 0.6, rho_climate: float = CLIMATE_RHO,
             event=None, hist_bins=None) -> SimResult:
    """Run the paths; `statistic(values, days)` -> per-path statistic,
    `event(stat)` -> bool per path."""
    rng = np.random.default_rng(seed)
    k = len(days)
    rho = np.array([rho_climate if d.kind == "climatology" else rho_forecast for d in days])
    z = ar1_paths(n, k, rho, rng)
    mu = np.array([d.mean for d in days], dtype=float)
    sd = np.array([d.sd if d.kind != "observed" else 0.0 for d in days], dtype=float)
    out = {}
    for f in (1.0,) + SENSITIVITY:
        vals = mu + z * (sd * f)
        stat = statistic(vals, days)
        hit = event(stat)
        out[f] = (float(hit.mean()), stat)
    p, stat = out[1.0]
    probs = [v[0] for v in out.values()]
    hist = {}
    if hist_bins is not None:
        counts, edges = np.histogram(stat, bins=hist_bins)
        hist = {"edges": [round(float(e), 2) for e in edges], "counts": counts.tolist()}
    else:
        vals, counts = np.unique(np.round(stat, 1), return_counts=True)
        if len(vals) <= 60:
            hist = {"values": vals.tolist(), "counts": counts.tolist()}
    return SimResult(prob=p, mc_se=math.sqrt(max(p * (1 - p), 1e-12) / n),
                     prob_lo=min(probs), prob_hi=max(probs), n_paths=n, seed=seed, hist=hist)


# ---------------------------------------------------------------- engine
class ExtendedEngine:
    def __init__(self, store, pit, calib: Calibration, stations: dict, n_paths: int = DEFAULT_PATHS):
        self.store = store
        self.pit = pit
        self.calib = calib
        self.stations = stations
        self.n_paths = n_paths

    # which settled variable each class is built from
    @staticmethod
    def variable_for(c: dict) -> str:
        if c["aggregation"] == "RUN_LENGTH":
            return "temp_mean_hourly"
        if c["aggregation"] == "ANY_HOURLY":
            return "temp_min" if c["operator"] in ("<", "<=") else "temp_max"
        if c["variable"] in ("hdd", "cdd"):
            return "temp_mid"
        return c["variable"]

    def hourly_extreme_offset(self, station: str, kind: str = "min") -> float:
        """Month's lowest HOURLY value sits above the climate-report minimum
        (which also sees 5-minute readings). Fitted from TWC hourly vs CLI;
        0.5F until there are data."""
        rows = self.store.query(
            "SELECT h.local_date d, MIN(h.temp_f) hmin, MAX(h.temp_f) hmax, o.value v "
            "FROM wx_hourly_obs h JOIN wx_observations o ON o.station=h.station "
            "AND o.obs_date=h.local_date AND o.variable=? AND o.status IN ('FINAL','REVISED') "
            "WHERE h.station=? GROUP BY h.local_date HAVING COUNT(*) >= 20",
            ("temp_min" if kind == "min" else "temp_max", station))
        diffs = [((r["hmin"] - r["v"]) if kind == "min" else (r["v"] - r["hmax"]))
                 for r in rows if r["v"] is not None]
        k = 10.0
        return (sum(diffs) + k * 0.5) / (len(diffs) + k)

    def day_inputs(self, c: dict, var: str) -> list[DayInput]:
        st = self.stations.get(c["station"]) or {}
        tz = ZoneInfo(st.get("timezone") or "America/New_York")
        today = datetime.now(timezone.utc).astimezone(tz).date()
        start, end = date.fromisoformat(c["window_start"]), date.fromisoformat(c["window_end"])
        out: list[DayInput] = []
        last_anom = None
        last_fc_day = None
        d = start
        while d <= end:
            ds = d.isoformat()
            obs = self._observed(c["station"], var, ds) if d <= today else None
            if obs is not None and (d < today or obs.get("status") in ("FINAL", "REVISED")):
                out.append(DayInput(ds, "observed", obs["value"], 0.0, obs.get("hours"),
                                    note=obs.get("status", "")))
                d += timedelta(days=1)
                continue
            lead = max(1, (d - today).days)
            centre = self._centre(c["station"], var, ds, lead)
            clim = self._clim(c["station"], var, d)
            if centre is not None:
                sd = self._sd(var, lead, c["station"], d)
                if clim and clim[1]:
                    sd = min(sd, clim[1])
                out.append(DayInput(ds, "forecast", centre, sd, lead=lead,
                                    note="today (partial day, no intraday obs)" if d == today else ""))
                if clim:
                    last_anom, last_fc_day = centre - clim[0], d
            elif clim:
                anom = 0.0
                if last_anom is not None:
                    anom = last_anom * (CLIMATE_RHO ** (d - last_fc_day).days)
                out.append(DayInput(ds, "climatology", clim[0] + anom, clim[1], lead=lead))
            else:
                raise LookupError(f"no forecast or climatology for {c['station']} {var} {ds}")
            d += timedelta(days=1)
        return out

    # -- inputs ---------------------------------------------------------
    def _observed(self, station: str, var: str, day: str) -> dict | None:
        if var == "temp_mid":
            mx = self.pit.latest_observation(station, "temp_max", day)
            mn = self.pit.latest_observation(station, "temp_min", day)
            if mx and mn:
                return {"value": (mx["value"] + mn["value"]) / 2.0, "status": mx["status"]}
            return None
        return self.pit.latest_observation(station, var, day)

    def _centre(self, station: str, var: str, day: str, lead: int) -> float | None:
        if var == "temp_mid":
            a = self._centre(station, "temp_max", day, lead)
            b = self._centre(station, "temp_min", day, lead)
            return None if a is None or b is None else (a + b) / 2.0
        values = self.pit.latest_forecast(station, var, day)
        return self.calib.centre(var, lead, station, values) if values else None

    def _sd(self, var: str, lead: int, station: str, day: date) -> float:
        if var == "temp_mid":
            a = self.calib.sd("temp_max", lead, station, day) or 3.0
            b = self.calib.sd("temp_min", lead, station, day) or 3.0
            return math.sqrt(a * a + b * b + 2 * 0.5 * a * b) / 2.0   # max/min errors correlate
        return self.calib.sd(var, lead, station, day) or 3.0

    def _clim(self, station: str, var: str, d: date):
        if var == "temp_mid":
            return self.calib.climatology(station, "temp_mean", d)
        return self.calib.climatology(station, var, d)

    # -- one contract -----------------------------------------------------
    def price(self, c: dict, n: int | None = None) -> tuple[SimResult, list[DayInput], dict]:
        var = self.variable_for(c)
        days = self.day_inputs(c, var)
        n = n or self.n_paths
        seed = stable_seed(c["ticker"], datetime.now(timezone.utc).strftime("%Y%m%d%H"))
        rho_f = self.calib.rho("temp_mean_hourly" if var == "temp_mid" else var)
        op, thr, hi = c["operator"], c["threshold"], c.get("threshold_hi")
        extra: dict = {"variable": var, "rho": rho_f}
        agg = c["aggregation"]
        if agg == "RUN_LENGTH":
            k = int(c["run_k"] or 1)
            min_h = c.get("min_hours") or 0
            broken = np.array([(d.kind == "observed" and d.hours is not None and d.hours < min_h)
                               for d in days])

            def stat(vals, _days):
                ok = compare(round_half_up(vals), op, thr) & ~broken
                return longest_run(ok)

            res = simulate(days, stat, n, seed, rho_f, event=lambda s: s >= k)
        elif agg == "ANY_HOURLY":
            off = self.hourly_extreme_offset(c["station"], "min" if op in ("<", "<=") else "max")
            extra["hourly_offset_f"] = round(off, 2)
            lo_side = op in ("<", "<=")

            def stat(vals, _days):
                if lo_side:
                    return (vals + off).min(axis=1)
                return (vals - off).max(axis=1)

            res = simulate(days, stat, n, seed, rho_f, event=lambda s: compare(s, op, thr),
                           hist_bins=24)
        elif agg in ("WINDOW_MAX", "WINDOW_MIN"):
            def stat(vals, _days):
                r = round_half_up(vals)
                return r.max(axis=1) if agg == "WINDOW_MAX" else r.min(axis=1)

            res = simulate(days, stat, n, seed, rho_f, event=lambda s: compare(s, op, thr, hi))
        elif agg == "CUMULATIVE" and c["variable"] in ("hdd", "cdd"):
            base = 65.0
            heating = c["variable"] == "hdd"
            obs_dd = [self._observed_degree_days(c["station"], d.day, heating) if d.kind == "observed"
                      else None for d in days]

            def stat(vals, _days):
                mean_r = round_half_up(vals)
                dd = np.maximum(0.0, (base - mean_r) if heating else (mean_r - base))
                for i, v in enumerate(obs_dd):
                    if v is not None:
                        dd[:, i] = v          # the published figure, not our recompute
                return dd.sum(axis=1)

            res = simulate(days, stat, n, seed, rho_f, event=lambda s: compare(s, op, thr),
                           hist_bins=30)
        else:
            raise ValueError(f"no simulator for {c['cls']}/{agg}")
        return res, days, extra

    def _observed_degree_days(self, station: str, day: str, heating: bool) -> float | None:
        o = self.pit.latest_observation(station, "hdd" if heating else "cdd", day)
        return o["value"] if o else None

    def run(self, contracts: list[dict], quotes: dict | None = None) -> list[int]:
        """Price each contract, record a wx_model_runs row. Contracts of one
        event share inputs, so each event is simulated per market cheaply."""
        ids = []
        quotes = quotes or {}
        for c in contracts:
            try:
                res, days, extra = self.price(c)
            except Exception as exc:  # noqa: BLE001
                log.warning("WX EXTENDED  %s not priced: %s", c.get("ticker"), exc)
                continue
            q = quotes.get(c["ticker"]) or {}
            inputs = {"days": [d.__dict__ for d in days], **extra,
                      "observed_days": sum(d.kind == "observed" for d in days),
                      "forecast_days": sum(d.kind == "forecast" for d in days),
                      "climatology_days": sum(d.kind == "climatology" for d in days)}
            cur = self.store.execute(
                "INSERT INTO wx_model_runs (at, ticker, cls, method, model_version, calib_version, "
                "rules_version, seed, n_paths, prob, mc_se, prob_lo, prob_hi, stat_hist, inputs, "
                "market_yes_bid, market_yes_ask) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (utcnow(), c["ticker"], c["cls"], "monte_carlo_ar1", MODEL_VERSION,
                 self.calib.version, c.get("rules_version"), res.seed, res.n_paths,
                 round(res.prob, 5), round(res.mc_se, 5), round(res.prob_lo, 5),
                 round(res.prob_hi, 5), json.dumps(res.hist), json.dumps(inputs, default=str),
                 q.get("yes_bid", c.get("yes_bid")), q.get("yes_ask", c.get("yes_ask"))))
            ids.append(cur.lastrowid)
        return ids
