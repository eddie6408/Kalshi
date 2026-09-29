"""Same-day nowcast: today's high from what has happened plus what is left.

Why this exists. On 26 September, in the 0-6h band the bot trades, the market
out-forecast it (Brier 0.070 vs our 0.127). The ensemble behind our number is
5-20 hours old by the afternoon; the market is watching the thermometer. Near
the end of the day the high is mostly already DECIDED by what has been
recorded, and the rest is a question about the next few hours - which a
model that re-runs every hour, checked against today's own readings, answers
far better than yesterday's ensemble.

The model, for a daily HIGH:

    H = max(O, R)
        O  what the official report will say about the part of the day
           already recorded ~ Normal(highest reading so far + offset, sd).
           The report is not the station's hourly readings: they are whole
           degrees C (1.8F steps) and miss peaks between them. Measured over
           18 city-days (25-27 Sep): official minus station max ran -0.6 to
           +1.0F, centred near 0 - so the official high can land ABOVE the
           highest reading as well as below it.
        R  the highest temperature still to come, ~ Normal(mu, sigma)
           mu     = max over the remaining hours of (HRRR + bias)
           bias   = median(observed - HRRR) over the last few hours, clipped
           sigma  = sigma_base + sigma_per_hour * hours until that peak

    P(H <= x) = P(O <= x) * P(R <= x)          (max of two independent parts)

and for a daily LOW, min(O, R): P(L <= x) = 1 - P(O > x) * P(R > x).

Version 2 (29 Sep). Version 1 treated the highest reading less 1F as a hard
floor and, once the remaining hours ran cooler, put ALL the probability on
that floor - so a station max of 82.4F became "81, 99.5%" when the report
said 83. Three of six near-certain contracts on 26-27 Sep were wrong that way. Pure functions, no network: the data
is fetched by WeatherData and handed in, so this can be tested exactly and
replayed later from the journal.

The constants are deliberately conservative starting points. They are not
fitted yet - there is no same-day history to fit them on. That is safe for
one reason: the market-skill gate lets a forecaster trade only where it has
SHOWN it beats the market, so a badly set sigma costs a delay, not money.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from statistics import median


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass
class NowcastConfig:
    enabled: bool = True
    #: use the nowcast for same-day markets at most this far from the end of day
    max_lead_hours: float = 12.0
    #: recent hours of model-vs-station error used to correct the model
    bias_window_hours: float = 3.0
    min_obs_for_bias: int = 2
    max_bias_correction_f: float = 4.0
    #: uncertainty of the remaining-hours peak
    sigma_base_f: float = 0.8
    sigma_per_hour_f: float = 0.35
    sigma_max_f: float = 4.0
    #: the official report vs the highest station reading: mean offset and
    #: spread (measured -0.6..+1.0F over 18 city-days; sd set above that)
    observation_offset_f: float = 0.0
    observation_sd_f: float = 0.8

    @classmethod
    def from_dict(cls, d: dict | None) -> "NowcastConfig":
        d = d or {}
        f = {k: type(getattr(cls, k)) for k in cls.__dataclass_fields__}
        kw = {}
        for k, typ in f.items():
            if k in d and d[k] is not None:
                kw[k] = bool(d[k]) if typ is bool else typ(d[k])
        return cls(**kw)


@dataclass
class Nowcast:
    kind: str
    observed_extreme: float | None
    #: centre of the official value for the part of the day already recorded
    bound: float | None
    #: distribution of the remaining-hours extreme; None = the day is over
    mean: float | None
    sigma: float
    bias_f: float
    bias_samples: int
    hours_to_peak: float
    remaining_hours: int
    peak_time: str = ""
    notes: list[str] = field(default_factory=list)
    #: spread of the official value around `bound` (0 = exactly `bound`)
    observed_sd: float = 0.8

    @staticmethod
    def _norm_cdf(x: float, mean: float, sd: float) -> float:
        if sd <= 0:
            return 1.0 if x >= mean else 0.0
        return _phi((x - mean) / sd)

    def cdf(self, x: float) -> float:
        """P(the day's extreme <= x)."""
        if x == math.inf:
            return 1.0
        if x == -math.inf:
            return 0.0
        # each part: P(part <= x); a missing part never binds
        p_obs = (self._norm_cdf(x, self.bound, self.observed_sd)
                 if self.bound is not None else None)
        p_rem = (self._norm_cdf(x, self.mean, self.sigma)
                 if self.mean is not None else None)
        if self.kind == "high":             # max(O, R) <= x: both are
            return (1.0 if p_obs is None else p_obs) * (1.0 if p_rem is None else p_rem)
        # low: min(O, R) <= x unless both are above x
        above = (1.0 if p_obs is None else 1.0 - p_obs) * \
                (1.0 if p_rem is None else 1.0 - p_rem)
        return 1.0 - above

    def probability_between(self, lo: float | None, hi: float | None) -> float:
        """P(settled whole-degree value in [lo, hi]); None = open-ended."""
        a = (lo - 0.5) if lo is not None else -math.inf
        b = (hi + 0.5) if hi is not None else math.inf
        return max(0.0, self.cdf(b) - self.cdf(a))


def _nearest(series: list[tuple[dt.datetime, float]], t: dt.datetime,
             tolerance_min: float = 40.0) -> float | None:
    best = None
    for when, val in series:
        gap = abs((when - t).total_seconds()) / 60.0
        if gap <= tolerance_min and (best is None or gap < best[0]):
            best = (gap, val)
    return best[1] if best else None


def compute(now: dt.datetime, target: dt.date, kind: str,
            observations: list[tuple[dt.datetime, float]],
            model_hours: list[tuple[dt.datetime, float]],
            cfg: NowcastConfig | None = None) -> Nowcast | None:
    """Build the nowcast. All datetimes timezone-aware in the station's zone.

    Returns None when there is nothing to build it from (no model hours for
    the target day), so the caller falls back to the ensemble.
    """
    cfg = cfg or NowcastConfig()
    pick = max if kind == "high" else min
    day_model = [(t, v) for t, v in model_hours if t.date() == target]
    if not day_model:
        return None
    today_obs = [(t, v) for t, v in observations if t.date() == target and t <= now]
    notes: list[str] = []

    # --- how wrong has the model been today, lately? --------------------
    window_start = now - dt.timedelta(hours=cfg.bias_window_hours)
    errors = []
    for t, v in today_obs:
        if t < window_start:
            continue
        m = _nearest(model_hours, t)
        if m is not None:
            errors.append(v - m)
    bias = 0.0
    if len(errors) >= cfg.min_obs_for_bias:
        raw = median(errors)
        bias = max(-cfg.max_bias_correction_f, min(cfg.max_bias_correction_f, raw))
        if bias != raw:
            notes.append(f"bias {raw:+.1f}F clipped to {bias:+.1f}F")
    else:
        notes.append(f"only {len(errors)} recent reading(s) to check the model "
                     f"against; no bias correction")

    # --- what is left of the day ----------------------------------------
    remaining = [(t, v + bias) for t, v in day_model if t > now]
    observed = pick(v for _t, v in today_obs) if today_obs else None
    bound = None if observed is None else observed + cfg.observation_offset_f

    if not remaining:
        if observed is None:
            return None             # no readings and no hours left: nothing to say
        return Nowcast(kind, observed, bound, None, cfg.sigma_base_f, bias,
                       len(errors), 0.0, 0, notes=notes + ["no hours left in the day"],
                       observed_sd=cfg.observation_sd_f)

    peak_t, peak_v = pick(remaining, key=lambda x: x[1])
    hours_to_peak = max(0.0, (peak_t - now).total_seconds() / 3600.0)
    sigma = min(cfg.sigma_max_f, cfg.sigma_base_f + cfg.sigma_per_hour_f * hours_to_peak)
    return Nowcast(kind, observed, bound, peak_v, sigma, bias, len(errors),
                   hours_to_peak, len(remaining), peak_t.isoformat(), notes,
                   observed_sd=cfg.observation_sd_f)
