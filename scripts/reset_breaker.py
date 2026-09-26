#!/usr/bin/env python3
"""Acknowledge a tripped consecutive-loss circuit breaker.

    python scripts/reset_breaker.py --status
    python scripts/reset_breaker.py --note "reviewed: arbitrage legs were being dumped"

The breaker counts losing positions in a row. Only a winning close ends a
run, and a tripped breaker opens nothing that could win - so without an
explicit reset the bot re-trips on every restart. This writes one row to the
journal's `breaker_resets`; losses closed before it stop counting. The limit
itself (safety.max_consecutive_losses) is unchanged and applies to every
NEW loss.

It does not restart the bot: the trip is held in the running process until
it restarts. START BOT (FORECAST DEMO LIVE) (VPS).command on the Mac does
both, after asking.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kalshibot.config import load_settings  # noqa: E402
from kalshibot.journal import Journal       # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--status", action="store_true", help="report only; change nothing")
    ap.add_argument("--note", default="", help="why it is being reset (required to reset)")
    args = ap.parse_args()

    settings = load_settings(ROOT / "config.yaml")
    limit = int(settings.get("safety.max_consecutive_losses", 0) or 0)
    journal = Journal(settings.path("journal.database_path", "data/journal.db"))
    run = journal.consecutive_losses()
    last = journal.last_breaker_reset()
    tripped = bool(limit) and run >= limit

    print(f"  losing positions in a row: {run}   limit: {limit or 'off'}   "
          f"{'TRIPPED' if tripped else 'ok'}")
    print(f"  last reset: {last or 'never'}")
    if args.status:
        return 2 if tripped else 0
    if not args.note.strip():
        print("  refusing to reset without --note: say what was reviewed.")
        return 1
    losses = journal.reset_breaker(args.note.strip())
    print(f"  RESET. {losses} earlier loss(es) no longer count; the limit of "
          f"{limit} applies to new losses from now.")
    print("  Restart the bot for it to take effect.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
