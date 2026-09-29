#!/usr/bin/env python3
"""WX-6: class-separated validation.

    .venv/bin/python scripts/wx_validate.py            # historical + live report

1. HISTORICAL, run-length class (the KXAVGT rule), out of sample:
   calibration is fitted ONLY on dates before --split; for every Kalshi
   weekly-average station and every Mon-Sun week after it, the engine is run
   as it would have been on the Sunday before the week, using the forecasts
   as issued then (previous-runs) - and scored against TWC's hourly means.
   Thresholds: each station's own 60th and 85th percentile of the hourly mean
   for the season (a fixed 90F would be trivially NO most of the autumn),
   k = 2..5. Baseline: the same simulation from climatology alone.
   Prediction metrics only - there are no historical prices for these.

2. LIVE, every class: prediction metrics (last run before close vs Kalshi's
   result, and vs the market mid) and paper trading metrics, separately.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import yaml  # noqa: E402

from kalshibot.wx import calibration as cal  # noqa: E402
from kalshibot.wx.extended import DayInput, longest_run, round_half_up, simulate  # noqa: E402
from kalshibot.wx.paper import class_report  # noqa: E402
from kalshibot.wx.store import WxStore  # noqa: E402

RUN_STATIONS = ("KSAT", "KSAN", "KPHX", "KOKC", "KMSY", "KLAS", "KIAH", "KDFW", "KDEN", "KAUS")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default=None, help="first out-of-sample date (default: 60%% point)")
    ap.add_argument("--paths", type=int, default=4000)
    args = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    wx = cfg.get("wx_research") or {}
    store = WxStore(ROOT / wx.get("db_path", "data/weather_research.db"))
    truth = cal.load_truth(store, ("temp_mean_hourly",))
    days_avail = sorted({d for (_, d, v) in truth if v == "temp_mean_hourly"})
    if len(days_avail) < 40:
        print("not enough TWC hourly-mean history yet (run scripts/wx_backfill.py)")
    else:
        split = args.split or days_avail[int(len(days_avail) * 0.6)]
        params, _ = cal.fit(store, as_of=split)
        c = cal.Calibration(params, store)
        fc = {}
        for r in store.query("SELECT model, station, valid_date, lead_days, value FROM wx_forecasts "
                             "WHERE source='open_meteo_previous_runs' AND variable='temp_mean_hourly' "
                             "AND valid_date >= ?", (split,)):
            fc.setdefault((r["station"], r["valid_date"], r["lead_days"]), {})[r["model"]] = r["value"]
        rows = []
        monday = date.fromisoformat(split)
        monday += timedelta(days=(7 - monday.weekday()) % 7)
        while monday + timedelta(days=6) <= date.fromisoformat(days_avail[-1]):
            week = [monday + timedelta(days=i) for i in range(7)]
            for st in RUN_STATIONS:
                obs = [truth.get((st, d.isoformat(), "temp_mean_hourly")) for d in week]
                if any(o is None for o in obs):
                    continue
                hist = [v for (s, d, var), v in truth.items() if s == st and var == "temp_mean_hourly"
                        and d < split]
                if len(hist) < 30:
                    continue
                fdays, cdays = [], []
                for i, d in enumerate(week):
                    lead = i + 1                          # issued the Sunday before
                    vals = fc.get((st, d.isoformat(), lead)) or {}
                    ctr = c.centre("temp_mean_hourly", lead, st, vals) if vals else None
                    clim = c.climatology(st, "temp_mean_hourly", d)
                    if ctr is None or not clim:
                        break
                    sd = min(c.sd("temp_mean_hourly", lead, st, d) or 3.0, clim[1])
                    fdays.append(DayInput(d.isoformat(), "forecast", ctr, sd, lead=lead))
                    cdays.append(DayInput(d.isoformat(), "climatology", clim[0], clim[1]))
                if len(fdays) < 7:
                    continue
                obs_r = round_half_up(np.array(obs))
                for q in (60, 85):
                    thr = float(np.percentile(hist, q))
                    thr = round(thr)
                    for k in (2, 3, 4, 5):
                        def stat(v, _d, thr=thr):
                            return longest_run(round_half_up(v) > thr)
                        y = int(longest_run((obs_r > thr)[None, :])[0] >= k)
                        pm = simulate(fdays, stat, args.paths, 7, c.rho("temp_mean_hourly"),
                                      event=lambda s, k=k: s >= k).prob
                        pc = simulate(cdays, stat, args.paths, 7, 0.75, 0.75,
                                      event=lambda s, k=k: s >= k).prob
                        rows.append((st, monday.isoformat(), thr, k, pm, pc, y))
            monday += timedelta(days=7)
        if rows:
            bm = mean((r[4] - r[6]) ** 2 for r in rows)
            bc = mean((r[5] - r[6]) ** 2 for r in rows)
            print(f"RUN_LENGTH historical out-of-sample (split {split}): {len(rows)} contracts, "
                  f"{len({(r[0], r[1]) for r in rows})} station-weeks, YES rate "
                  f"{mean(r[6] for r in rows):.0%}")
            print(f"  Brier  model {bm:.4f}   climatology {bc:.4f}   skill {1 - bm / bc:+.1%}")
            for k in (2, 3, 4, 5):
                rk = [r for r in rows if r[3] == k]
                if rk:
                    print(f"  k={k}: n={len(rk):4d}  model {mean((r[4]-r[6])**2 for r in rk):.4f}  "
                          f"clim {mean((r[5]-r[6])**2 for r in rk):.4f}")
        else:
            print("RUN_LENGTH historical: no complete out-of-sample weeks yet")
    print("\nLIVE class report (prediction and trading kept separate):")
    print(json.dumps(class_report(store), indent=1, default=lambda x: round(x, 4)
                     if isinstance(x, float) else str(x)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
