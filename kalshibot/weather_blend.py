"""Multi-model blend for the weather strategy: pure functions, no network.

The ensemble (GEFS / ICON / ECMWF ENS members) gives the SHAPE of the
distribution - how uncertain tomorrow's high is. Where its CENTRE sits is
better told by a blend of the deterministic and post-processed models the
spec names, each corrected for its own bias at that station:

    NOAA NBM (station-calibrated guidance), GFS, ECMWF IFS, ECMWF AIFS,
    NOAA HRRR, plus Canada's GEM and Germany's ICON.

    centre = alpha * ensemble_mean + (1 - alpha) * sum_m w_m (value_m - bias_m,station)

The ensemble members are shifted so their mean lands on that centre; their
spread, the station-representativeness kernel, the same-day nowcast and the
NWS cross-check are all unchanged. Nothing is replaced - the other sources
are blended in.

`fit()` measures it all against the value Kalshi actually settles on (The
Weather Company's official daily reports, weather.com/kalshi): per-model
bias (global, and per station shrunk toward the global), weights inversely
proportional to each model's bias-corrected squared error, and the blend's
residual spread - with an out-of-sample check on the most recent days.
`alpha`, the ensemble's share, cannot be fitted from the archive (Open-Meteo
keeps no ensemble history); it is a config value until the live forecasts
table has enough settled days to fit it.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean

MODELS = ("ncep_nbm_conus", "gfs_global", "ecmwf_ifs025", "ecmwf_aifs025_single",
          "ncep_hrrr_conus", "gem_seamless", "icon_seamless")
LABELS = {"ncep_nbm_conus": "NBM", "gfs_global": "GFS", "ecmwf_ifs025": "ECMWF IFS",
          "ecmwf_aifs025_single": "ECMWF AIFS", "ncep_hrrr_conus": "HRRR",
          "gem_seamless": "GEM", "icon_seamless": "ICON"}
BLEND_VERSION = "blend@1"


@dataclass
class BlendParams:
    models: tuple = MODELS
    #: {kind: {model: weight}}
    weights: dict = field(default_factory=dict)
    #: {kind: {model: bias}}  (model minus truth, deg F)
    global_bias: dict = field(default_factory=dict)
    #: {kind: {station: {model: bias}}}  shrunk toward global
    station_bias: dict = field(default_factory=dict)
    #: {kind: sd of the blend's error}
    residual_sd: dict = field(default_factory=dict)
    alpha: float = 0.25
    fitted_at: str = ""
    fitted: bool = False

    @classmethod
    def load(cls, path: str | Path, alpha: float = 0.25) -> "BlendParams":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls(alpha=alpha)
        p = raw.get("params") or {}
        return cls(models=tuple(p.get("models") or MODELS), weights=p.get("weights") or {},
                   global_bias=p.get("global_bias") or {},
                   station_bias=p.get("station_bias") or {},
                   residual_sd=p.get("residual_sd") or {}, alpha=alpha,
                   fitted_at=raw.get("fitted_at", ""), fitted=bool(p.get("weights")))

    def bias(self, kind: str, station: str, model: str) -> float:
        st = (self.station_bias.get(kind) or {}).get(station) or {}
        if model in st:
            return float(st[model])
        return float((self.global_bias.get(kind) or {}).get(model, 0.0))


@dataclass
class Blend:
    centre: float
    model_blend: float
    used: dict                       # model -> bias-corrected value
    raw: dict                        # model -> raw value
    spread: float                    # max - min of the raw model values
    weights: dict


def blend(params: BlendParams, kind: str, station: str, values: dict,
          ensemble_mean: float | None) -> Blend | None:
    """The blended centre, or None if no model has a value (or the blend is
    not fitted - an unfitted blend would be a guess, so it does nothing)."""
    if not params.fitted:
        return None
    w_all = params.weights.get(kind) or {}
    raw = {m: float(v) for m, v in (values or {}).items()
           if v is not None and m in params.models and w_all.get(m, 0) > 0}
    if not raw:
        return None
    used = {m: v - params.bias(kind, station, m) for m, v in raw.items()}
    wsum = sum(w_all[m] for m in used)
    mb = sum(w_all[m] * v for m, v in used.items()) / wsum
    centre = (params.alpha * ensemble_mean + (1 - params.alpha) * mb
              if ensemble_mean is not None and not math.isnan(ensemble_mean) else mb)
    return Blend(centre, mb, used, raw, max(raw.values()) - min(raw.values()),
                 {m: w_all[m] / wsum for m in used})


# ------------------------------------------------------------------ fitting
def _rmse(xs):
    return math.sqrt(sum(x * x for x in xs) / len(xs)) if xs else None


def fit(rows: list[dict], split: str | None = None, shrink: float = 20.0,
        models: tuple = MODELS) -> tuple[dict, dict]:
    """rows: {"station", "date" (ISO), "kind", "truth", "models": {m: v}}.
    Returns (params, report). With `split`, the report scores a fit on rows
    before it against rows on/after it; the params are always fitted on all."""
    def _fit(train):
        out = {"models": list(models), "weights": {}, "global_bias": {},
               "station_bias": {}, "residual_sd": {}}
        for kind in ("high", "low"):
            rk = [r for r in train if r["kind"] == kind]
            gb, sb, mse = {}, {}, {}
            for m in models:
                errs = [(r["station"], r["models"][m] - r["truth"]) for r in rk
                        if r["models"].get(m) is not None]
                if len(errs) < 30:
                    continue
                gb[m] = mean(e for _, e in errs)
                per: dict[str, list[float]] = {}
                for st, e in errs:
                    per.setdefault(st, []).append(e)
                for st, es in per.items():
                    sb.setdefault(st, {})[m] = (sum(es) + shrink * gb[m]) / (len(es) + shrink)
                corrected = [e - sb[st][m] for st, e in errs]
                mse[m] = sum(x * x for x in corrected) / len(corrected)
            if not mse:
                continue
            inv = {m: 1.0 / max(v, 0.05) for m, v in mse.items()}
            tot = sum(inv.values())
            out["weights"][kind] = {m: v / tot for m, v in inv.items()}
            out["global_bias"][kind] = gb
            out["station_bias"][kind] = sb
            p = BlendParams(models=models, weights=out["weights"], global_bias=out["global_bias"],
                            station_bias=out["station_bias"], alpha=0.0, fitted=True)
            res = [b.centre - r["truth"] for r in rk
                   if (b := blend(p, kind, r["station"], r["models"], None)) is not None]
            out["residual_sd"][kind] = _rmse(res)
        return out

    def _score(params, test):
        p = BlendParams(models=tuple(params["models"]), weights=params["weights"],
                        global_bias=params["global_bias"], station_bias=params["station_bias"],
                        alpha=0.0, fitted=True)
        rep = {}
        for kind in ("high", "low"):
            rk = [r for r in test if r["kind"] == kind]
            if not rk:
                continue
            k = {"n": len(rk)}
            for m in models:
                e = [r["models"][m] - r["truth"] for r in rk if r["models"].get(m) is not None]
                if e:
                    k[m] = {"rmse_raw": _rmse(e), "mae_raw": mean(abs(x) for x in e), "n": len(e)}
            eq = [mean(v for v in r["models"].values() if v is not None) - r["truth"]
                  for r in rk if any(v is not None for v in r["models"].values())]
            k["equal_average"] = {"rmse": _rmse(eq), "mae": mean(abs(x) for x in eq) if eq else None}
            bl = [b.centre - r["truth"] for r in rk
                  if (b := blend(p, kind, r["station"], r["models"], None)) is not None]
            k["blend"] = {"rmse": _rmse(bl), "mae": mean(abs(x) for x in bl) if bl else None,
                          "within_1f": (sum(abs(x) <= 1.0 for x in bl) / len(bl)) if bl else None}
            rep[kind] = k
        return rep

    report = {}
    if split:
        train = [r for r in rows if r["date"] < split]
        test = [r for r in rows if r["date"] >= split]
        if train and test:
            report["out_of_sample"] = _score(_fit(train), test)
            report["split"] = split
    params = _fit(rows)
    report["in_sample"] = _score(params, rows)
    report["rows"] = len(rows)
    report["stations"] = len({r["station"] for r in rows})
    return params, report
