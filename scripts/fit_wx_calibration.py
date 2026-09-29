#!/usr/bin/env python3
"""WX-3: fit source calibration on the point-in-time history and report it.

    .venv/bin/python scripts/fit_wx_calibration.py            # fit, report, write
    .venv/bin/python scripts/fit_wx_calibration.py --dry-run

Needs scripts/wx_backfill.py first. Writes data/wx_calibration.json and the
wx_calibration table; the research worker reloads it on its next pass.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from kalshibot.wx import calibration as cal  # noqa: E402
from kalshibot.wx.store import WxStore  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-frac", type=float, default=0.25)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    wx = cfg.get("wx_research") or {}
    store = WxStore(ROOT / wx.get("db_path", "data/weather_research.db"))
    params, report = cal.fit(store, test_frac=args.test_frac)
    print(f"rows {report['rows']}  stations {report['stations']}  dates {report['dates']}  "
          f"test from {report['split']}\n")
    print(f"{'variable':18}{'lead':>5}{'n':>7}{'blend':>8}{'equal':>8}{'best 1':>8}"
          f"  {'cover80':>7}{'cover90':>8}  best single")
    for key, o in sorted((report.get("out_of_sample") or {}).items(),
                         key=lambda kv: (kv[0].split('|')[0], int(kv[0].split('|')[1]))):
        var, lead = key.split("|")
        print(f"{var:18}{lead:>5}{o['n']:>7}{o['rmse_blend']:>8.2f}{(o['rmse_equal'] or 0):>8.2f}"
              f"{(o['rmse_best_single'] or 0):>8.2f}  {o['cover80']:>7.0%}{o['cover90']:>8.0%}  "
              f"{o['best_single']}")
    print("\ntemporal dependence (lag-1 corr of standardized errors):",
          {k: round(v["rho"], 2) for k, v in params["rho"].items()})
    mo = params["mean_offset"]
    print(f"hourly mean - (max+min)/2: global {mo.get('_global', 0):+.2f}F over "
          f"{len(mo) - 1} stations")
    if args.dry_run:
        return 0
    path = ROOT / wx.get("calibration_path", "data/wx_calibration.json")
    cal.write_calibration(store, params, report, path)
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
