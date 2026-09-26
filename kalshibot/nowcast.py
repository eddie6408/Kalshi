"""Same-day nowcast: today's high from what has happened plus what is left.

Why this exists. On 26 September, in the 0-6h band the bot trades, the market
out-forecast it (Brier 0.070 vs our 0.127). The ensemble behind our number is
5-20 hours old by the afternoon; the market is watching the thermometer. Near
the end of the day the high is mostly already DECIDED by what has been
recorded, and the rest is a question about the next few hours - which a
model that re-runs every hour, checked against today's own readings, answers
far better than yesterday's ensemble.

The model, for a daily HIGH:

    H = max(L, R)
        L  the highest reading so far, less a margin (station readings run a
           little warm of the official climate report - 0.4-0.8F on
           2026-09-24 - and the report is a whole number)
        R  the highest temperature still to come, ~ Normal(mu, sigma)
           mu     = max over the remaining hours of (HRRR + bias)
           bias   = median(observed - HRRR) over the last few hours, clipped
           sigma  = sigma_base + sigma_per_hour * hours until that peak

    P(H <= x) = 0                      for x <  L
              = Phi((x - mu) / sigma)  for x >= L

and the mirror image for a daily LOW. Pure functions, no network: the data
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
    #: how far below the highest station reading the official value can land
    observation_margin_f: float = 1.0

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
    #: the lower (high) / upper (low) bound the observations set
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

    def cdf(self, x: float) -> float:
        """P(the day's extreme <= x)."""
        if x == math.inf:
            return 1.0
        if x == -math.inf:
            return 0.0
        if self.kind == "high":
            if self.bound is not None and x < self.bound:
                return 0.0
            if self.mean is None:
                return 1.0          # nothing left to happen; bound is the answer
            return _phi((x - self.mean) / self.sigma)
        # low: min(U, R). Certain to be <= U once U is reached.
        if self.bound is not None and x >= self.bound:
            return 1.0
        if self.mean is None:
            return 0.0
        return _phi((x - self.mean) / self.sigma)

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
    if observed is None:
        bound = None
    elif kind == "high":
        bound = observed - cfg.observation_margin_f
    else:
        bound = observed + cfg.observation_margin_f

    if not remaining:
        if observed is None:
            return None             # no readings and no hours left: nothing to say
        return Nowcast(kind, observed, bound, None, cfg.sigma_base_f, bias,
                       len(errors), 0.0, 0, notes=notes + ["no hours left in the day"])

    peak_t, peak_v = pick(remaining, key=lambda x: x[1])
    hours_to_peak = max(0.0, (peak_t - now).total_seconds() / 3600.0)
    sigma = min(cfg.sigma_max_f, cfg.sigma_base_f + cfg.sigma_per_hour_f * hours_to_peak)
    return Nowcast(kind, observed, bound, peak_v, sigma, bias, len(errors),
                   hours_to_peak, len(remaining), peak_t.isoformat(), notes)
