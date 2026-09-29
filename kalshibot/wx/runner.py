"""The research worker: WX-1 .. WX-7 on their own clocks, in one supervised
worker beside the trading workers.

Every task is isolated: one failing (a source down, a quota reached) logs and
the rest still run. Nothing here holds a trading client or can place an
order; the only thing it can change about trading is to make the daily
estimator SKIP a series whose rules no longer match (registry.blocked_series).
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .calibration import Calibration
from .eligibility import evaluate
from .extended import ExtendedEngine
from .hourly import HourlyIndexModel, load_indexes
from .paper import PaperBook, PaperConfig, daily_shadow, quotes_of
from .pit import PointInTime
from .quota import QuotaGuard, monitor_usage
from .registry import WeatherRegistry, with_backoff
from .store import WxStore

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]

DEFAULT_EVERY = {           # seconds
    "registry": 6 * 3600, "observations": 3600, "hourly_obs": 3 * 3600,
    "forecasts": 6 * 3600, "climatology": 24 * 3600, "extended": 1800, "hourly": 600,
    "daily_shadow": 1800, "resolve": 3600, "eligibility": 24 * 3600, "prune": 24 * 3600,
}
EXTENDED_CLASSES = ("RUN_LENGTH", "WEEKLY_MAX", "WEEKLY_MIN", "MONTHLY_MAX", "MONTHLY_MIN", "SEASONAL")


def _path(p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else ROOT / q


def station_records(weather_cfg: dict) -> dict[str, dict]:
    path = _path((weather_cfg or {}).get("stations_file") or "config/weather_stations.json")
    try:
        reg = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {s["icao"].upper(): s for s in reg.get("stations") or [] if "latitude" in s}


class WxResearch:
    def __init__(self, cfg: dict, weather_cfg: dict, reader, live_series: dict,
                 user_agent: str = "", journal_path: str | None = None, monitor=None):
        cfg = cfg or {}
        self.cfg = cfg
        self.every = {**DEFAULT_EVERY, **(cfg.get("every_seconds") or {})}
        self.store = WxStore(_path(cfg.get("db_path", "data/weather_research.db")))
        from .. import sources as _sources
        self.monitor = monitor or _sources.MONITOR
        self.quota = QuotaGuard(self.store, cfg.get("quota_per_day"),
                                shared_usage=monitor_usage(self.monitor))
        self.stations = station_records(weather_cfg)
        self.registry = WeatherRegistry(self.store, reader, self.quota, live_series)
        self.reader = reader
        self.pit = PointInTime(self.store, self.quota, self.stations, user_agent, self.monitor)
        self.calib_path = _path(cfg.get("calibration_path", "data/wx_calibration.json"))
        self.calib = Calibration.load(self.calib_path, self.store)
        self._calib_mtime = self._mtime()
        self.extended = ExtendedEngine(self.store, self.pit, self.calib, self.stations,
                                       int(cfg.get("n_paths", 20000)))
        self.hourly = HourlyIndexModel(
            self.store, self.quota,
            load_indexes(_path(cfg.get("index_members", "config/kalshi_weather_index.json"))),
            user_agent, self.monitor)
        self.paper = PaperBook(self.store, PaperConfig(cfg.get("paper")))
        self.reviews_path = _path(cfg.get("reviews_path", "data/control/wx_reviews.json"))
        self.journal_path = journal_path
        self._last: dict[str, float] = {}
        self.last_results: dict[str, dict] = {}
        self._blocked: set = set()
        self._blocked_at = 0.0

    # ------------------------------------------------------------ helpers
    def _mtime(self) -> float:
        try:
            return self.calib_path.stat().st_mtime
        except OSError:
            return 0.0

    def _due(self, task: str) -> bool:
        return time.time() - self._last.get(task, 0.0) >= float(self.every.get(task, 3600))

    def _run(self, task: str, fn) -> None:
        if not self._due(task):
            return
        self._last[task] = time.time()
        t0 = time.time()
        try:
            out = fn()
            self.last_results[task] = {"ok": True, "at": datetime.now(timezone.utc).isoformat(),
                                       "seconds": round(time.time() - t0, 1), "result": out}
        except Exception as exc:  # noqa: BLE001
            log.error("WX %s failed: %s", task.upper(), exc, exc_info=True)
            self.last_results[task] = {"ok": False, "at": datetime.now(timezone.utc).isoformat(),
                                       "error": str(exc)[:300]}

    def blocked_series(self) -> set[str]:
        """For the daily estimator: series the registry DISABLED. Cached 60s."""
        if time.time() - self._blocked_at > 60:
            try:
                self._blocked = self.registry.blocked_series()
            except Exception:  # noqa: BLE001
                pass
            self._blocked_at = time.time()
        return self._blocked

    def _quotes(self, tickers: list[str]) -> dict:
        out = {}
        for i in range(0, len(tickers), 100):
            if not self.quota.allow("kalshi_public"):
                break
            part = tickers[i:i + 100]
            out.update(quotes_of(with_backoff(lambda: self.reader.markets_by_ticker(part))))
        return out

    # -------------------------------------------------------------- tasks
    def _extended(self):
        if self._mtime() != self._calib_mtime:
            self.calib = Calibration.load(self.calib_path, self.store)
            self.extended.calib = self.calib
            self._calib_mtime = self._mtime()
        cs = [c for c in self.registry.open_contracts(EXTENDED_CLASSES)
              if c["parse_ok"] and c["status"] != "DISABLED" and c["station"] in self.stations]
        if not cs:
            return {"priced": 0}
        quotes = self._quotes([c["ticker"] for c in cs])
        ids = self.extended.run(cs, quotes)
        runs = self.store.query(f"SELECT * FROM wx_model_runs WHERE id IN ({','.join('?' * len(ids))})",
                                ids) if ids else []
        paper = self.paper.record(runs, {c["ticker"]: c for c in cs})
        return {"contracts": len(cs), "priced": len(ids), "paper_rows": paper}

    def _hourly(self):
        now = datetime.now(timezone.utc)
        cs = [c for c in self.registry.open_contracts(("HOURLY_TEMP",))
              if c["parse_ok"] and c["status"] != "DISABLED"
              and c["close_time"] and c["close_time"] > now.isoformat()]
        if not cs:
            return {"priced": 0}
        quotes = self._quotes([c["ticker"] for c in cs])
        ids = self.hourly.run(cs, quotes)
        runs = self.store.query(f"SELECT * FROM wx_model_runs WHERE id IN ({','.join('?' * len(ids))})",
                                ids) if ids else []
        paper = self.paper.record(runs, {c["ticker"]: c for c in cs})
        return {"contracts": len(cs), "priced": len(ids), "paper_rows": paper}

    def _daily_shadow(self):
        cs = self.registry.open_contracts(("DAILY_HIGH", "DAILY_LOW"), ("PAPER_TRADING",))
        return {"logged": daily_shadow(self.store, self.pit, self.calib, cs, self.stations,
                                       self.journal_path)}

    def _resolve(self):
        now = datetime.now(timezone.utc).isoformat()
        rows = self.store.query(
            "SELECT DISTINCT c.ticker FROM wx_contracts c WHERE c.result IS NULL AND c.close_time < ? "
            "AND (EXISTS (SELECT 1 FROM wx_model_runs r WHERE r.ticker=c.ticker) "
            "OR EXISTS (SELECT 1 FROM wx_daily_shadow s WHERE s.ticker=c.ticker)) LIMIT 2000", (now,))
        tick = [r["ticker"] for r in rows]
        q = self._quotes(tick)
        results = {t: v["result"] for t, v in q.items() if v.get("result") in ("yes", "no")}
        return {"checked": len(tick), "settled": self.paper.resolve(results)}

    def _climatology(self):
        n = 0
        for icao in sorted(self.stations):
            r = self.pit.backfill_climatology(icao)
            if r == -1:
                break
            n += max(0, r)
        return {"rows": n}

    def run_pass(self) -> dict:
        self._run("registry", self.registry.refresh)
        self._run("observations", lambda: {"rows": self.pit.record_observations()})
        self._run("hourly_obs", lambda: {"rows": self.pit.record_hourly()})
        self._run("forecasts", lambda: {"rows": self.pit.snapshot_forecasts()})
        self._run("climatology", self._climatology)
        self._run("extended", self._extended)
        self._run("hourly", self._hourly)
        self._run("daily_shadow", self._daily_shadow)
        self._run("resolve", self._resolve)
        self._run("eligibility", lambda: {k: v["status"] for k, v in
                                          evaluate(self.store, self.reviews_path,
                                                   self.cfg.get("eligibility")).items()
                                          if isinstance(v, dict)})
        self._run("prune", lambda: {"deleted": self.pit.prune()})
        return self.summary()

    def summary(self) -> dict:
        return {"tasks": self.last_results, "quota": self.quota.summary(),
                "calibration": self.calib.version if self.calib.fitted else "unfitted",
                "registry": self.registry.last_summary}
