"""WX-3: source calibration by model x station x horizon x season, and the
sharpened daily distribution (shadow only).

Fitted on WX-2's point-in-time history - Open-Meteo previous-runs (each model
as issued 1-7 days before) - against what the markets settle on:

    temp_max / temp_min   NWS climate report values (ACIS = the NWS archive;
                          TWC republishes exactly these - checked 12/12)
    temp_mean_hourly      TWC's mean of hourly readings (the KXAVGT rule)

Per variable and lead day:
    bias per model (global, and per station shrunk toward global)
    weights  proportional to 1 / bias-corrected MSE
    sd of the weighted blend's error: pooled, per season, per station (shrunk)
    rho      lag-1 correlation of standardized blend errors across
             consecutive valid days from the same issue - the temporal
             dependence WX-4 simulates with

Train / validate / out-of-sample: the last `test_frac` of dates is held out;
the report scores the train-fitted blend there (RMSE, and the share of
outcomes inside the 80% and 90% intervals - calibrated dispersion reads
80 / 90).

Nothing here prices a live order. The daily "sharp" model is a new version
logged beside the live one (wx_daily_shadow) until settled data shows which
is better.
"""
from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean

log = logging.getLogger(__name__)

CALIB_VERSION = "wxcal@1"
SHARP_VERSION = "daily-sharp@1"
VARIABLES = ("temp_max", "temp_min", "temp_mean_hourly")
MAX_LEAD = 7
SEASONS = {12: "DJF", 1: "DJF", 2: "DJF", 3: "MAM", 4: "MAM", 5: "MAM",
           6: "JJA", 7: "JJA", 8: "JJA", 9: "SON", 10: "SON", 11: "SON"}


def season_of(d: str | date) -> str:
    m = (date.fromisoformat(d) if isinstance(d, str) else d).month
    return SEASONS[m]


def _rmse(xs):
    return math.sqrt(sum(x * x for x in xs) / len(xs)) if xs else None


def _sd(xs):
    if len(xs) < 2:
        return None
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# ---------------------------------------------------------------- truth
def load_truth(store, variables=VARIABLES) -> dict[tuple, float]:
    """{(station, date, variable): settled value}."""
    truth: dict[tuple, float] = {}
    for r in store.query("SELECT station, obs_date, maxt, mint FROM wx_climatology "
                         "WHERE obs_date >= '2023-01-01'"):
        if r["maxt"] is not None:
            truth[(r["station"], r["obs_date"], "temp_max")] = r["maxt"]
        if r["mint"] is not None:
            truth[(r["station"], r["obs_date"], "temp_min")] = r["mint"]
    # TWC: final values win where present (identical to the CLI in practice);
    # the hourly mean exists only here.
    for r in store.query(
            "SELECT station, obs_date, variable, value, status, hours FROM wx_observations "
            "WHERE source='twc_kalshi' AND value IS NOT NULL AND quality IN ('VALID','STALE') "
            "AND variable IN ('temp_max','temp_min','temp_mean_hourly') "
            "ORDER BY CASE status WHEN 'PRELIMINARY' THEN 0 ELSE 1 END, retrieved_at"):
        if r["variable"] == "temp_mean_hourly" and (r["hours"] or 0) < 18:
            continue
        truth[(r["station"], r["obs_date"], r["variable"])] = r["value"]
    return truth


def load_history(store, variables=VARIABLES) -> list[dict]:
    """Previous-runs rows joined into {station, date, variable, lead, models}."""
    grouped: dict[tuple, dict] = defaultdict(dict)
    for r in store.query(
            "SELECT model, station, valid_date, variable, lead_days, value FROM wx_forecasts "
            "WHERE source='open_meteo_previous_runs' AND lead_days BETWEEN 1 AND ?", (MAX_LEAD,)):
        if r["variable"] in variables:
            grouped[(r["station"], r["valid_date"], r["variable"], r["lead_days"])][r["model"]] = r["value"]
    return [{"station": k[0], "date": k[1], "variable": k[2], "lead": k[3], "models": v}
            for k, v in grouped.items()]


