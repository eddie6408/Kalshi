#!/usr/bin/env python3
"""What the bot evaluated, what it decided, and what came of it.

    python scripts/opportunity_report.py              # last 7 days
    python scripts/opportunity_report.py --days 30

Package 2 (research brief section 18). Reads the opportunity ledger,
read-only. Two kinds of money appear and are never added together:

    REALISED      what traded opportunities actually made (fills, exits,
                  settlements)
    HYPOTHETICAL  SIMULATED - what one contract/set at the recorded touch
                  would have made, for everything that was NOT traded. It
                  ignores fill probability and adverse selection.

Every hypothetical figure carries its sample size and a 95% bootstrap
interval clustered by market (a market seen every hour is one market, not
twenty-four observations).
"""
from __future__ import annotations

import argparse
import random
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _cluster_ci(rows, key="markets", value="hypothetical_pnl", n_boot=2000, seed=7):
    """Mean P&L per market, and a 95% bootstrap interval over MARKETS."""
    by: dict = defaultdict(list)
    for r in rows:
        if r[value] is not None:
            by[r[key]].append(r[value])
    per = [sum(v) / len(v) for v in by.values()]
    if not per:
        return None
    mean = sum(per) / len(per)
    if len(per) < 2:
        return mean, None, None, len(per)
    rnd = random.Random(seed)
    boots = sorted(sum(rnd.choice(per) for _ in per) / len(per) for _ in range(n_boot))
    return mean, boots[int(0.025 * n_boot)], boots[int(0.975 * n_boot)], len(per)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=str(ROOT / "data" / "journal.db"))
    ap.add_argument("--days", type=float, default=7.0)
    args = ap.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
    try:
        rows = conn.execute("SELECT * FROM opportunity_ledger WHERE detected_at >= ?",
                            (since,)).fetchall()
    except sqlite3.OperationalError:
        print("No opportunity ledger yet (Package 2 not deployed).")
        return 1
    print(f"\n  OPPORTUNITY REPORT - last {args.days:g} days, {len(rows)} evaluated\n")
    if not rows:
        return 0

    by_class = defaultdict(lambda: defaultdict(int))
    for r in rows:
        by_class[r["opportunity_class"]][r["decision"]] += 1
    print("  By class")
    for cls, d in sorted(by_class.items()):
        print(f"    {cls:<18} " + "  ".join(f"{k} {v}" for k, v in sorted(d.items())))

    traded = [r for r in rows if r["final_state"] in
              ("FILLED", "PARTIALLY_FILLED", "EXITED", "SETTLED")]
    real = [r["realised_pnl"] for r in traded if r["realised_pnl"] is not None]
    print(f"\n  Traded: {len(traded)}   REALISED P&L (resolved {len(real)}): "
          f"${sum(real):+.2f}")

    print("\n  Rejected, by stage and reason        n   resolved  HYPOTHETICAL mean/market "
          "(95% CI)  [SIMULATED]")
    groups = defaultdict(list)
    for r in rows:
        if r["decision"] == "REJECTED":
            groups[(r["decision_stage"], r["rejection_reason"])].append(r)
    for (stage, reason), rs in sorted(groups.items(), key=lambda x: -len(x[1])):
        res = [r for r in rs if r["resolved_at"]]
        ci = _cluster_ci(res)
        if ci is None:
            txt = "-"
        else:
            m, lo, hi, n = ci
            txt = (f"${m:+.3f}  ({n} markets)" if lo is None else
                   f"${m:+.3f}  (${lo:+.3f} .. ${hi:+.3f}, {n} markets)")
        print(f"    {(stage or '?')[:12]:<12} {(reason or '')[:26]:<26} {len(rs):>5} "
              f"{len(res):>9}  {txt}")
    print("\n  A positive HYPOTHETICAL mean with an interval above zero would mean a "
          "rule is turning\n  away money; an interval that spans zero means the "
          "sample cannot tell yet.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
