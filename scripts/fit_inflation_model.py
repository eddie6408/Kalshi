#!/usr/bin/env python3
"""Fit the inflation model on the Cleveland Fed's own archive.

    .venv/bin/python scripts/fit_inflation_model.py            # fit, report, write
    .venv/bin/python scripts/fit_inflation_model.py --dry-run  # report only

For every measure the bot trades (CPI, core CPI, PCE, core PCE; month over
month and year over year) it measures, on every archived day since 2016,
how far the nowcast was from the value later released - by days before the
release - and fits the blend weight and the error distribution the
estimator prices with. It reports an out-of-sample check first (fit on
2016-2023, score 2024 onward) so the numbers it writes have been tested on
months they were not fitted on, then fits on everything and writes
data/inflation_model.json.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kalshibot import inflation_model as im  # noqa: E402
from kalshibot.econdata import EconData  # noqa: E402

KEYS = sorted({(m, f) for m, f in im.SERIES.values()})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2016-01")
    ap.add_argument("--split", default="2023-12", help="last month of the training half")
    ap.add_argument("--out", default=str(ROOT / "data" / "inflation_model.json"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    data = EconData(user_agent="kalshibot/1.0 (personal trading research; model fit)")
    archives = {"mom": data.cleveland("month"), "yoy": data.cleveland("year")}
    months = sorted(archives["mom"])
    print(f"Cleveland Fed archive: {len(months)} target months, {months[0]} .. {months[-1]}\n")
    print(f"{'measure':<14}{'rows':>6}{'w<=7d':>7}{'w<=31d':>8}{'sd<=7d':>8}{'sd<=31d':>9}"
          f"{'  out-of-sample Brier':>22}{'  (normal)':>10}")
    params_out, report_out = {}, {}
    for measure, freq in KEYS:
        arch = archives[freq]
        train, _ = im.fit(arch, measure, freq, since=args.since, until=args.split)
        p_train = im.Params.from_dict(train, freq)
        oos = im.calibration(arch, measure, freq, p_train, since=im.month_add(args.split, 1))
        normal = im.calibration(arch, measure, freq, im.Params.from_dict(
            {k: v for k, v in train.items() if k != "quantiles"}, freq),
            since=im.month_add(args.split, 1))
        full, rep = im.fit(arch, measure, freq, since=args.since)
        p = im.Params.from_dict(full, freq)
        key = f"{measure}:{freq}"
        params_out[key] = full
        report_out[key] = {"fit": rep, "out_of_sample": oos,
                           "out_of_sample_normal_brier": normal.get("brier")}
        f = lambda x: f"{x:.4f}" if isinstance(x, float) else "   -  "  # noqa: E731
        print(f"{key:<14}{rep.get('n', 0):>6}{p.w_for(5):>7.2f}{p.w_for(25):>8.2f}"
              f"{p.sigma_for(5):>8.3f}{p.sigma_for(25):>9.3f}{f(oos.get('brier')):>22}"
              f"{f(normal.get('brier')):>10}")
    if args.dry_run:
        print("\n(dry run - nothing written)")
        return 0
    out = {"fitted_at": datetime.now(timezone.utc).isoformat(), "since": args.since,
           "archive_months": [months[0], months[-1]], "model_version": im.MODEL_VERSION,
           "params": params_out, "report": report_out}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=1, default=str))
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
