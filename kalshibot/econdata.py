"""Economic data for the inflation strategy: fetch, parse, cache.

    Cleveland Fed Inflation Nowcasting (primary, tier 1)
        month.json   month-over-month nowcasts, daily, for CPI, core CPI, PCE
                     and core PCE - and the ACTUAL released value of each - for
                     every month since 2013 (one chart per target month)
        year.json    the same, year over year
    FRED (tier 2, redistributing BLS and BEA) - keyless CSV
        CPIAUCSL, CPILFESL, PCEPI, PCEPILFE index levels: a cross-check that
        the last actual the nowcast archive reports is what the statistical
        agency published.
    BLS public API v1 (tier 1) - optional, rate-limited, off by default.

Every request goes through the source monitor (health, latency, data time)
and the parsed values are plain Python, so the model (inflation_model.py) is
pure and can be tested and backtested without a network.
"""
from __future__ import annotations

import csv
import io
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date

import requests

from . import sources as _sources

log = logging.getLogger(__name__)

CLEVELAND_BASE = ("https://www.clevelandfed.org/-/media/files/webcharts/"
                  "inflationnowcasting/nowcast_{kind}.json?sc_lang=en")
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
BLS_V1 = "https://api.bls.gov/publicAPI/v1/timeseries/data/{series}"

#: Cleveland dataset names -> our measure names
MEASURES = {"CPI Inflation": "CPI", "Core CPI Inflation": "CORE_CPI",
            "PCE Inflation": "PCE", "Core PCE Inflation": "CORE_PCE"}
ACTUAL_PREFIX = "Actual "


@dataclass
class MeasureHistory:
    """One measure for one target month: the daily nowcasts, and the actual."""
    nowcasts: list[tuple[date, float]] = field(default_factory=list)
    actual: float | None = None
    released: date | None = None


#: {"2026-09": {"CPI": MeasureHistory, ...}, ...}
Archive = dict


def _month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def parse_cleveland(charts: list[dict]) -> Archive:
    """The Cleveland webchart JSON -> {target month: {measure: history}}.

    Each chart covers one target month ("subcaption": "2026-9"). Its category
    labels are dates ("09/28") plus vertical-line markers ("CPI Aug") that
    carry no data point, so data index i belongs to the i-th NON-marker label.
    Labels run past the target month into the release months; a label month
    earlier than the target month belongs to the next year.
    """
    out: Archive = {}
    for chart in charts or []:
        try:
            y, m = (int(x) for x in str(chart["chart"]["subcaption"]).split("-"))
        except (KeyError, ValueError, TypeError):
            continue
        labels = [c.get("label", "") for c in chart.get("categories", [{}])[0].get("category", [])
                  if not c.get("vline")]
        dates: list[date | None] = []
        for lab in labels:
            try:
                mm, dd = (int(x) for x in lab.split("/"))
                yy = y + (1 if mm < m else 0)
                dates.append(date(yy, mm, dd))
            except (ValueError, TypeError):
                dates.append(None)
        key = _month_key(y, m)
        month = out.setdefault(key, {})
        for ds in chart.get("dataset", []):
            name = ds.get("seriesname", "")
            actual = name.startswith(ACTUAL_PREFIX)
            measure = MEASURES.get(name[len(ACTUAL_PREFIX):] if actual else name)
            if measure is None:
                continue
            h = month.setdefault(measure, MeasureHistory())
            for i, point in enumerate(ds.get("data", [])):
                raw = point.get("value") if isinstance(point, dict) else None
                if raw in (None, "") or i >= len(dates) or dates[i] is None:
                    continue
                try:
                    v = float(raw)
                except ValueError:
                    continue
                if actual:
                    if h.actual is None:
                        h.actual, h.released = v, dates[i]
                else:
                    h.nowcasts.append((dates[i], v))
    return out


def parse_fred_csv(text: str) -> list[tuple[date, float]]:
    rows = []
    for r in csv.reader(io.StringIO(text)):
        if len(r) < 2 or not r[0][:1].isdigit():
            continue
        try:
            rows.append((date.fromisoformat(r[0]), float(r[1])))
        except ValueError:
            continue
    return rows


class EconData:
    """Fetches and caches; the only part of the inflation strategy that talks
    to the network."""

    def __init__(self, user_agent: str = "kalshibot/1.0 (personal research)",
                 cache_seconds: float = 3600.0, fred_cache_seconds: float = 6 * 3600.0,
                 timeout: float = 30.0, session=None, clock=time.time):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "application/json, text/csv"})
        self.cache_seconds = cache_seconds
        self.fred_cache_seconds = fred_cache_seconds
        self.timeout = timeout
        self.clock = clock
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def _cached(self, key: str, ttl: float, fetch):
        now = self.clock()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        try:
            value = fetch()
        except Exception as exc:  # noqa: BLE001
            log.warning("ECON  %s failed: %s", key, exc)
            if hit:                      # serve the last good copy, marked by age
                return hit[1]
            raise
        with self._lock:
            self._cache[key] = (now, value)
        return value

    def fetched_at(self, key: str) -> float | None:
        with self._lock:
            hit = self._cache.get(key)
        return hit[0] if hit else None

    # ---- Cleveland Fed ---------------------------------------------------
    def cleveland(self, kind: str = "month") -> Archive:
        def fetch():
            with _sources.MONITOR.track("cleveland_fed_nowcast") as call:
                r = self.session.get(CLEVELAND_BASE.format(kind=kind), timeout=self.timeout)
                call.status = r.status_code
                r.raise_for_status()
                archive = parse_cleveland(r.json())
                newest = max((d for mh in archive.values() for h in mh.values()
                              for d, _ in h.nowcasts), default=None)
                call.data_time = newest.isoformat() if newest else None
                return archive
        return self._cached(f"cleveland:{kind}", self.cache_seconds, fetch)

    # ---- FRED -------------------------------------------------------------
    def fred(self, series: str) -> list[tuple[date, float]]:
        def fetch():
            with _sources.MONITOR.track("fred") as call:
                r = self.session.get(FRED_CSV.format(series=series), timeout=self.timeout)
                call.status = r.status_code
                r.raise_for_status()
                rows = parse_fred_csv(r.text)
                call.data_time = rows[-1][0].isoformat() if rows else None
                return rows
        return self._cached(f"fred:{series}", self.fred_cache_seconds, fetch)

    # ---- BLS (optional cross-check) --------------------------------------
    def bls_latest(self, series: str) -> tuple[date, float] | None:
        def fetch():
            with _sources.MONITOR.track("bls_api") as call:
                r = self.session.get(BLS_V1.format(series=series), timeout=self.timeout)
                call.status = r.status_code
                r.raise_for_status()
                data = (((r.json().get("Results") or {}).get("series") or [{}])[0]
                        .get("data") or [])
                if not data:
                    return None
                d = data[0]
                month = int(str(d.get("period", "M01"))[1:])
                return date(int(d["year"]), month, 1), float(d["value"])
        return self._cached(f"bls:{series}", 12 * 3600.0, fetch)
