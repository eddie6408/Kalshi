#!/usr/bin/env python3
"""Package C2, phase M0: would resting (maker) orders have worked?

    .venv/bin/python scripts/maker_report.py
    .venv/bin/python scripts/maker_report.py --since 2026-09-28

Every number here is SIMULATED against the production trade tape - no order
was sent. Read with that in mind, and with the sample sizes shown.

  fill rate        share of posted orders that got any fill before expiry
  time to fill     minutes from posting to the first fill
  drift N m        production mid on our side N minutes after the first
                   fill, minus our price (cents; negative = moved against us,
                   i.e. adverse selection)
  maker markout    per filled contract at settlement: payout - price - fee
  taker markout    per contract had we crossed at the ask instead, for the
                   same decisions (filled and unfilled) - the comparison
  +/- figures      2 standard errors, clustered by contract (each ticker's
                   orders averaged first)
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def clustered(rows, value) -> tuple[float | None, float | None, int]:
    """Mean of per-ticker means, and 2 SE across tickers."""
    per = defaultdict(list)
    for r in rows:
        v = value(r)
        if v is not None:
            per[r["ticker"]].append(v)
    means = [sum(v) / len(v) for v in per.values()]
    if not means:
        return None, None, 0
    m = sum(means) / len(means)
    se = (statistics.stdev(means) / math.sqrt(len(means))) if len(means) > 1 else None
    return m, (2 * se if se is not None else None), len(means)


def fmt(m, ci, n, unit="c") -> str:
    if m is None:
        return "   -"
    return f"{m:+6.2f}{unit}" + (f" +/-{ci:.2f}" if ci is not None else "") + f"  (n={n})"


def section(title: str, rows: list[dict]) -> None:
    done = [r for r in rows if r["state"] in ("FILLED", "PARTIAL", "EXPIRED")]
    print(f"\n--- {title}: {len(done)} closed simulated orders "
          f"({len({r['ticker'] for r in done})} contracts) ---")
    if not done:
        return
    filled = [r for r in done if (r["filled"] or 0) > 0]
    full = [r for r in done if r["state"] == "FILLED"]
    through = [r for r in filled if r["fill_kind"] == "through"]
    print(f"  fill rate          {len(filled) / len(done):6.1%}  "
          f"(full {len(full) / len(done):.1%}; {len(through)} of {len(filled)} "
          f"by a print through our price)")
    ttf = [(r["first_fill_at"] - r["created_at"]) / 60 for r in filled]
    if ttf:
        print(f"  time to fill       median {statistics.median(ttf):.1f} min")
    print(f"  posted vs touch    median {statistics.median([r['best_bid'] - r['price_cents'] for r in done]):+.0f}c "
          f"from the bid, spread median {statistics.median([r['best_ask'] - r['best_bid'] for r in done]):.0f}c")
    for m in (5, 30, 60):
        print(f"  drift {m:>2}m          " + fmt(*clustered(filled, lambda r, m=m: r[f"drift_{m}m"])))
    settled = [r for r in done if r["settled_yes"] is not None]
    print(f"  settled            {len(settled)} of {len(done)}")
    if settled:
        print("  maker markout      " + fmt(*clustered(
            [r for r in settled if r["maker_markout_cents"] is not None],
            lambda r: r["maker_markout_cents"])) + "   per filled contract")
        print("  taker markout      " + fmt(*clustered(settled, lambda r: r["taker_markout_cents"]))
              + "   crossing instead, all decisions")
        print("    ...on filled     " + fmt(*clustered(
            [r for r in settled if (r["filled"] or 0) > 0], lambda r: r["taker_markout_cents"])))
        print("    ...on unfilled   " + fmt(*clustered(
            [r for r in settled if not (r["filled"] or 0)], lambda r: r["taker_markout_cents"]))
              + "   (the trades a maker order would have missed)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "journal.db"))
    ap.add_argument("--since", default="", help="UTC date, e.g. 2026-09-28")
    args = ap.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    since = (datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc).timestamp()
             if args.since else 0)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM maker_shadow WHERE created_at >= ? ORDER BY id", (since,))]
    except sqlite3.Error as exc:
        print(f"no maker_shadow table yet ({exc})")
        return 1
    counts = defaultdict(int)
    for r in rows:
        counts[r["state"]] += 1
    print("MAKER SHADOW (C2 M0) - SIMULATED on the production tape; no orders sent")
    print("  rows: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    section("all", rows)
    section("lead <= 6h (where the strategy trades)",
            [r for r in rows if r["lead_hours"] is not None and r["lead_hours"] <= 6])
    section("lead > 6h", [r for r in rows if r["lead_hours"] is not None and r["lead_hours"] > 6])
    section("accepted by the strategy", [r for r in rows if r["stage"] == "ACCEPTED"])
    section("blocked: market forecasts better", [r for r in rows if r["stage"] == "MARKET_SKILL"])
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
