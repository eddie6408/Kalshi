#!/usr/bin/env python3
"""Fit the weather multi-model blend against what Kalshi settles on.

    .venv/bin/python scripts/fit_weather_blend.py             # fit, report, write
    .venv/bin/python scripts/fit_weather_blend.py --days 60 --dry-run

Truth: The Weather Company's official daily max/min (weather.com/kalshi
climate portal - the source Kalshi's weather rules name), for every station
in config/weather_stations.json.
Forecasts: Open-Meteo's historical-forecast archive for NBM, GFS, ECMWF IFS,
ECMWF AIFS, HRRR, GEM and ICON at each station (short lead - the window the
strategy trades in).

Reports each model's error, an equal average, and the fitted blend on the
most recent `--test-days` days after fitting on the days before them; then
fits on everything and writes data/weather_blend.json.

Note: Open-Meteo aggregates the local calendar day; the climate report uses
local STANDARD time (1am-1am under daylight saving). The per-station bias
absorbs the systematic part of that difference.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kalshibot import weather_blend as wb  # noqa: E402

TWC = "https://weather.com/kalshi/api/climate/primary?date={date}"
HIST = "https://historical-forecast-api.open-meteo.com/v1/forecast"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--test-days", type=int, default=25)
    ap.add_argument("--stations", default=str(ROOT / "config" / "weather_stations.json"))
    ap.add_argument("--out", default=str(ROOT / "data" / "weather_blend.json"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    s = requests.Session()
    s.headers.update({"User-Agent": "kalshibot/1.0 (personal research; blend fit)"})
    reg = json.loads(Path(args.stations).read_text())
    stations = {x["icao"]: x for x in reg["stations"] if "latitude" in x}
    end = datetime.now(timezone.utc).date() - timedelta(days=2)
    start = end - timedelta(days=args.days - 1)

    truth: dict[tuple, float] = {}
    d = start
    while d <= end:
        try:
            j = s.get(TWC.format(date=d.isoformat()), timeout=30).json()
            for r in j.get("results") or []:
                icao = (r.get("station") or {}).get("icao", "").upper()
                data = r.get("data") or {}
                if icao in stations and r.get("status") in ("official", "revised"):
                    for kind, key in (("high", "maxTemp"), ("low", "minTemp")):
                        if isinstance(data.get(key), (int, float)):
                            truth[(icao, d.isoformat(), kind)] = float(data[key])
        except Exception as exc:  # noqa: BLE001
            print(f"  ! TWC {d}: {exc}", file=sys.stderr)
        d += timedelta(days=1)
        time.sleep(0.1)
    print(f"truth: {len(truth)} station-day values from The Weather Company, {start} .. {end}")

    rows = []
    for icao, st in stations.items():
        params = {"latitude": st["latitude"], "longitude": st["longitude"],
                  "daily": "temperature_2m_max,temperature_2m_min",
                  "temperature_unit": "fahrenheit", "timezone": st["timezone"],
                  "start_date": start.isoformat(), "end_date": end.isoformat(),
                  "models": ",".join(wb.MODELS)}
        try:
            j = s.get(HIST, params=params, timeout=60).json()
        except Exception as exc:  # noqa: BLE001
            print(f"  ! forecasts {icao}: {exc}", file=sys.stderr)
            continue
        daily = j.get("daily") or {}
        days = daily.get("time") or []
        for kind, var in (("high", "temperature_2m_max"), ("low", "temperature_2m_min")):
            for i, day in enumerate(days):
                t = truth.get((icao, day, kind))
                if t is None:
                    continue
                vals = {m: (daily.get(f"{var}_{m}") or [None] * len(days))[i] for m in wb.MODELS}
                if any(v is not None for v in vals.values()):
                    rows.append({"station": icao, "date": day, "kind": kind, "truth": t,
                                 "models": vals})
        time.sleep(0.3)
    split = (end - timedelta(days=args.test_days - 1)).isoformat()
    params, report = wb.fit(rows, split=split)
    print(f"rows: {report['rows']} across {report['stations']} stations; "
          f"test on {split} .. {end}\n")
    oos = report.get("out_of_sample") or {}
    for kind in ("high", "low"):
        k = oos.get(kind) or {}
        if not k:
            continue
        print(f"{kind.upper():5} (out of sample, n={k['n']})   RMSE F   MAE F")
        for m in wb.MODELS:
            if m in k:
                print(f"  {wb.LABELS[m]:<22}{k[m]['rmse_raw']:>7.2f}{k[m]['mae_raw']:>8.2f}")
        ea = k.get("equal_average") or {}
        bl = k.get("blend") or {}
        print(f"  {'equal average':<22}{ea.get('rmse') or 0:>7.2f}{ea.get('mae') or 0:>8.2f}")
        print(f"  {'FITTED BLEND':<22}{bl.get('rmse') or 0:>7.2f}{bl.get('mae') or 0:>8.2f}"
              f"   within 1F: {100 * (bl.get('within_1f') or 0):.0f}%")
        w = params["weights"].get(kind) or {}
        print("  weights: " + ", ".join(f"{wb.LABELS[m]} {v:.2f}" for m, v in
                                        sorted(w.items(), key=lambda x: -x[1])))
        print()
    if args.dry_run:
        return 0
    out = {"fitted_at": datetime.now(timezone.utc).isoformat(), "window": [start.isoformat(),
           end.isoformat()], "version": wb.BLEND_VERSION, "params": params, "report": report}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=1, default=str))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
