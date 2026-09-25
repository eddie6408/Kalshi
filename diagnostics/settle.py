#!/usr/bin/env python3
"""Score past forecasts against the temperature that actually happened.

    python diagnostics/settle.py

The bot now does this on its own clock (the `scoring` worker, hourly), so
this is here for a backfill or when you want to watch it happen. Both call
the same code in kalshibot/forecast_scoring.py.

`probability_log` answers "was the probability right". This answers the
question underneath: was the TEMPERATURE right. Only this one can tell you
that a station is being forecast four degrees too cool - which is what Miami
was doing, unseen, for eight days, because this script existed and nothing
ever ran it.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.config import load_settings                    # noqa: E402
from kalshibot.forecast_scoring import (bias_by_station,      # noqa: E402
                                        score_pending)
from kalshibot.weatherdata import WeatherData                 # noqa: E402

B = "\033[1m"; D = "\033[2m"; G = "\033[32m"; R = "\033[31m"
Y = "\033[33m"; X = "\033[0m"


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    s = load_settings(str(root / "config.yaml"))
    db = s.path("journal.database_path", "data/journal.db")
    if not db.exists():
        print("No journal yet.")
        return 0

    tz_by_series = {x["ticker_prefix"]: x.get("timezone", "America/New_York")
                    for x in (s.get("weather.series") or [])}
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    wd = WeatherData(cache_seconds=1)

    print(f"\n{B}FORECAST SCORING{X}\n")
    updated, looked_up = score_pending(conn, wd._session, tz_by_series,
                                       limit=200)
    print(f"  filled {updated} forecast rows from {looked_up} station-days\n")

    rows = bias_by_station(conn)
    if not rows:
        print(f"  {D}nothing scored yet - come back once a target day is "
              f"over{X}\n")
        return 0

    print(f"  {B}Predicted mean against what actually happened{X}")
    print(f"  {D}positive bias = we forecast WARMER than it turned out{X}\n")
    print(f"  {'series':<12}{'station':<8}{'days':>6}{'rows':>6}{'bias':>9}"
          f"{'day sd':>8}{'sigma':>7}   {'within 6h':>10}")
    for r in rows:
        colour = R if abs(r["bias_f"]) >= 2.0 else (Y if abs(r["bias_f"]) >= 1.0 else G)
        short = ("--" if r["bias_short_f"] is None
                 else f"{r['bias_short_f']:+.2f}F n={r['n_short']}")
        sd = "--" if r["day_sd_f"] is None else f"{r['day_sd_f']:.2f}"
        sig = ("--" if not r["stderr_f"]
               else f"{abs(r['bias_f']) / r['stderr_f']:.1f}")
        print(f"  {r['series']:<12}{r['station']:<8}{r['n_days']:>6}{r['n']:>6}"
              f"{colour}{r['bias_f']:>+9.2f}{X}{sd:>8}{sig:>7}   {short:>10}")

    print(f"\n  {D}days, not rows, is the unit that counts: one day's weather "
          f"drives every\n  forecast made for it. The sigma column uses the "
          f"spread ACROSS days.{X}")

    bad = [r for r in rows if abs(r["bias_f"]) >= 2.0]
    if bad:
        print(f"\n  {R}{len(bad)} station(s) off by two degrees or more.{X}")
        print(f"  {D}A systematic bias is not bad luck. Check the settlement "
              f"station against\n  Kalshi's rule text (diagnostics/"
              f"weather_audit.py), and whether the ensemble\n  grid cell is "
              f"representative - coastal stations are the usual offenders.{X}")
        sep = [r for r in bad if r["separable"]]
        if not sep:
            print(f"  {D}None of them is separable from day-to-day weather "
                  f"yet. A correction was\n  built for this and REJECTED by "
                  f"walk-forward validation - see\n  kalshibot/station_bias.py"
                  f". Do not subtract these numbers.{X}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
