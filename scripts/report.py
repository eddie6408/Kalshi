#!/usr/bin/env python3
"""Performance and calibration report from the journal.

    python scripts/report.py

The calibration table is the important one. Group every forecast by the
probability you assigned, then check how often those things actually
happened. A well-calibrated bot's "70%" bucket resolves YES about 70% of the
time. If your 70% bucket comes in at 45%, you do not have an edge - you have
a bug or an overconfident model, and no amount of position sizing fixes that.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.config import load_settings   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", help="journal to read (default: the one in config.yaml)")
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[1]
    if args.db:
        db = Path(args.db)
        if not db.is_absolute():
            db = root / db
    else:
        s = load_settings(str(root / "config.yaml"))
        db = s.path("journal.database_path", "data/journal.db")
    if not db.exists():
        print(f"No journal yet at {db}. Run the bot first.")
        return 0
    print(f"\nreading {db}")

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    print("\n" + "=" * 72)
    print("  DECISIONS")
    print("=" * 72)
    for r in conn.execute(
        "SELECT action, COUNT(*) n, ROUND(AVG(edge_after_fees),4) avg_edge "
        "FROM decisions GROUP BY action ORDER BY n DESC"
    ):
        print(f"  {r['action']:<12} {r['n']:>6}   avg edge {r['avg_edge']}")

    print("\n" + "=" * 72)
    print("  TRADES")
    print("=" * 72)
    row = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(contracts*price_cents)/100.0,0) cost, "
        "COALESCE(SUM(fee_usd),0) fees FROM trades WHERE action='buy'"
    ).fetchone()
    print(f"  entries        : {row['n']}")
    print(f"  gross deployed : ${row['cost']:,.2f}")
    print(f"  fees paid      : ${row['fees']:,.2f}")

    closed = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(pnl_usd),0) pnl, "
        "SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END) wins FROM trades WHERE status='closed'"
    ).fetchone()
    if closed["n"]:
        wr = 100.0 * closed["wins"] / closed["n"]
        print(f"  closed         : {closed['n']}  |  win rate {wr:.1f}%  "
              f"|  net P/L ${closed['pnl']:+,.2f}")
    else:
        print("  closed         : none yet")

    print("\n" + "=" * 72)
    print("  CALIBRATION  (are your probabilities honest?)")
    print("=" * 72)
    print("  Bucket every trade by the probability we assigned to THE SIDE WE")
    print("  BOUGHT, then check how often that side actually paid out.")
    print("  Buying NO at 20c is a 80% bet, not a 20% bet - scoring it any")
    print("  other way flatters the model.\n")

    rows = conn.execute(
        "SELECT our_probability p, side, outcome, pnl_usd FROM trades "
        "WHERE status='closed' AND our_probability IS NOT NULL "
        "AND outcome IS NOT NULL"
    ).fetchall()

    scored = []
    for r in rows:
        # our_probability is always P(YES). Flip it if we bought NO.
        p_side = r["p"] if r["side"] == "yes" else 1.0 - r["p"]
        won = (r["outcome"] == "yes") if r["side"] == "yes" else (r["outcome"] == "no")
        scored.append((p_side, won, r["pnl_usd"] or 0.0))

    closed_total = conn.execute(
        "SELECT COUNT(*) n FROM trades WHERE status='closed'").fetchone()["n"]
    unscored = closed_total - len(scored)
    if unscored > 0:
        print(f"  {unscored} of {closed_total} closed positions cannot be scored here.")
        print("  Those were exited before settlement, so the market never told us")
        print("  whether we were right - only whether the price moved our way.")
        print("  exit.flatten_minutes_before_close controls how often that happens.")
        print("  Use scripts/settle.py + the FORECAST ERROR table below to check")
        print("  the model itself, which is scored regardless of how you traded it.\n")

    if not scored:
        print("  Not enough settled trades yet. Come back after ~50 closed positions.")
        print("  (A trade only counts here once it has an outcome recorded.)")
    else:
        buckets = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)]
        print(f"  {'bucket':<12}{'n':>5}{'we said':>10}{'happened':>11}"
              f"{'gap':>8}{'net P/L':>12}")
        brier = 0.0
        for lo, hi in buckets:
            sel = [x for x in scored if lo <= x[0] < hi]
            if not sel:
                continue
            said = sum(x[0] for x in sel) / len(sel)
            happened = sum(1 for x in sel if x[1]) / len(sel)
            pnl = sum(x[2] for x in sel)
            gap = happened - said
            flag = "" if abs(gap) < 0.12 or len(sel) < 10 else "  <-- off"
            print(f"  {lo:.1f}-{hi:.1f}      {len(sel):>5}{said:>10.3f}"
                  f"{happened:>11.3f}{gap:>+8.3f}{pnl:>+12.2f}{flag}")

        brier = sum((x[0] - (1.0 if x[1] else 0.0)) ** 2 for x in scored) / len(scored)
        base = sum(1 for x in scored if x[1]) / len(scored)
        ref = base * (1 - base)
        skill = (1 - brier / ref) if ref > 0 else float("nan")
        print(f"\n  Brier score      : {brier:.4f}   (lower is better; 0.25 = coin flip)")
        print(f"  Skill vs. always guessing the base rate: {skill:+.3f}")
        print("  A negative skill score means you would do better ignoring the")
        print("  model entirely. That is the signal to stop and fix it.")
        if len(scored) < 50:
            print(f"\n  NOTE: only {len(scored)} settled trades. Do not read anything")
            print("  into this table until you have at least 50.")

    print("\n" + "=" * 72)
    print("  FORECAST ERROR (weather)")
    print("=" * 72)
    f = conn.execute(
        "SELECT COUNT(*) n, AVG(ABS(actual_f - predicted_mean_f)) mae, "
        "AVG(actual_f - predicted_mean_f) bias FROM forecasts WHERE actual_f IS NOT NULL"
    ).fetchone()
    if f["n"]:
        print(f"  n={f['n']}  mean abs error {f['mae']:.2f} F  bias {f['bias']:+.2f} F")
        print("  -> if |bias| > 1F, add a correction. If MAE is well above your")
        print("     configured sigma, raise weather.station_representativeness_sigma_f.")
    else:
        print("  No settled forecasts recorded yet.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