# ------------------------------------------------------------------ fit
def _fit_block(rows, shrink):
    """bias / station bias / weights for one (variable, lead)."""
    models = sorted({m for r in rows for m in r["models"]})
    gb, sb, mse = {}, {}, {}
    for m in models:
        errs = [(r["station"], r["models"][m] - r["truth"]) for r in rows
                if r["models"].get(m) is not None]
        if len(errs) < 30:
            continue
        gb[m] = mean(e for _, e in errs)
        per: dict[str, list] = defaultdict(list)
        for st, e in errs:
            per[st].append(e)
        for st, es in per.items():
            sb.setdefault(st, {})[m] = (sum(es) + shrink * gb[m]) / (len(es) + shrink)
        mse[m] = sum((e - sb[st][m]) ** 2 for st, e in errs) / len(errs)
    inv = {m: 1.0 / max(v, 0.05) for m, v in mse.items()}
    tot = sum(inv.values()) or 1.0
    return {"weights": {m: v / tot for m, v in inv.items()}, "global_bias": gb,
            "station_bias": sb, "mse": mse}


def blend_value(block: dict, station: str, values: dict) -> float | None:
    w = block.get("weights") or {}
    used = {m: v for m, v in (values or {}).items() if v is not None and w.get(m, 0) > 0}
    if not used:
        return None
    sb = (block.get("station_bias") or {}).get(station) or {}
    gb = block.get("global_bias") or {}
    tot = sum(w[m] for m in used)
    return sum(w[m] * (v - sb.get(m, gb.get(m, 0.0))) for m, v in used.items()) / tot


