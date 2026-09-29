#!/usr/bin/env python3
"""Packages 6/7: the fake-money account on production prices.

    .venv/bin/python scripts/paper_report.py

SIMULATED: taker fills at the production book as read, held to settlement,
no money at risk. Forecast entries are split by whether the market-skill gate
would have allowed them. Plus: how often an arbitrage the demo sweep looked
at exists on production at all.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def clustered(rows, value):
    per = defaultdict(list)
    for r in rows:
        v = value(r)
        if v is not None:
            per[r["ticker"]].append(v)
    means = [sum(v) / len(v) for v in per.values()]
    if not means:
        return None, None, 0
    m = sum(means) / len(means)
    se = statistics.stdev(means) / math.sqrt(len(means)) if len(means) > 1 else None
    return m, (2 * se if se is not None else None), len(means)


def section(title, rows):
    settled = [r for r in rows if r["state"] == "SETTLED"]
    opened = [r for r in rows if r["state"] == "OPEN"]
    pnl = sum(r["pnl_usd"] or 0 for r in settled)
    exp = sum(r["expected_edge_usd"] or 0 for r in settled)
    wins = sum(1 for r in settled if (r["pnl_usd"] or 0) > 0)
    print(f"\n--- {title}: {len(settled)} settled, {len(opened)} open "
          f"(${sum(r['cost_usd'] for r in opened):.2f} tied up) ---")
    if not settled:
        return
    print(f"  realised P&L      ${pnl:+.2f}   (the strategy expected ${exp:+.2f})")
    print(f"  winners           {wins} of {len(settled)}")
    m, ci, n = clustered(settled, lambda r: 100 * (r["pnl_usd"] or 0) / max(1, r["contracts"]))
    if m is not None:
        print(f"  per contract      {m:+.2f}c" + (f" +/-{ci:.2f}" if ci else "")
              + f"  (clustered over {n} contracts)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "journal.db"))
    args = ap.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM paper_positions ORDER BY id")]
    except sqlite3.Error as exc:
        print(f"no paper account yet ({exc})")
        return 1
    print("LIVE PAPER ACCOUNT - SIMULATED on production prices; no orders, no money")
    fc = [r for r in rows if r["kind"] == "FORECAST"]
    section("weather, allowed by the market-skill gate", [r for r in fc if not r["gate_blocked"]])
    section("weather, the gate would have BLOCKED", [r for r in fc if r["gate_blocked"]])
    section("weather, all", fc)
    section("arbitrage on production books", [r for r in rows if r["kind"] == "ARB"])
    try:
        ck = [dict(r) for r in conn.execute("SELECT * FROM prod_arb_checks")]
    except sqlite3.Error:
        ck = []
    if ck:
        demo_yes = [c for c in ck if c["demo_found"]]
        print(f"\n--- arbitrage: demo vs production ({len(ck)} event checks) ---")
        print(f"  demo found an arbitrage      {len(demo_yes)}")
        print(f"  ...and it existed on prod    {sum(1 for c in demo_yes if c['prod_found'])}")
        print(f"  prod tradable (gate passed)  {sum(1 for c in ck if c['prod_accepted'])}")
        gaps = [c["prod_near_miss_gap"] for c in ck if c["prod_near_miss_gap"] is not None]
        if gaps:
            print(f"  prod closest miss            median {statistics.median(gaps):.0f}c "
                  f"from crossing (n={len(gaps)})")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
