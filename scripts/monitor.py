#!/usr/bin/env python3
"""Live dashboard. Watch the bot scan, in real time.

    python scripts/monitor.py                 # follows the live journal
    python scripts/monitor.py --interval 5    # slower refresh

Run this in a SECOND Terminal window while the bot runs in the first.
Ctrl+C stops watching. The bot keeps running.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

B, D, X = "\033[1m", "\033[2m", "\033[0m"
G, R, Y, C, M = "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[35m"
CLEAR = "\033[2J\033[H"


def _age(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if not t.tzinfo:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds()


def _ago(iso: str | None) -> str:
    s = _age(iso)
    if s is None:
        return "-"
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s/60:.0f}m"
    return f"{s/3600:.1f}h"


def _table(conn, name) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone())


def render(conn, db: Path, rows: int) -> str:
    out: list[str] = []
    w = 96
    out.append(f"{C}{B}{'=' * w}{X}")
    out.append(f"{C}{B}  KALSHI BOT MONITOR{X}   {D}{db.name}   "
               f"{datetime.now():%H:%M:%S}{X}")
    out.append(f"{C}{B}{'=' * w}{X}")

    # ---------------- is it running? ----------------
    scan = None
    if _table(conn, "scans"):
        scan = conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    run = None
    if _table(conn, "runs"):
        run = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    mode = (run["mode"] if run else "?") or "?"
    mode_colour = {"SCAN_ONLY": C, "PAPER": Y, "DEMO_LIVE": R}.get(mode, D)

    out.append("")
    if scan is None:
        out.append(f"{R}{B}  NOT RUNNING{X}  no scan recorded yet.")
        out.append(f"{D}    Start it by double-clicking 'START BOT.command'{X}")
    else:
        age = _age(scan["ts"]) or 1e9
        if age > 600:
            out.append(f"{R}{B}  STOPPED{X}  last scan {_ago(scan['ts'])} ago")
        elif age > 180:
            out.append(f"{Y}{B}  QUIET{X}  last scan {_ago(scan['ts'])} ago")
        else:
            out.append(f"{G}{B}  RUNNING{X}  mode {mode_colour}{B}{mode}{X}  "
                       f"last scan {_ago(scan['ts'])} ago, took "
                       f"{scan['seconds']:.1f}s")
        out.append(f"{D}    {scan['markets_seen']} markets seen  |  "
                   f"{scan['priced']} order books pulled  |  "
                   f"{scan['edge_too_small']} rejected on economics  |  "
                   f"{scan['risk_rejected']} rejected on risk  |  "
                   f"{scan['traded']} traded{X}")

    # ---------------- money ----------------
    if _table(conn, "executions"):
        e = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(realised_edge_usd),0) pnl, "
            "COALESCE(SUM(total_fees_usd),0) fees, "
            "SUM(CASE WHEN outcome='COMPLETE' THEN 1 ELSE 0 END) done, "
            "SUM(exposed) exposed FROM executions").fetchone()
        pnl = e["pnl"] or 0.0
        col = G if pnl > 0 else (R if pnl < 0 else D)
        out.append("")
        out.append(f"  {B}DEMO P/L{X} {col}{B}${pnl:+,.2f}{X}    "
                   f"trades {e['n']}  complete {e['done'] or 0}  "
                   f"fees ${e['fees']:.2f}")
        if e["exposed"]:
            out.append(f"  {R}{B}  {e['exposed']} trade(s) left unhedged - "
                       f"MANUAL REVIEW{X}")

    # ---------------- sources (package 14A) ----------------
    # Where the outside information comes from and whether it is working.
    # Written by the bot every heartbeat; blank on a journal from before 14A.
    if _table(conn, "source_health"):
        srcs = conn.execute(
            "SELECT * FROM source_health ORDER BY tier, source_id").fetchall()
        if srcs:
            colour = {"HEALTHY": G, "DEGRADED": Y, "DOWN": R}
            out.append("")
            out.append(f"  {B}SOURCES{X}  {D}updated {_ago(srcs[0]['updated_at'])} ago{X}")
            line = "    "
            for r in srcs:
                cell = (f"{colour.get(r['state'], D)}{r['source_id']} "
                        f"{r['state']}{X}   ")
                if len(line) + len(r["source_id"]) + len(r["state"]) > w + 20:
                    out.append(line.rstrip())
                    line = "    "
                line += cell
            out.append(line.rstrip())
            for r in srcs:
                if r["state"] in ("DEGRADED", "DOWN"):
                    out.append(f"{D}      {r['source_id']}: {r['consecutive_failures']} "
                               f"failing in a row, error rate "
                               f"{(r['error_rate'] or 0) * 100:.0f}%, last error "
                               f"{(r['last_error'] or '-')[:70]}{X}")

    # ---------------- opportunities ----------------
    if _table(conn, "opportunities"):
        out.append("")
        out.append(f"{B}  LATEST OPPORTUNITIES{X}  "
                   f"{D}what it found and what it decided{X}")
        out.append(f"{D}    {'strategy':<18}{'legs':>5}{'qty':>5}{'cost':>9}"
                   f"{'floor':>9}{'fees':>8}{'net':>9}  decision / why{X}")
        found = list(conn.execute(
            "SELECT * FROM opportunities ORDER BY id DESC LIMIT ?", (rows,)))
        if not found:
            out.append(f"{D}    nothing priced yet{X}")
        for o in found:
            acc = o["decision"] == "TRADE"
            col = G if acc else D
            why = (o["reject_reason"] if not acc else "TRADE")[:22]
            out.append(
                f"    {o['strategy'][:17]:<18}{o['n_legs']:>5}{o['quantity']:>5}"
                f"{o['gross_cost']:>9.2f}{o['guaranteed_min_payout']:>9.2f}"
                f"{o['estimated_fees']:>8.2f}{o['net_edge']:>+9.3f}  "
                f"{col}{why}{X}")

        counts = {r["reject_reason"]: r["n"] for r in conn.execute(
            "SELECT reject_reason, COUNT(*) n FROM opportunities "
            "WHERE decision='REJECT' GROUP BY reject_reason ORDER BY n DESC")}
        if counts:
            out.append("")
            out.append(f"{D}  rejections: " +
                       "  ".join(f"{k} {v}" for k, v in list(counts.items())[:6]) + X)

    # ---------------- recent legs ----------------
    if _table(conn, "leg_orders"):
        legs = list(conn.execute(
            "SELECT * FROM leg_orders ORDER BY id DESC LIMIT 6"))
        if legs:
            out.append("")
            out.append(f"{B}  RECENT ORDERS{X}")
            for l in legs:
                st = l["state"]
                col = G if st == "FILLED" else (Y if "PARTIAL" in st else R)
                out.append(f"    {col}{st:<20}{X}{l['ticker'][:28]:<29}"
                           f"{l['side']:<4}{l['filled']}/{l['requested']} "
                           f"@ {l['avg_price_cents']:.0f}c")

    out.append("")
    out.append(f"{D}  refreshing every few seconds. Ctrl+C to stop watching - "
               f"the bot keeps running.{X}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/journal.db")
    ap.add_argument("--rows", type=int, default=12)
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[1]
    db = Path(args.db)
    if not db.is_absolute():
        db = root / db

    print(CLEAR + f"{D}waiting for {db.name} ...{X}")
    for _ in range(60):
        if db.exists():
            break
        time.sleep(1)
    if not db.exists():
        print(f"\nNo journal at {db}.\nStart the bot first "
              f"('START BOT.command'), then open this again.\n")
        return 1

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        while True:
            try:
                frame = render(conn, db, args.rows)
            except sqlite3.OperationalError as exc:
                # A dashboard must never end in a traceback. The most likely
                # cause by far is a leftover rollback journal: SQLite has to
                # replay it before it can read a single row, and replaying it
                # is a WRITE - which this read-only connection cannot do.
                print(_explain(db, exc))
                return 1
            if args.once:
                print(frame)
                return 0
            sys.stdout.write(CLEAR + frame + "\n")
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped watching\n")
    return 0


def _explain(db, exc) -> str:
    """Say what is wrong and what to do about it, in words."""
    hot = [p for p in (db.with_name(db.name + "-journal"),
                       db.with_name(db.name + "-wal")) if p.exists()]
    lines = ["", f"Cannot read {db.name}: {exc}", ""]
    if "readonly" in str(exc).lower() and hot:
        lines += [
            f"  There is a leftover {hot[0].name} beside it. That is SQLite's",
            "  rollback file, and it means a process opened the database and",
            "  did not finish. Before anything can READ the database, SQLite",
            "  has to replay that file - and replaying it is a write, which",
            "  this dashboard is not allowed to do.",
            "",
            "  Nothing is lost. Any process that opens the database for",
            "  writing will replay it automatically:",
            "",
            "      double-click  REPAIR JOURNAL.command",
            "",
            "  or just start the bot once and let it recover the database.",
        ]
    elif "readonly" in str(exc).lower():
        lines += [
            "  The file is not writable by you, or the folder is not.",
            f"  Check:  ls -l {db}",
        ]
    else:
        lines += [
            "  If the project lives on iCloud Drive, Dropbox or a network",
            "  share, SQLite cannot lock the file. Move the project to a",
            "  local folder, or point journal.database_path somewhere local.",
        ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
