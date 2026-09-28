#!/usr/bin/env python3
"""Package 3a: what the market-data recorder has kept, and what it shows.

    .venv/bin/python scripts/tape_report.py              # today (UTC)
    .venv/bin/python scripts/tape_report.py --days 3     # the last three days

Per day: records and bytes per source. Per series: production book reads,
spread, one-sided share, trade prints and contracts. And how far the demo
price sits from the production price for the same contract at about the same
moment (from probability_log, where both are recorded).
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kalshibot.recorder import SOURCES, iter_ticks  # noqa: E402


def series_of(ticker: str) -> str:
    return (ticker or "?").split("-")[0]


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def report_day(root: Path, day: str) -> None:
    folder = root / day
    print(f"\n=== {day} ===")
    if not folder.exists():
        print("  nothing recorded")
        return
    for src in SOURCES:
        f = folder / f"{src}.jsonl.gz"
        if f.exists():
            n = sum(1 for _ in iter_ticks(root, day, src))
            print(f"  {src:<12} {n:>10,} records  {f.stat().st_size / 1e6:8.2f} MB")

    books = defaultdict(lambda: {"reads": 0, "spreads": [], "one_sided": 0})
    for r in iter_ticks(root, day, "prod_book"):
        b = books[series_of(r.get("m"))]
        b["reads"] += 1
        yes, no = (r.get("b") or {}).get("yes") or [], (r.get("b") or {}).get("no") or []
        if yes and no:
            best_bid = max(p for p, _ in yes)
            best_ask = 100 - max(p for p, _ in no)
            b["spreads"].append(best_ask - best_bid)
        else:
            b["one_sided"] += 1
    trades = defaultdict(lambda: {"prints": 0, "contracts": 0})
    for r in iter_ticks(root, day, "prod_trades"):
        t = trades[series_of(r.get("m"))]
        t["prints"] += 1
        t["contracts"] += int(r.get("n") or 0)
    if books or trades:
        print(f"\n  {'series':<12}{'book reads':>11}{'med spread':>11}{'p90':>6}"
              f"{'one-sided':>10}{'prints':>8}{'contracts':>11}")
        for s in sorted(set(books) | set(trades)):
            b, t = books.get(s, {"reads": 0, "spreads": [], "one_sided": 0}), trades.get(s, {})
            sp = b["spreads"]
            print(f"  {s:<12}{b['reads']:>11}"
                  f"{(statistics.median(sp) if sp else float('nan')):>10.1f}c"
                  f"{(pct(sp, 0.9) if sp else 0):>5}c"
                  f"{(b['one_sided'] / b['reads'] if b['reads'] else 0):>10.0%}"
                  f"{t.get('prints', 0):>8}{t.get('contracts', 0):>11,}")


def demo_vs_production(db: Path, since: str) -> None:
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = conn.execute(
            """SELECT series, market_yes_cents, market_yes_bid_cents,
                      prod_yes_ask_cents, prod_yes_bid_cents FROM probability_log
               WHERE ts >= ? AND prod_yes_ask_cents IS NOT NULL
                 AND prod_yes_bid_cents IS NOT NULL
                 AND market_yes_cents IS NOT NULL AND market_yes_bid_cents IS NOT NULL""",
            (since,)).fetchall()
    except sqlite3.Error as exc:
        print(f"\n  demo vs production: unavailable ({exc})")
        return
    if not rows:
        print("\n  demo vs production: no forecasts with both prices yet")
        return
    gaps = defaultdict(list)
    for s, da, db_, pa, pb in rows:
        gaps[s].append(abs((da + db_) / 2 - (pa + pb) / 2))
    print("\n  demo mid vs production mid, same contract, same forecast row:")
    print(f"  {'series':<12}{'rows':>6}{'median gap':>12}{'p90':>7}")
    for s in sorted(gaps):
        g = gaps[s]
        print(f"  {s:<12}{len(g):>6}{statistics.median(g):>11.1f}c{pct(g, 0.9):>6.0f}c")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--root", default=str(ROOT / "data" / "ticks"))
    ap.add_argument("--db", default=str(ROOT / "data" / "journal.db"))
    args = ap.parse_args()
    root = Path(args.root)
    today = datetime.now(timezone.utc).date()
    days = [(today - timedelta(days=i)).isoformat() for i in range(args.days - 1, -1, -1)]
    for d in days:
        report_day(root, d)
    demo_vs_production(Path(args.db), days[0])
    total = sum(f.stat().st_size for f in root.rglob("*.gz")) if root.exists() else 0
    print(f"\n  on disk: {total / 1e6:.1f} MB in {root}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