def fit(store, test_frac: float = 0.25, shrink: float = 20.0, as_of: str | None = None
        ) -> tuple[dict, dict]:
    """(params, report). Params are fitted on all rows; the report scores a
    fit on the earlier dates against the held-out later ones."""
    truth = load_truth(store)
    hist = [dict(r, truth=truth[(r["station"], r["date"], r["variable"])])
            for r in load_history(store)
            if (r["station"], r["date"], r["variable"]) in truth
            and (as_of is None or r["date"] < as_of)]
    dates = sorted({r["date"] for r in hist})
    split = dates[int(len(dates) * (1 - test_frac))] if len(dates) > 20 else None

    def fit_all(rows):
        out = {}
        for var in VARIABLES:
            for lead in range(1, MAX_LEAD + 1):
                rk = [r for r in rows if r["variable"] == var and r["lead"] == lead]
                if len(rk) < 60:
                    continue
                b = _fit_block(rk, shrink)
                res = [(r, bv - r["truth"]) for r in rk
                       if (bv := blend_value(b, r["station"], r["models"])) is not None]
                errs = [e for _, e in res]
                b["bias_after"] = mean(errs) if errs else 0.0
                b["sd"] = _sd(errs)
                b["n"] = len(errs)
                # per season, per station: shrunk toward the pooled sd
                by_season: dict[str, list] = defaultdict(list)
                by_station: dict[str, list] = defaultdict(list)
                for r, e in res:
                    by_season[season_of(r["date"])].append(e)
                    by_station[r["station"]].append(e)
                k = 30.0
                pooled_var = (b["sd"] or 2.0) ** 2
                b["sd_season"] = {s: math.sqrt((sum(x * x for x in es) + k * pooled_var) / (len(es) + k))
                                  for s, es in by_season.items()}
                b["sd_station"] = {s: math.sqrt((sum(x * x for x in es) + k * pooled_var) / (len(es) + k))
                                   for s, es in by_station.items()}
                out[f"{var}|{lead}"] = b
        # temporal dependence: error at (V, lead d) vs (V+1, lead d+1), same issue
        rho = {}
        for var in VARIABLES:
            e = {}
            for r in rows:
                if r["variable"] != var:
                    continue
                b = out.get(f"{var}|{r['lead']}")
                if not b or not b.get("sd"):
                    continue
                bv = blend_value(b, r["station"], r["models"])
                if bv is not None:
                    e[(r["station"], r["date"], r["lead"])] = (bv - r["truth"] - b["bias_after"]) / b["sd"]
            pairs = []
            for (st, d, ld), z in e.items():
                if ld >= MAX_LEAD:
                    continue
                nxt = (date.fromisoformat(d) + timedelta(days=1)).isoformat()
                z2 = e.get((st, nxt, ld + 1))
                if z2 is not None:
                    pairs.append((z, z2))
            if len(pairs) >= 50:
                mx, my = mean(p[0] for p in pairs), mean(p[1] for p in pairs)
                cov = sum((a - mx) * (b - my) for a, b in pairs) / len(pairs)
                vx = sum((a - mx) ** 2 for a, _ in pairs) / len(pairs)
                vy = sum((b - my) ** 2 for _, b in pairs) / len(pairs)
                rho[var] = {"rho": max(-0.2, min(0.95, cov / math.sqrt(vx * vy))) if vx and vy else 0.0,
                            "n": len(pairs)}
        return out, rho

    report: dict = {"rows": len(hist), "stations": len({r["station"] for r in hist}),
                    "dates": [dates[0], dates[-1]] if dates else None, "split": split}
    if split:
        train = [r for r in hist if r["date"] < split]
        test = [r for r in hist if r["date"] >= split]
        blocks, _ = fit_all(train)
        oos = {}
        for key, b in blocks.items():
            var, lead = key.split("|")
            rk = [r for r in test if r["variable"] == var and r["lead"] == int(lead)]
            errs, z = [], []
            for r in rk:
                bv = blend_value(b, r["station"], r["models"])
                if bv is None:
                    continue
                e = bv - r["truth"]
                errs.append(e)
                z.append(abs(e - b["bias_after"]) / (b["sd"] or 1.0))
            eq = [mean(v for v in r["models"].values() if v is not None) - r["truth"] for r in rk
                  if any(v is not None for v in r["models"].values())]
            single = {}
            for m in b["weights"]:
                es = [r["models"][m] - r["truth"] for r in rk if r["models"].get(m) is not None]
                if len(es) >= 30:
                    single[m] = _rmse(es)
            if errs:
                oos[key] = {"n": len(errs), "rmse_blend": _rmse(errs), "rmse_equal": _rmse(eq),
                            "rmse_best_single": min(single.values()) if single else None,
                            "best_single": min(single, key=single.get) if single else None,
                            "cover80": sum(x <= 1.2816 for x in z) / len(z),
                            "cover90": sum(x <= 1.6449 for x in z) / len(z)}
        report["out_of_sample"] = oos
    blocks, rho = fit_all(hist)
    params = {"version": CALIB_VERSION, "fitted_at": datetime.now(timezone.utc).isoformat(),
              "blocks": blocks, "rho": rho, "mean_offset": fit_mean_offset(store)}
    return params, report


def fit_mean_offset(store, shrink: float = 10.0) -> dict:
    """TWC hourly mean minus the (max+min)/2 midpoint, per station: turns
    ACIS climatology (midpoints) into hourly-mean climatology."""
    rows = store.query(
        "SELECT a.station, a.value hm, b.value mx, c.value mn FROM wx_observations a "
        "JOIN wx_observations b ON b.station=a.station AND b.obs_date=a.obs_date "
        "AND b.variable='temp_max' AND b.status=a.status AND b.source=a.source "
        "JOIN wx_observations c ON c.station=a.station AND c.obs_date=a.obs_date "
        "AND c.variable='temp_min' AND c.status=a.status AND c.source=a.source "
        "WHERE a.variable='temp_mean_hourly' AND a.hours >= 18 AND a.status IN ('FINAL','REVISED')")
    diffs: dict[str, list] = defaultdict(list)
    for r in rows:
        if None not in (r["hm"], r["mx"], r["mn"]):
            diffs[r["station"]].append(r["hm"] - (r["mx"] + r["mn"]) / 2.0)
    allv = [x for v in diffs.values() for x in v]
    g = mean(allv) if allv else -0.5
    out = {"_global": g}
    for st, v in diffs.items():
        out[st] = (sum(v) + shrink * g) / (len(v) + shrink)
    return out


