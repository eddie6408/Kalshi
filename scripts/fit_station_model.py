#!/usr/bin/env python3
"""How good is the weather model, station by station? A report; changes nothing.

    python scripts/fit_station_model.py

Phase B, item 7. Three questions, each answered on the unit that carries the
information - a DAY per station, not a row:

  1. Bias. Is a station forecast consistently warm or cold? Measured against
     the OFFICIAL climate report where scoring has it (actual_source), and
     validated walk-forward: an offset fitted only on earlier days, tested on
     each later day. kalshibot/station_bias.py's rule stands - 20+ distinct
     days and a walk-forward improvement, or it is not applied.

  2. Sigma. The ensemble adds station_representativeness_sigma_f (1.6F) of
     uncertainty. If that is right, (actual - mean) / sigma has a standard
     deviation near 1. Much above 1 = overconfident; much below = too timid.

  3. Nowcast vs ensemble. Once same-day markets have settled, the Brier score
     of the probability each model gave the SAME contract at the SAME moment
     (probability_log.model / alt_probability).
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean, pstdev

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kalshibot.station_bias import MIN_DAYS  # noqa: E402


def _daily(conn, official_only: bool):
    """station -> [(date, mean predicted, actual, mean sigma)] one row a day."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(forecasts)")}
    where = "actual_f IS NOT NULL"
    if official_only and "actual_source" in cols:
        where += " AND actual_source = 'nws_climate_report'"
    rows = conn.execute(
        f"SELECT station, target_date, AVG(predicted_mean_f), AVG(actual_f), "
        f"AVG(predicted_sigma_f) FROM forecasts WHERE {where} "
        f"GROUP BY station, target_date ORDER BY station, target_date").fetchall()
    out = defaultdict(list)
    for st, d, pred, act, sig in rows:
        out[st].append((d, pred, act, sig))
    return out


def bias_report(days) -> None:
    print("  1. BIAS  (predicted - actual, one point per day; walk-forward)")
    print(f"     {'station':8} {'days':>4} {'bias':>7} {'day sd':>7} "
          f"{'raw MAE':>8} {'corr MAE':>8}  verdict")
    for st, rows in sorted(days.items()):
        errs = [p - a for _d, p, a, _s in rows]
        raw, corr = [], []
        for i in range(1, len(rows)):
            offset = mean(errs[:i])
            raw.append(abs(errs[i]))
            corr.append(abs(errs[i] - offset))
        sd = pstdev(errs) if len(errs) > 1 else float("nan")
        if len(rows) < MIN_DAYS:
            verdict = f"too few days (need {MIN_DAYS})"
        elif corr and mean(corr) < mean(raw):
            verdict = "correction helps walk-forward - candidate"
        else:
            verdict = "correction does not help - keep raw"
        print(f"     {st:8} {len(rows):>4} {mean(errs):>+7.2f} {sd:>7.2f} "
              f"{(mean(raw) if raw else float('nan')):>8.2f} "
              f"{(mean(corr) if corr else float('nan')):>8.2f}  {verdict}")


def sigma_report(days) -> None:
    print("\n  2. SIGMA  (sd of (actual - predicted) / sigma; ~1.0 means right-sized)")
    for st, rows in sorted(days.items()):
        z = [(a - p) / s for _d, p, a, s in rows if s]
        if len(z) < 3:
            print(f"     {st:8} n={len(z)}  too few")
            continue
        sd = pstdev(z)
        word = ("overconfident - sigma too small" if sd > 1.25 else
                "too timid - sigma too large" if sd < 0.75 else "about right")
        print(f"     {st:8} n={len(z):>3}  sd {sd:4.2f}  {word}")


def model_report(conn) -> None:
    print("\n  3. NOWCAST vs ENSEMBLE  (same contracts, same moments; lower is better)")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(probability_log)")}
    if "alt_probability" not in cols:
        print("     no model column yet")
        return
    rows = conn.execute(
        "SELECT series, ticker, our_probability, alt_probability, settled_yes "
        "FROM probability_log WHERE model = 'nowcast' AND alt_probability IS NOT NULL "
        "AND settled_yes IS NOT NULL").fetchall()
    if not rows:
        print("     no settled nowcast forecasts yet")
        return
    per = defaultdict(list)
    for series, ticker, p_now, p_ens, y in rows:
        per[ticker].append(((p_now - y) ** 2, (p_ens - y) ** 2, series))
    diffs = [mean(a - b for a, b, _s in v) for v in per.values()]
    now_b = mean(a for v in per.values() for a, _b, _s in v)
    ens_b = mean(b for v in per.values() for _a, b, _s in v)
    se = (pstdev(diffs) / math.sqrt(len(diffs))) if len(diffs) > 1 else float("nan")
    print(f"     {len(per)} contracts, {len(rows)} forecasts: nowcast {now_b:.4f}  "
          f"ensemble {ens_b:.4f}  gap {mean(diffs):+.4f} (se {se:.4f}; negative = nowcast better)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=str(ROOT / "data" / "journal.db"))
    ap.add_argument("--all-actuals", action="store_true",
                    help="include days scored from raw readings, not only the official report")
    args = ap.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    days = _daily(conn, official_only=not args.all_actuals)
    print(f"\n  Station model report  ({'all actuals' if args.all_actuals else 'official climate report only'})\n")
    if not days:
        print("  no scored days yet" + ("" if args.all_actuals else
              " from the official report - try --all-actuals"))
    else:
        bias_report(days)
        sigma_report(days)
    model_report(conn)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
