#!/usr/bin/env python3
"""Fit the DATA_CONFLICT threshold from what the bot has actually seen.

    python scripts/fit_conflict_threshold.py            # report
    python scripts/fit_conflict_threshold.py --pct 95   # a different percentile

Package 14A phase 4. Reads the journal read-only; changes nothing. The number
it recommends goes into config.yaml `sources.gates.conflict_threshold_f` only
after a person has read this report.

How it counts, which is the part that matters:

  - Only forecasts at lead times the bot actually trades (<= forecast.max_lead_hours).
  - One data point per CITY-DAY, not per row. The same day's forecasts share
    one weather regime; forty rows for one day are one fact. Counting rows
    is the trap kalshibot/station_bias.py documents - it made Miami's offset
    look like twelve standard errors when it was three.
  - The day's value is its median absolute gap, so one odd fetch cannot
    stand in for the day.

It also answers the question the threshold is really about: at each
candidate, how many city-days would have been blocked, and how many trades
the bot actually placed on those days.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MIN_DAYS = 20          # below this many city-days, say the fit is thin


def percentile(values: list[float], pct: float) -> float:
    s = sorted(values)
    if not s:
        return float("nan")
    k = (len(s) - 1) * pct / 100.0
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def city_days(conn, max_lead: float) -> dict[tuple[str, str], float]:
    rows = conn.execute(
        """SELECT series, target_date, diff_f FROM data_checks
           WHERE diff_f IS NOT NULL AND lead_hours IS NOT NULL
             AND lead_hours <= ?""", (max_lead,)).fetchall()
    grouped: dict[tuple[str, str], list[float]] = {}
    for series, date, diff in rows:
        grouped.setdefault((series, date), []).append(abs(float(diff)))
    return {k: median(v) for k, v in grouped.items()}


def traded_on(conn, series: str, date: str) -> int:
    """Forecast trades the bot took for that city and target date."""
    from datetime import date as _d
    try:
        tag = _d.fromisoformat(date).strftime("%y%b%d").upper()   # 26SEP25
    except ValueError:
        return 0
    return conn.execute(
        """SELECT COUNT(*) FROM opportunities
           WHERE strategy LIKE 'forecast%' AND decision='TRADE'
             AND tickers LIKE ?""", (f"{series}-{tag}%",)).fetchone()[0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=str(ROOT / "data" / "journal.db"))
    ap.add_argument("--pct", type=float, default=95.0)
    ap.add_argument("--max-lead", type=float, default=None,
                    help="lead-time ceiling in hours (default: forecast.max_lead_hours)")
    args = ap.parse_args()

    max_lead = args.max_lead
    if max_lead is None:
        try:
            from kalshibot.config import load_settings
            max_lead = float(load_settings(ROOT / "config.yaml")
                             .get("forecast.max_lead_hours", 6.0))
        except Exception:  # noqa: BLE001
            max_lead = 6.0

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        days = city_days(conn, max_lead)
    except sqlite3.OperationalError:
        print("No data_checks table yet - the phase-4 gates have not run.")
        return 1
    if not days:
        print(f"No cross-checked forecasts at <= {max_lead:g}h lead yet.")
        return 1

    gaps = list(days.values())
    fit = percentile(gaps, args.pct)
    print(f"\n  DATA_CONFLICT threshold fit  (lead <= {max_lead:g}h, one point per city-day)\n")
    print(f"  city-days: {len(gaps)}   dates: "
          f"{min(d for _, d in days)} .. {max(d for _, d in days)}")
    if len(gaps) < MIN_DAYS:
        print(f"  !! only {len(gaps)} city-days; under {MIN_DAYS} the percentile "
              f"is mostly noise. Keep collecting before enforcing.")
    print(f"  median gap {median(gaps):.2f}F   max {max(gaps):.2f}F")
    print(f"  {args.pct:g}th percentile: {fit:.2f}F   <- suggested threshold "
          f"(round up: {(int(fit * 2) + 1) / 2:.1f}F)\n")

    print("  by city (median gap, city-days):")
    by_city: dict[str, list[float]] = {}
    for (series, _d), g in days.items():
        by_city.setdefault(series, []).append(g)
    for series, g in sorted(by_city.items()):
        print(f"    {series:<12} {median(g):5.2f}F   n={len(g)}")

    print("\n  what each threshold would have blocked:")
    print(f"    {'threshold':>9}  {'city-days':>9}  {'trades on them':>14}")
    for thr in sorted({2.0, 2.5, 3.0, 3.5, 4.0, 5.0, round(fit, 1)}):
        hit = [k for k, g in days.items() if g > thr]
        trades = sum(traded_on(conn, s, d) for s, d in hit)
        print(f"    {thr:>8.1f}F  {len(hit):>9}  {trades:>14}")
    print("\n  To apply: set sources.gates.conflict_threshold_f in config.yaml,")
    print("  keep conflict_mode: shadow for a day, then switch it to enforce.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