def write_calibration(store, params: dict, report: dict, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps({"params": params, "report": report}, default=str))
    now = params["fitted_at"]
    rows = []
    for key, b in params["blocks"].items():
        var, lead = key.split("|")
        rows.append((CALIB_VERSION, "BLEND", "ALL", var, int(lead), "ALL", "all", b["n"],
                     b["bias_after"], None, None, b["sd"], now))
        for s, sd in b["sd_season"].items():
            rows.append((CALIB_VERSION, "BLEND", "ALL", var, int(lead), s, "all", None,
                         None, None, None, sd, now))
        for m, gb in b["global_bias"].items():
            rows.append((CALIB_VERSION, m, "ALL", var, int(lead), "ALL", "all", None, gb, None,
                         math.sqrt(b["mse"][m]), None, now))
    for key, o in (report.get("out_of_sample") or {}).items():
        var, lead = key.split("|")
        rows.append((CALIB_VERSION, "BLEND", "ALL", var, int(lead), "ALL", "oos", o["n"], None,
                     None, o["rmse_blend"], None, now))
    store.execute("DELETE FROM wx_calibration WHERE version=?", (CALIB_VERSION,))
    store.executemany("INSERT INTO wx_calibration (version, model, station, variable, lead_days, "
                      "season, split, n, bias, mae, rmse, sd, fitted_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)


# --------------------------------------------------------------- params
class Calibration:
    """Read side, used by the shadow daily model and the extended engine."""

    def __init__(self, params: dict | None, store=None):
        self.p = params or {}
        self.store = store
        self._clim: dict = {}

    @classmethod
    def load(cls, path: str | Path, store=None) -> "Calibration":
        try:
            raw = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            return cls(None, store)
        return cls(raw.get("params"), store)

    @property
    def fitted(self) -> bool:
        return bool(self.p.get("blocks"))

    @property
    def version(self) -> str:
        return f"{self.p.get('version', CALIB_VERSION)}:{str(self.p.get('fitted_at', ''))[:10]}"

    def block(self, variable: str, lead: int) -> dict | None:
        lead = max(1, min(MAX_LEAD, int(lead)))
        blocks = self.p.get("blocks") or {}
        for ld in [lead] + [x for x in range(MAX_LEAD, 0, -1) if x != lead]:
            b = blocks.get(f"{variable}|{ld}")
            if b:
                return b
        return None

    def centre(self, variable: str, lead: int, station: str, values: dict) -> float | None:
        b = self.block(variable, lead)
        if not b:
            return None
        v = blend_value(b, station, values)
        return None if v is None else v - (b.get("bias_after") or 0.0)

    def sd(self, variable: str, lead: int, station: str | None = None,
           when: str | date | None = None) -> float | None:
        """Forecast error sd at this lead day. Past 7 days it grows along the
        fitted 1..7 trend; callers cap it at the climatological sd."""
        blocks = self.p.get("blocks") or {}
        pts = [(ld, blocks[f"{variable}|{ld}"]["sd"]) for ld in range(1, MAX_LEAD + 1)
               if f"{variable}|{ld}" in blocks and blocks[f"{variable}|{ld}"].get("sd")]
        if not pts:
            return None
        if lead > MAX_LEAD and len(pts) >= 2:
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            mx, my = mean(xs), mean(ys)
            slope = (sum((x - mx) * (y - my) for x, y in zip(xs, ys))
                     / (sum((x - mx) ** 2 for x in xs) or 1.0))
            base = my + max(0.0, slope) * (lead - mx)
            b = blocks.get(f"{variable}|{pts[-1][0]}")
        else:
            b = self.block(variable, lead)
            base = b["sd"]
        s = base
        if station and b and station in (b.get("sd_station") or {}):
            s = b["sd_station"][station] * (base / (b["sd"] or base))
        if when is not None and b and (b.get("sd_season") or {}).get(season_of(when)):
            s *= b["sd_season"][season_of(when)] / (b["sd"] or s)
        return max(0.6, s)

    def rho(self, variable: str) -> float:
        return float(((self.p.get("rho") or {}).get(variable) or {}).get("rho", 0.6))

    def mean_offset(self, station: str) -> float:
        mo = self.p.get("mean_offset") or {}
        return float(mo.get(station, mo.get("_global", -0.5)))

    # ------------------------------------------------------ climatology
    def climatology(self, station: str, variable: str, day: date, window: int = 7
                    ) -> tuple[float, float] | None:
        """(mean, sd) of the settled variable for this calendar day over the
        ACIS record, +/- `window` days. temp_mean_hourly = midpoint + the
        station's measured hourly-mean offset."""
        key = (station, variable, day.month, day.day)
        if key in self._clim:
            return self._clim[key]
        if self.store is None:
            return None
        col = {"temp_max": "maxt", "temp_min": "mint", "temp_mean_hourly": "avgt",
               "temp_mean": "avgt", "hdd": "avgt"}.get(variable)
        if not col:
            return None
        doys = set()
        for k in range(-window, window + 1):
            d = day + timedelta(days=k)
            doys.add(f"{d.month:02d}-{d.day:02d}")
        rows = self.store.query(f"SELECT substr(obs_date,6,5) md, {col} v FROM wx_climatology "
                                f"WHERE station=? AND {col} IS NOT NULL", (station,))
        vals = [r["v"] for r in rows if r["md"] in doys]
        if len(vals) < 60:
            self._clim[key] = None
            return None
        m, s = mean(vals), _sd(vals)
        if variable == "temp_mean_hourly":
            m += self.mean_offset(station)
        self._clim[key] = (m, s)
        return self._clim[key]


# ------------------------------------------------ integer contract maths
def prob_integer(op: str, lo: float | None, hi: float | None, mu: float, sd: float) -> float:
    """P(contract YES) when the settled value is a whole number (the climate
    report) and the forecast is N(mu, sd) on the continuous temperature."""
    def cdf(x):
        return norm_cdf((x - mu) / sd)
    if op == ">":            # T > lo  <=>  T >= floor(lo)+1
        return 1.0 - cdf(math.floor(lo) + 0.5)
    if op == ">=":
        return 1.0 - cdf(math.ceil(lo) - 0.5)
    if op == "<":            # T < hi  <=>  T <= ceil(hi)-1
        return cdf(math.ceil(hi) - 0.5)
    if op == "<=":
        return cdf(math.floor(hi) + 0.5)
    if op == "between":
        return max(0.0, cdf(math.floor(hi) + 0.5) - cdf(math.ceil(lo) - 0.5))
    raise ValueError(op)


def contract_bounds(c: dict) -> tuple[str, float | None, float | None]:
    """(op, lo, hi) of a daily contract from Kalshi's structured strike."""
    st = (c.get("strike_type") or "").lower()
    f, cap = c.get("floor_strike"), c.get("cap_strike")
    if st == "greater":
        return ">", f, None
    if st == "greater_or_equal":
        return ">=", f, None
    if st == "less":
        return "<", None, cap
    if st == "less_or_equal":
        return "<=", None, cap
    if st == "between":
        return "between", f, cap
    return c.get("operator") or "", c.get("threshold"), c.get("threshold_hi")
