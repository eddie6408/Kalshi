#!/usr/bin/env python3
"""WX-2 history for calibration and climatology. Resumable; quota-guarded.

    .venv/bin/python scripts/wx_backfill.py                  # all of it
    .venv/bin/python scripts/wx_backfill.py --days 365 --only previous_runs

  climatology    NOAA ACIS: 30 years of official daily max/min/avg per station
  twc            weather.com/kalshi daily history (starts ~2026-06-01): the
                 hourly-mean truth the weekly markets settle on
  hourly_obs     TWC hourly observations, the last few weeks
  previous_runs  Open-Meteo previous-runs: each model as issued 1-7 days
                 ahead, in 90-day chunks per station

Open-Meteo counts a long, many-variable request as several calls; the guard
budgets that (pit.om_weight) and the script stops cleanly at the daily
budget - run it again tomorrow and it continues where it stopped.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from kalshibot.wx.pit import PointInTime, TWC_PRIMARY, parse_twc_day  # noqa: E402
from kalshibot.wx.quota import QuotaGuard  # noqa: E402
from kalshibot.wx.runner import station_records  # noqa: E402
from kalshibot.wx.store import WxStore, utcnow  # noqa: E402

BACKFILL_MODELS = ("ncep_nbm_conus", "gfs_global", "ecmwf_ifs025", "ecmwf_aifs025_single",
                   "gem_seamless", "icon_seamless")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180, help="previous-runs history length")
    ap.add_argument("--chunk", type=int, default=90)
    ap.add_argument("--only", choices=["climatology", "twc", "hourly_obs", "previous_runs"])
    ap.add_argument("--open-meteo-budget", type=int, default=None,
                    help="override today's Open-Meteo budget for this run")
    args = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    wx = cfg.get("wx_research") or {}
    store = WxStore(ROOT / wx.get("db_path", "data/weather_research.db"))
    limits = dict(wx.get("quota_per_day") or {})
    if args.open_meteo_budget:
        limits["open_meteo"] = args.open_meteo_budget
    quota = QuotaGuard(store, limits)
    stations = station_records(cfg.get("weather") or {})
    pit = PointInTime(store, quota, stations, "kalshibot/1.0 (research backfill)")
    today = datetime.now(timezone.utc).date()
    do = lambda k: args.only in (None, k)  # noqa: E731

    if do("climatology"):
        n = 0
        for icao in sorted(stations):
            r = pit.backfill_climatology(icao)
            if r == -1:
                print("  ACIS budget reached")
                break
            n += max(0, r)
        print(f"climatology: {n} station-days")

    if do("twc"):
        d, n = date(2026, 6, 1), 0
        have = {r["obs_date"] for r in store.query(
            "SELECT DISTINCT obs_date FROM wx_observations WHERE source='twc_kalshi' "
            "AND status IN ('FINAL','REVISED')")}
        while d < today - timedelta(days=1):
            if d.isoformat() not in have:
                if not quota.allow("twc_climate"):
                    print("  TWC budget reached")
                    break
                j = pit._get("twc_settlement", TWC_PRIMARY, {"date": d.isoformat()})
                if j:
                    n += store.executemany(
                        "INSERT OR IGNORE INTO wx_observations (source, station, obs_date, variable, "
                        "value, status, quality, hours, retrieved_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        parse_twc_day(j, utcnow(), today))
            d += timedelta(days=1)
        print(f"twc history: {n} rows")

    if do("hourly_obs"):
        n = 0
        for k in range(0, 5):
            ws = today - timedelta(days=today.weekday() + 7 * k)
            n += pit.record_hourly(ws)
        print(f"twc hourly: {n} rows")

    if do("previous_runs"):
        start_all = today - timedelta(days=args.days)
        end_all = today - timedelta(days=1)
        done = n = 0
        stop = False
        for icao in sorted(stations):
            s = start_all
            while s <= end_all and not stop:
                e = min(end_all, s + timedelta(days=args.chunk - 1))
                have = store.one(
                    "SELECT COUNT(*) c FROM wx_forecasts WHERE source='open_meteo_previous_runs' "
                    "AND station=? AND valid_date BETWEEN ? AND ?", (icao, s.isoformat(), e.isoformat()))
                expected = (e - s).days * len(BACKFILL_MODELS) * 7 * 3 * 0.8
                if have and have["c"] >= expected:
                    s = e + timedelta(days=1)
                    continue
                r = pit.backfill_previous_runs(icao, s, e, models=BACKFILL_MODELS)
                if r == -1:
                    print(f"  Open-Meteo budget reached at {icao} {s} - run again tomorrow")
                    stop = True
                    break
                n += r
                done += 1
                s = e + timedelta(days=1)
            if stop:
                break
        print(f"previous runs: {done} requests, {n} rows")
    print("quota today:", quota.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
