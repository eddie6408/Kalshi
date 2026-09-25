#!/usr/bin/env python3
"""What does execution really cost? Demo book against live book.

    python diagnostics/execution_report.py
    python diagnostics/execution_report.py --since 2026-09-25

The forecasting edge is measured. This is the other half: what it costs to
take it. Everything the bot has learned about fills came from a synthetic
demo book, so until this table has rows there is no basis for believing any
trade would have executed the way the record says it did.

Read it like this: if live spreads are wider and live slippage is larger, the
edge measured on demo is an overstatement, and the difference is roughly what
it is overstated by - per contract, before fees.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.config import load_settings              # noqa: E402
from kalshibot.execution_probe import summarise         # noqa: E402

B = "\033[1m"; D = "\033[2m"; G = "\033[32m"; R = "\033[31m"
Y = "\033[33m"; X = "\033[0m"


def main() -> int:
    since = None
    if "--since" in sys.argv:
        since = sys.argv[sys.argv.index("--since") + 1]

    root = Path(__file__).resolve().parents[1]
    s = load_settings(str(root / "config.yaml"))
    db = s.path("journal.database_path", "data/journal.db")
    if not db.exists():
        print("No journal yet.")
        return 0
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    sql = "SELECT * FROM book_quality"
    args: tuple = ()
    if since:
        sql += " WHERE ts >= ?"
        args = (since,)
    try:
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY id", args)]
    except sqlite3.Error:
        rows = []

    print(f"\n{B}EXECUTION QUALITY{X}  {D}demo book vs live production book{X}\n")
    if not rows:
        print(f"  {D}Nothing sampled yet. The probe runs every 15 minutes once"
              f"\n  execution_probe.enabled is true and the bot is running.{X}\n")
        return 0

    su = summarise(rows)
    size = rows[-1].get("size") or 25

    def c(v):
        return "--" if v is None else f"{v:.2f}c"

    print(f"  {su['n']} samples, {su['comparable']} comparable, "
          f"order size {size} contracts\n")
    print(f"  {'':<22}{'demo':>10}{'live':>10}{'difference':>14}")
    print(f"  {'spread':<22}{c(su['demo_spread']):>10}{c(su['live_spread']):>10}"
          f"{c(None if None in (su['live_spread'], su['demo_spread']) else su['live_spread'] - su['demo_spread']):>14}")
    print(f"  {'slippage on ' + str(size):<22}{c(su['demo_slippage']):>10}"
          f"{c(su['live_slippage']):>10}{c(su['cost_gap']):>14}")
    print()

    fill = su["live_fill_rate"]
    colour = G if fill > 0.9 else (Y if fill > 0.7 else R)
    print(f"  live book could fill {size} contracts: "
          f"{colour}{fill:.0%}{X} of the time "
          f"({su['live_could_not_fill']} of {su['n']} could not)")

    gap = su["cost_gap"]
    if gap is not None:
        per_trade = gap * size / 100.0
        print()
        if gap > 0:
            print(f"  {R}A {size}-contract order costs {gap:.2f}c per contract MORE "
                  f"on the real book{X}")
            print(f"  {D}about ${per_trade:.2f} per trade, before fees. Any edge "
                  f"measured on demo is\n  overstated by roughly that much.{X}")
        else:
            print(f"  {G}The real book is no worse than demo "
                  f"({gap:+.2f}c per contract).{X}")
            print(f"  {D}Unusual, and worth checking the sample covers markets "
                  f"the bot would really trade.{X}")

    print(f"\n  {D}Sampled markets are the ones the bot was actively pricing. "
          f"A market the live\n  exchange does not list is skipped rather than "
          f"compared against nothing.{X}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
