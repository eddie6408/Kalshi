"""The inflation estimator: Kalshi CPI / core CPI / PCE markets, priced from
the Cleveland Fed nowcast and an internal weighted nowcast.

One question per market, like every estimator: "what is the probability this
contract resolves YES, and how much do I trust that number?" Everything
about money, sizing, fees, gates, risk and execution is the shared engine's,
unchanged - which is what makes this the second strategy of one account
rather than a second bot quietly spending the first one's limits.

    market      KXCPI-26DEC-T0.3   "Will CPI rise more than 0.3% in December 2026?"
    target      2026-12, measure CPI, month over month
    strike      0.3, type "greater": YES iff the published figure > 0.3
    model       inflation_model.forecast() -> mu, measured error distribution
    P(YES)      inflation_model.prob_yes()

Declines (and says why) when: the series is not one it knows; the strike
type is not "greater"; it cannot tell which month the contract is about; the
month is already released; or the nowcast archive cannot be read. A month
the Cleveland Fed is not nowcasting yet is priced by the trend alone, at
lower confidence; the lead-time gate keeps it out of trading.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path

from .. import inflation_model as im
from ..alerts import ALERTS, today_key
from ..control import write_status
from ..models import Market
from ..sources import MONITOR
from .base import Estimate

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"])}
ABBR = {m[:3].upper(): i for m, i in MONTHS.items()}
SOURCE_TIER = 1          # the Federal Reserve Bank of Cleveland publishes it
#: FRED index series for the cross-check of the latest released month
FRED_INDEX = {"CPI": "CPIAUCSL", "CORE_CPI": "CPILFESL", "PCE": "PCEPI",
              "CORE_PCE": "PCEPILFE"}


def target_month(market: Market) -> str | None:
    """Which month's figure the contract is about.

    The title says it ("... in December 2026", "... for August 2026"); the
    event ticker says it too (KXCPI-26DEC). A ticker with a day in it
    (KXPCEHEAD-26SEP30) is a RELEASE date - the data month is the one before.
    """
    title = f"{market.title or ''} {getattr(market, 'subtitle', '') or ''}".lower()
    m = re.search(r"(january|february|march|april|may|june|july|august|september|"
                  r"october|november|december)\s+(\d{4})", title)
    if m:
        return f"{int(m.group(2)):04d}-{MONTHS[m.group(1)]:02d}"
    ev = (market.event_ticker or market.ticker or "").split("-")
    if len(ev) >= 2:
        tag = ev[1]
        mm = re.fullmatch(r"(\d{2})([A-Z]{3})(\d{2})?", tag)
        if mm and mm.group(2) in ABBR:
            key = f"{2000 + int(mm.group(1)):04d}-{ABBR[mm.group(2)]:02d}"
            return im.month_add(key, -1) if mm.group(3) else key
    return None


class InflationNowcastEstimator:
    name = "inflation_nowcast"

    def __init__(self, cfg: dict | None, data, clock=time.time):
        self.cfg = cfg or {}
        self.data = data
        self.clock = clock
        self.series = {s: im.SERIES[s] for s in (self.cfg.get("series") or list(im.SERIES))
                       if s in im.SERIES}
        self.regime_threshold = float(self.cfg.get("regime_threshold", 0.10))
        self.stale_days = float(self.cfg.get("stale_nowcast_days", 4))
        self.params_path = self._path(self.cfg.get("params_path", "data/inflation_model.json"))
        self.status_path = self._path(self.cfg.get("status_path", "data/control/inflation.json"))
        self._params: dict[str, im.Params] = {}
        self._params_at = 0.0
        self._observed: set[tuple] = set()
        self._status: dict = {}
        self._status_at = 0.0
        self._lock = threading.Lock()
        self.cross_check_tolerance = float(self.cfg.get("cross_check_tolerance", 0.05))
        self._cross: dict[str, tuple[float, bool, str]] = {}

    @staticmethod
    def _path(p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else ROOT / path

    def candidate_series(self) -> list[str]:
        return list(self.series)

    # ---- parameters -------------------------------------------------------
    def params(self, measure: str, freq: str) -> im.Params:
        now = self.clock()
        if now - self._params_at > 600:
            try:
                raw = json.loads(self.params_path.read_text(encoding="utf-8"))
                self._params = {k: im.Params.from_dict(v, k.split(":")[1])
                                for k, v in (raw.get("params") or {}).items()}
            except (OSError, ValueError):
                self._params = {}
            self._params_at = now
        return self._params.get(f"{measure}:{freq}") or im.Params.from_dict(None, freq)

    def parse_strike(self, market: Market):
        """For the coherence check: a "greater" ladder is a set of thresholds."""
        raw = market.raw or {}
        if str(raw.get("strike_type") or "").lower() != "greater":
            return None, None, ""
        try:
            k = float(raw.get("floor_strike"))
        except (TypeError, ValueError):
            return None, None, ""
        return k + im.STEP, None, f"> {k:g}%"

    # ---- the estimate -----------------------------------------------------
    def estimate(self, market: Market) -> Estimate:
        prefix = (market.series_ticker or market.ticker.split("-")[0]).upper()
        if prefix not in self.series:
            return Estimate.skip("not an inflation series this estimator prices", self.name)
        measure, freq = self.series[prefix]
        raw = market.raw or {}
        if str(raw.get("strike_type") or "").lower() != "greater":
            return Estimate.skip(f"strike type {raw.get('strike_type')!r} not supported", self.name)
        try:
            strike = float(raw.get("floor_strike"))
        except (TypeError, ValueError):
            return Estimate.skip("no strike on the market", self.name)
        target = target_month(market)
        if target is None:
            return Estimate.skip("cannot tell which month the contract is about", self.name)
        try:
            archive = self.data.cleveland("month" if freq == "mom" else "year")
        except Exception as exc:  # noqa: BLE001
            return Estimate.skip(f"Cleveland Fed nowcast unavailable: {exc}", self.name)

        now = datetime.fromtimestamp(self.clock(), timezone.utc)
        close = market.close_time
        hours = (close - now).total_seconds() / 3600.0 if close else None
        params = self.params(measure, freq)
        fc = im.forecast(archive, measure, freq, target, now.date(), params,
                         days_to_release=(hours / 24.0 if hours is not None else None),
                         regime_threshold=self.regime_threshold)
        if fc is None:
            return Estimate.skip("no nowcast and no history for this month", self.name)
        if fc.source == "released":
            return Estimate.skip(f"{target} already released ({fc.actual:.2f}%)", self.name)

        p = im.prob_yes(strike, fc.mu, fc.sigma, fc.quantiles)
        p = min(0.99, max(0.01, p))
        # the Cleveland-only probability, beside it, so the blend can be scored
        alt = (im.prob_yes(strike, fc.cleveland - params.bias, fc.sigma, fc.quantiles)
               if fc.cleveland is not None else None)

        nowcast_age = ((now.date() - fc.cleveland_date).days
                       if fc.cleveland_date else None)
        confidence = 0.80 if fc.source == "blend" else 0.55
        notes = []
        if fc.source == "blend" and nowcast_age is not None and nowcast_age > self.stale_days:
            confidence *= 0.7
            notes.append(f"nowcast {nowcast_age}d old")
        if fc.regime_shift:
            confidence *= 0.6
            notes.append(f"regime: nowcast moved {fc.regime_delta:+.2f}pt over 5 readings")
            ALERTS.send(today_key("inflation-regime", f"{measure}:{freq}:{target}"),
                        f"Inflation regime shift: {measure} {freq} {target}",
                        f"The Cleveland Fed nowcast moved {fc.regime_delta:+.2f}pt over its "
                        f"last five readings (to {fc.cleveland:.2f}). Confidence is cut "
                        f"until it settles.", priority="default", tags="chart_with_upwards_trend")

        prov = self._provenance(measure, freq, target, fc)
        evidence = {
            "series": prefix, "measure": measure, "freq": freq, "target_date": target,
            "strike": strike, "mu": round(fc.mu, 4), "sigma": round(fc.sigma, 4),
            "distribution": "measured" if fc.quantiles else "normal",
            "model": fc.source, "cleveland": fc.cleveland,
            "cleveland_date": fc.cleveland_date.isoformat() if fc.cleveland_date else None,
            "nowcast_age_days": nowcast_age, "trend": fc.trend,
            "days_to_release": fc.days_to_release, "months_ahead": fc.months_ahead,
            "lead_hours": hours, "regime_shift": fc.regime_shift,
            "regime_delta": fc.regime_delta, "notes": notes,
            "model_version": im.MODEL_VERSION, "source_tier": SOURCE_TIER,
            "params_fitted": params.fitted, "alt_model": "cleveland",
            "alt_probability": round(alt, 4) if alt is not None else None,
            "provenance_ids": [prov] if prov else [],
        }
        ok, why = self._cross_check(measure, freq)
        evidence["cross_check"] = why
        if not ok:
            return Estimate.skip(f"data conflict: {why}", self.name)
        self._note_status(prefix, fc, now)
        return Estimate(p, confidence, self.name, evidence)

    # ---- does the archive agree with the statistical agency? ------------
    def _cross_check(self, measure: str, freq: str) -> tuple[bool, str]:
        """The latest released month-over-month change in the Cleveland
        archive against the one computed from FRED's index (BLS / BEA data).
        A disagreement beyond tolerance means one of them is wrong, and a
        nowcast built on a wrong history is not traded. FRED unreachable is
        recorded, not blocking. Checked every six hours."""
        hit = self._cross.get(measure)
        if hit and self.clock() - hit[0] < 6 * 3600:
            return hit[1], hit[2]
        ok, why = True, "not checked"
        try:
            archive = self.data.cleveland("month")
            last = im.latest_released(archive, measure)
            rows = self.data.fred(FRED_INDEX[measure])
            idx = {d.strftime("%Y-%m"): v for d, v in rows}
            prev = im.month_add(last, -1) if last else None
            if last and last in idx and prev in idx:
                fred = 100.0 * (idx[last] / idx[prev] - 1.0)
                cf = archive[last][measure].actual
                gap = abs(fred - cf)
                ok = gap <= self.cross_check_tolerance
                why = (f"{measure} {last}: Cleveland {cf:.3f}% vs FRED {fred:.3f}% "
                       f"({'agree' if ok else 'DISAGREE'}, gap {gap:.3f})")
            else:
                why = f"FRED has no {measure} for {last}"
        except Exception as exc:  # noqa: BLE001
            why = f"FRED unavailable ({exc})"
        self._cross[measure] = (self.clock(), ok, why)
        if not ok:
            log.warning("INFLATION  %s", why)
        return ok, why

    # ---- records ----------------------------------------------------------
    def _provenance(self, measure, freq, target, fc) -> int | None:
        if fc.cleveland is None:
            return None
        key = (measure, freq, target, fc.cleveland_date)
        if key in self._observed:
            return None
        self._observed.add(key)
        return MONITOR.observe(
            "cleveland_fed_nowcast", data_type=f"{measure.lower()}_{freq}_nowcast",
            subject=measure, target_date=target, value=fc.cleveland,
            data_time=fc.cleveland_date.isoformat() if fc.cleveland_date else None,
            retrieved_at=datetime.now(timezone.utc).isoformat())

    def _note_status(self, prefix: str, fc, now: datetime) -> None:
        """What the dashboard's Inflation page shows. Written at most once a
        minute; never raises."""
        with self._lock:
            self._status.setdefault(prefix, {})[fc.target] = {
                "measure": fc.measure, "freq": fc.freq, "mu": fc.mu, "sigma": fc.sigma,
                "source": fc.source, "cleveland": fc.cleveland,
                "cleveland_date": fc.cleveland_date.isoformat() if fc.cleveland_date else None,
                "trend": fc.trend, "days_to_release": fc.days_to_release,
                "regime_shift": fc.regime_shift, "regime_delta": fc.regime_delta,
                "distribution": "measured" if fc.quantiles else "normal"}
            if self.clock() - self._status_at < 60:
                return
            self._status_at = self.clock()
            payload = {"at": now.isoformat(), "series": self._status,
                       "model_version": im.MODEL_VERSION,
                       "params_file": str(self.params_path),
                       "params_fitted": {k: v.fitted for k, v in self._params.items()}}
        try:
            write_status(self.status_path, payload)
        except Exception as exc:  # noqa: BLE001
            log.debug("could not write the inflation status: %s", exc)
