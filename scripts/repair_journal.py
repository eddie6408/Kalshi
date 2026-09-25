#!/usr/bin/env python3
"""Recover a SQLite database that was left with a hot rollback journal.

A `journal.db-journal` (or `-wal`) file beside the database means some process
opened it, started writing, and did not finish. SQLite will not let ANYTHING
read the database until that file has been replayed - and replaying it is a
write, so a read-only reader like the dashboard fails with

    attempt to write a readonly database

on what looks like a harmless SELECT. Nothing is lost. The fix is simply to
open the database once with write access and let SQLite do the replay itself.

This script does that, verifies the result, and never deletes a rollback
journal by hand - doing that is how a recoverable database becomes a corrupt
one.
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "journal.db"

G = "\033[32m"; R = "\033[31m"; Y = "\033[33m"; B = "\033[1m"; X = "\033[0m"


def main() -> int:
    print(f"\n{B}Journal repair{X}\n  {DB}\n")
    if not DB.exists():
        print(f"{R}No database at that path.{X}\n")
        return 1

    strays = [p for p in (DB.with_name(DB.name + "-journal"),
                          DB.with_name(DB.name + "-wal"),
                          DB.with_name(DB.name + "-shm")) if p.exists()]
    if strays:
        for p in strays:
            print(f"  found {p.name}  ({p.stat().st_size} bytes)")
    else:
        print("  no rollback journal present - nothing to replay")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = DB.with_name(f"{DB.stem}.backup-{stamp}{DB.suffix}")
    shutil.copy2(DB, backup)
    for p in strays:
        shutil.copy2(p, p.with_name(p.name + f".backup-{stamp}"))
    print(f"\n  backed up to {backup.name}")

    print("\n  opening read-write so SQLite can replay it ...")
    try:
        conn = sqlite3.connect(str(DB), timeout=30)
        conn.row_factory = sqlite3.Row
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            print(f"\n{R}  integrity check says: {result}{X}")
            print(f"  Your backup is {backup.name}. Stop here and ask.\n")
            return 2
        print(f"{G}  integrity check: ok{X}")

        tables = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        print(f"\n  {len(tables)} tables:")
        for t in tables:
            try:
                n = conn.execute(f"SELECT COUNT(*) c FROM '{t}'").fetchone()["c"]
                print(f"    {t:<24} {n:>7} rows")
            except sqlite3.Error as exc:
                print(f"    {t:<24} {Y}unreadable: {exc}{X}")
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        print(f"\n{R}  could not open it: {exc}{X}")
        print(f"  Your backup is {backup.name}.\n")
        return 2

    left = [p for p in strays if p.exists()]
    if left:
        print(f"\n{Y}  still present: {', '.join(p.name for p in left)}{X}")
        print("  That is unusual. Do NOT delete it by hand - say so instead.")
    else:
        print(f"\n{G}  rollback journal replayed and cleared.{X}")

    print(f"\n{G}{B}  Done. WATCH BOT.command should work now.{X}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
