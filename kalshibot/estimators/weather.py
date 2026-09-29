"""Weather temperature estimator for Kalshi KXHIGH*/KXLOW* daily markets.

The idea in one paragraph
-------------------------
Kalshi's daily high-temperature markets settle on a single integer: the
official NWS climate report for one specific station. That means the whole
market is a bet on one number. If we can build an honest *probability
distribution* for that number, we can price every strike on the board at
once and compare each one to what the market is charging. Free ensemble
forecasts (dozens of model runs with slightly different starting conditions)
give us exactly that distribution. We widen it a little, because the model
grid point is not literally the thermometer at the airport, and because
models have biases the ensemble spread does not capture.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import re
from zoneinfo import ZoneInfo

from .. import nowcast as _nowcast
from ..models import Market
from ..weatherdata import DailyTempForecast, WeatherData
from .base import Estimate

log = logging.getLogger(__name__)

MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}

# KXHIGHNY-25SEP12-B72.5  ->  (25, SEP, 12)
DATE_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})")


def _to_utc(value):
    from ..sources import to_utc
    return to_utc(value)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


#: Bump when a model's maths or constants change, so every forecast and
#: trade names the model that produced it (research brief section 24).
MODEL_VERSIONS = {"ensemble": "1", "nowcast": "2"}

#: Worst source tier behind each model's number (1 = the organisation that
#: publishes it, 2 = an established redistributor - Open-Meteo).
MODEL_SOURCE_TIER = {"ensemble": 2, "nowcast": 2}


class WeatherTemperatureEstimator:
    name = "weather_temperature"

    def __init__(self, weather_cfg: dict, data: WeatherData | None = None):
        self.cfg = weather_cfg or {}
        self.series = {s["ticker_prefix"].upper(): s for s in self.cfg.get("series", [])}
        self.data = data or WeatherData(
            ensemble_models=self.cfg.get("ensemble_models"),
        )
        self.rep_sigma = float(self.cfg.get("station_representativeness_sigma_f", 1.6))
        self.min_sigma = float(self.cfg.get("min_sigma_f", 1.0))
        #: Kalshi settles from The Weather Company; our observations come from
        #: NWS api.weather.gov. Same station, different provider, so an
        #: observed extreme is close to the settling value but not identical.
        #: Give the "this outcome is already ruled out" logic this much slack
        #: in degrees F before it claims certainty.
        self.observation_margin = float(
            self.cfg.get("observation_margin_f", 0.5))
        self.fallback_sigma = {
            int(k): float(v) for k, v in (self.cfg.get("fallback_sigma_by_lead_hours") or {}).items()
        }
        #: Same-day nowcast (kalshibot/nowcast.py): observations so far plus
        #: the hourly HRRR, corrected against today's readings. Used for
        #: same-day markets within nowcast.max_lead_hours of the end of day.
        self.nowcast_cfg = _nowcast.NowcastConfig.from_dict(self.cfg.get("nowcast"))

    # ------------------------------------------------------------------
    def candidate_series(self) -> list[str]:
        return list(self.series.keys())

    def _series_for(self, market: Market) -> dict | None:
        ticker = (market.series_ticker or market.ticker).upper()
        for prefix, cfg in self.series.items():
            if ticker.startswith(prefix):
                return cfg
        return None

    # ------------------------------------------------------------------
    @staticmethod
    def parse_event_date(market: Market, tz_name: str) -> dt.date | None:
        m = DATE_RE.search(market.event_ticker or market.ticker or "")
        if not m:
            return None
        yy, mon, dd = m.groups()
        month = MONTHS.get(mon)
        if not month:
            return None
        try:
            return dt.date(2000 + int(yy), month, int(dd))
        except ValueError:
            return None

    @staticmethod
    def parse_strike(market: Market) -> tuple[float | None, float | None, str]:
        """Return (lower_inclusive, upper_inclusive, description) in degrees F.

        Prefers the API's structured strike fields; falls back to parsing the
        subtitle text, because Kalshi phrases these several ways.
        """
        st = (market.strike_type or "").lower()
        lo, hi = market.floor_strike, market.cap_strike

        # Kalshi's strike types do NOT share a convention, and getting this
        # wrong is silent and expensive:
        #
        #   between  floor=81 cap=82   -> "81 to 82"      INCLUSIVE both ends
        #   greater  floor=82          -> "83 or above"   STRICTLY greater
        #   less     cap=74            -> "73 or below"   STRICTLY less
        #
        # Treating `greater` as inclusive made P(>=82) masquerade as P(>=83),
        # which put a threshold inside a bucket it should have sat above and
        # produced a distribution that contradicted itself.
        #
        # Settlement is a whole number of degrees, so "strictly above 82"
        # is "83 or above".
        structured = None
        if st in ("between", "range") and lo is not None and hi is not None:
            structured = (lo, hi, f"{lo:g} to {hi:g}")
        elif st == "greater" and lo is not None:
            structured = (lo + 1, None, f"{lo + 1:g} or above")
        elif st == "greater_or_equal" and lo is not None:
            structured = (lo, None, f"{lo:g} or above")
        elif st == "less" and hi is not None:
            structured = (None, hi - 1, f"{hi - 1:g} or below")
        elif st == "less_or_equal" and hi is not None:
            structured = (None, hi, f"{hi:g} or below")

        text = f"{market.subtitle} {market.title}".strip()
        spoken = None
        m = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:°|degrees)?\s*(?:F)?\s*(?:to|-|–)\s*(-?\d+(?:\.\d+)?)", text)
        if m:
            a, b = float(m.group(1)), float(m.group(2))
            spoken = (min(a, b), max(a, b), f"{a:g} to {b:g}")
        if spoken is None:
            m = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:°|degrees)?\s*(?:F)?\s*or (?:above|higher|more|greater)", text, re.I)
            if m:
                v = float(m.group(1))
                spoken = (v, None, f"{v:g} or above")
        if spoken is None:
            m = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:°|degrees)?\s*(?:F)?\s*or (?:below|lower|less|fewer)", text, re.I)
            if m:
                v = float(m.group(1))
                spoken = (None, v, f"{v:g} or below")

        # Kalshi tells us the strike twice - in structured fields and in the
        # sentence shown to humans. When both are available they must agree.
        # If they do not, we have misunderstood the convention again, and the
        # only safe answer is to decline the market rather than guess which
        # one is right.
        if structured and spoken:
            if (structured[0], structured[1]) != (spoken[0], spoken[1]):
                return None, None, (f"CONFLICT: fields say {structured[2]!r}, "
                                    f"subtitle says {spoken[2]!r}")
            return structured
        if structured:
            return structured
        if spoken:
            return spoken
        if lo is not None and hi is not None:
            return lo, hi, f"{lo:g} to {hi:g}"
        return None, None, "unparsed"

    # ------------------------------------------------------------------
    def _sigma(self, fc: DailyTempForecast) -> float:
        """Total standard deviation to apply around each ensemble member."""
        if fc.n_members > 1:
            return max(self.min_sigma, self.rep_sigma)
        # Single deterministic value: widen by lead time instead.
        best = None
        for lead, sig in sorted(self.fallback_sigma.items()):
            if fc.lead_hours <= lead:
                best = sig
                break
        return max(self.min_sigma, best if best is not None else max(self.fallback_sigma.values(), default=4.0))

    def _probability_in_range(self, fc: DailyTempForecast, lo: float | None, hi: float | None) -> float:
        """P(settlement temperature falls in [lo, hi]) as whole degrees.

        The climate report is a whole number, so a bucket labelled "72 to 73"
        really means the true temperature lands in [71.5, 73.5).
        """
        a = (lo - 0.5) if lo is not None else float("-inf")
        b = (hi + 0.5) if hi is not None else float("inf")
        sigma = self._sigma(fc)

        members = list(fc.members)
        floor = fc.observed_extreme_so_far
        if floor is not None and fc.kind == "high":
            # The day's high is already AT LEAST what has been observed. That
            # is not a nudge to the forecast, it is a hard bound on the
            # support: P(high < observed) = 0.
            #
            # Clamping the member values alone is not enough - each member
            # still carries a Gaussian kernel that leaks probability below the
            # observed temperature. With 75.02F already recorded, this put
            # 30.4% on "74 or below", an outcome that could no longer happen.
            members = [max(m, floor) for m in members]
            a = max(a, floor - 0.5 - self.observation_margin)
        if floor is not None and fc.kind == "low":
            # Mirror image: the day's low is already AT MOST what was seen.
            members = [min(m, floor) for m in members]
            b = min(b, floor + 0.5 + self.observation_margin)

        if a >= b:
            return 0.0     # the observation has ruled this range out entirely

        total = 0.0
        for m in members:
            upper = 1.0 if b == float("inf") else _norm_cdf((b - m) / sigma)
            lower = 0.0 if a == float("-inf") else _norm_cdf((a - m) / sigma)
            total += max(0.0, upper - lower)
        return total / len(members)

    def _confidence(self, fc: DailyTempForecast, prob: float) -> float:
        """How much do we trust this number? 0..1.

        Drivers: how many ensemble members we got, how far out the forecast is,
        and how tight the ensemble agrees with itself.
        """
        if fc.n_members <= 1:
            base = 0.40                       # single deterministic model
        elif fc.n_members < 15:
            base = 0.62
        else:
            base = 0.78

        # Lead-time decay: full credit inside 12h, tapering to ~0.55x at 60h.
        lead_factor = 1.0 if fc.lead_hours <= 12 else max(0.55, 1.0 - (fc.lead_hours - 12) / 110.0)

        # Disagreement penalty: a 6F ensemble spread means the models are lost.
        spread_factor = 1.0 if fc.spread <= 2.0 else max(0.55, 1.0 - (fc.spread - 2.0) / 10.0)

        # Already-observed floor greatly sharpens a same-day high.
        obs_bonus = 1.10 if fc.observed_extreme_so_far is not None and fc.lead_hours <= 14 else 1.0

        return max(0.0, min(1.0, base * lead_factor * spread_factor * obs_bonus))

    # ------------------------------------------------------------------
    def estimate(self, market: Market) -> Estimate:
        cfg = self._series_for(market)
        if not cfg:
            return Estimate.skip("market is not in weather.series", self.name)

        tz_name = cfg.get("timezone", "America/New_York")
        target = self.parse_event_date(market, tz_name)
        if not target:
            return Estimate.skip(f"could not parse date from {market.event_ticker!r}", self.name)

        lo, hi, label = self.parse_strike(market)
        if lo is None and hi is None:
            # A conflict carries its own explanation and is worth surfacing
            # verbatim - it means Kalshi's convention changed under us.
            reason = (label if label.startswith("CONFLICT")
                      else f"could not parse strike from {market.subtitle!r}")
            return Estimate.skip(reason, self.name)

        fc = self.data.daily_extreme_forecast(
            latitude=cfg["latitude"], longitude=cfg["longitude"], tz_name=tz_name,
            target_date=target, kind=cfg.get("kind", "high"), station=cfg.get("station"),
        )
        if fc is None:
            return Estimate.skip("no forecast data available", self.name)

        ens_prob = min(0.995, max(0.005, self._probability_in_range(fc, lo, hi)))
        prob, conf, model = ens_prob, self._confidence(fc, ens_prob), "ensemble"
        nc, hm = self._nowcast(cfg, fc, target, tz_name)
        if nc is not None:
            prob = min(0.995, max(0.005, nc.probability_between(lo, hi)))
            conf = self._nowcast_confidence(nc)
            model = "nowcast"

        now = dt.datetime.now(dt.timezone.utc)

        def _age(t):
            return round((now - t).total_seconds()) if t is not None else None

        def _iso(t):
            return t.isoformat() if t is not None else None

        cross_diff = (round(fc.mean - fc.cross_check_f, 2)
                      if fc.cross_check_f is not None else None)
        return Estimate(
            probability_yes=prob,
            confidence=conf,
            source=f"{self.name}:{fc.source}",
            evidence={
                "city": cfg.get("label", cfg.get("ticker_prefix")),
                "station": cfg.get("station"),
                "target_date": target.isoformat(),
                "strike": label,
                "ensemble_members": fc.n_members,
                "source": fc.source,
                # The nowcast does not use the ensemble; a degraded ensemble
                # only matters when the ensemble is what priced it.
                "degraded_source": fc.degraded if model == "ensemble" else False,
                "ensemble_mean_f": round(fc.mean, 2),
                "ensemble_spread_f": round(fc.spread, 2),
                "sigma_used_f": round(self._sigma(fc), 2),
                "observed_so_far_f": fc.observed_extreme_so_far,
                "lead_hours": round(fc.lead_hours, 1),
                # ---- provenance (package 14A): what the bot knew, and when.
                # Ids point at journal source_observations rows. Ages are at
                # the moment of THIS estimate, so a cached forecast shows its
                # real age rather than looking freshly fetched.
                "provenance_ids": (list(fc.provenance)
                                   + ([fc.observation_provenance_id]
                                      if fc.observation_provenance_id is not None else [])
                                   + ([hm.provenance_id]
                                      if nc is not None and hm.provenance_id is not None else [])),
                "data_retrieved_at": _iso(fc.retrieved_at),
                "fetch_age_seconds": _age(fc.retrieved_at),
                "forecast_published_at": _iso(fc.data_time),
                "forecast_age_seconds": _age(fc.data_time),
                "forecast_vintage": fc.vintage or None,
                "observation_time": _iso(fc.observation_time),
                "observation_age_seconds": _age(fc.observation_time),
                "observation_suspect": fc.observation_suspect or None,
                "cross_check_source": fc.cross_check_source or None,
                "cross_check_f": (round(fc.cross_check_f, 2)
                                  if fc.cross_check_f is not None else None),
                "cross_check_diff_f": cross_diff,
                # ---- which model priced this, and what the other one said.
                # Both are logged on every contract so the nowcast can be
                # scored against the ensemble it replaced, like for like.
                "model": model,
                "model_version": f"{self.name}/{model}@{MODEL_VERSIONS[model]}",
                "source_tier": MODEL_SOURCE_TIER[model],
                "alt_model": "ensemble" if model == "nowcast" else None,
                "alt_probability": round(ens_prob, 4) if model == "nowcast" else None,
                "nowcast": ({
                    "observed_f": nc.observed_extreme, "bound_f": nc.bound,
                    "remaining_mean_f": (round(nc.mean, 2) if nc.mean is not None else None),
                    "sigma_f": round(nc.sigma, 2), "bias_f": round(nc.bias_f, 2),
                    "bias_samples": nc.bias_samples,
                    "hours_to_peak": round(nc.hours_to_peak, 2),
                    "remaining_hours": nc.remaining_hours, "peak_time": nc.peak_time,
                    "hrrr_run": hm.run, "hrrr_released": hm.released,
                    "hrrr_age_seconds": _age(_to_utc(hm.released)),
                    "notes": nc.notes,
                } if nc is not None else None),
            },
        )

    # ------------------------------------------------------------------
    def _nowcast(self, cfg: dict, fc: DailyTempForecast, target: dt.date,
                 tz_name: str):
        """(Nowcast, HourlyModel) for a same-day market close to its end, or
        (None, None) - in which case the ensemble prices it, as before."""
        nc_cfg = self.nowcast_cfg
        if not nc_cfg.enabled or fc.lead_hours > nc_cfg.max_lead_hours:
            return None, None
        now_local = dt.datetime.now(ZoneInfo(tz_name))
        if now_local.date() != target:
            return None, None
        fetch = getattr(self.data, "hourly_model", None)
        if not callable(fetch):
            return None, None            # e.g. the offline simulator
        hm = fetch(cfg["latitude"], cfg["longitude"], tz_name)
        if hm is None or not hm.hours:
            return None, None
        nc = _nowcast.compute(now_local, target, fc.kind, fc.observations,
                              hm.hours, nc_cfg)
        return (nc, hm) if nc is not None else (None, None)

    @staticmethod
    def _nowcast_confidence(nc) -> float:
        """Trust the nowcast more once it has been checked against today's
        readings and the peak is close; never as much as certainty."""
        base = 0.85 if nc.bias_samples >= 2 else 0.70
        if nc.mean is not None and nc.hours_to_peak > 6:
            base *= 0.85
        return base
