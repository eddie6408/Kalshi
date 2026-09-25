#!/usr/bin/env python3
"""Prometheus - the dashboard, read straight from the journal.

    python scripts/dashboard.py            # build and open it
    python scripts/dashboard.py --no-open  # just build it

Everything here is a READ. The database is opened immutable, so this can run
while the bot is trading and cannot lock, block or corrupt anything - the same
mistake that left a hot rollback journal behind on 17 September is not
available to it.

The page answers, in order, the questions worth asking of a forecasting bot:

    Is it calibrated?          the reliability curve
    Where is it weak?          Brier by city and by lead time
    Is it making money?        realised P/L per market, and what is still open
    Why is it not trading?     rejection reasons, counted
    Is it alive?               last run, last pass, settlement backlog
"""
from __future__ import annotations

import argparse
import html
import json
import shutil
import sqlite3
import sys
import tempfile
import webbrowser
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: The day the lead-time gate went in. Forecasts before it came from a bot
#: that traded fourteen days out; forecasts after it came from one that trades
#: six hours out. Pooling them describes neither.
REGIME_BOUNDARY = "2026-09-21"
REGIME_LABEL = "6-hour lead gate"

#: Smallest bucket that can support a frequency on the reliability curve.
#: Was 5, which plotted noise at full size.
MIN_BUCKET_N = 30

#: Lead buckets, matched to the gate so the band actually traded stands alone.
LEAD_BUCKET_SQL = """CASE WHEN lead_hours < 6  THEN '0-6h'
                          WHEN lead_hours < 12 THEN '6-12h'
                          WHEN lead_hours < 24 THEN '12-24h'
                          ELSE '24h+' END"""
TRADED_LEAD_BUCKETS = {"0-6h"}


# ----------------------------------------------------------------------
def open_journal(path: Path) -> sqlite3.Connection:
    """Read-only, and unable to take a lock.

    `immutable=1` promises SQLite the file will not change underneath it,
    which lets it skip locking entirely. That is what makes this safe to run
    against a live bot - and on a network or synced folder it is the only way
    to read the file at all.
    """
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        return conn
    except sqlite3.Error:
        # A copy is the fallback, never a read-write open.
        tmp = Path(tempfile.gettempdir()) / "prometheus-dashboard-copy.db"
        shutil.copy2(path, tmp)
        conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn


def q(conn, sql, *args):
    try:
        return list(conn.execute(sql, args))
    except sqlite3.Error:
        return []


def one(conn, sql, *args, default=None):
    rows = q(conn, sql, *args)
    return rows[0] if rows else default


def _wilson(observed: float, n: int, z: float = 1.96) -> dict:
    """95% interval on an observed frequency.

    Wilson rather than the textbook +-1.96*sqrt(p(1-p)/n), because that one
    collapses to zero width exactly when the bucket is most suspicious: six
    outcomes that all happened give p=1 and an interval of +-0.000, drawn as
    a confident point. Wilson gives that bucket [0.61, 1.0], which is the
    honest answer.
    """
    if n <= 0:
        return {"lo": observed, "hi": observed}
    centre = (observed + z * z / (2 * n)) / (1 + z * z / n)
    half = (z / (1 + z * z / n)) * (
        (observed * (1 - observed) / n + z * z / (4 * n * n)) ** 0.5)
    return {"lo": max(0.0, centre - half), "hi": min(1.0, centre + half)}


def _skill(conn, key_name: str, key_sql: str, where: str = "1=1") -> list[dict]:
    """Brier AND Brier Skill Score against each group's own climatology.

    A group's climatology is p(1-p) at its own base rate, so a city whose
    questions resolve YES a quarter of the time is not compared against a city
    whose questions resolve YES a fifth of the time. BSS above zero is skill;
    below zero is worse than a table of long-run frequencies.
    """
    rows = q(conn, f"""SELECT {key_sql} k, COUNT(*) n, AVG(brier) b,
                              AVG(settled_yes) base
                       FROM probability_log
                       WHERE settled_yes IS NOT NULL AND {where}
                       GROUP BY k""")
    out = []
    for x in rows:
        if x["b"] is None or x["base"] is None:
            continue
        base = float(x["base"])
        clim = base * (1 - base)
        brier = float(x["b"])
        out.append({
            "key": "(before the series fix)" if x["k"] == "?" else str(x["k"]),
            "n": int(x["n"]), "brier": brier, "base": base,
            "climatology": clim,
            "bss": (1 - brier / clim) if clim > 0 else None,
        })
    out.sort(key=lambda r: -(r["bss"] if r["bss"] is not None else -9))
    return out


def _bias(conn) -> list[dict]:
    """Predicted temperature against observed, per station.

    Empty until the scoring worker has filled forecasts.actual_f. That table
    was empty for eight days while Miami ran four degrees cold, which is the
    whole reason this panel exists.
    """
    try:
        sys.path.insert(0, str(ROOT))
        from kalshibot.forecast_scoring import bias_by_station
        return bias_by_station(conn)
    except Exception:       # noqa: BLE001
        return []


# ----------------------------------------------------------------------
def gather(conn) -> dict:
    d: dict = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    # ---- forecasts and calibration ----------------------------------
    r = one(conn, """SELECT COUNT(*) n, SUM(settled_yes IS NOT NULL) settled,
                            AVG(CASE WHEN settled_yes IS NOT NULL THEN brier END) brier,
                            AVG(CASE WHEN settled_yes IS NOT NULL THEN our_probability END) mean_p,
                            AVG(settled_yes) base FROM probability_log""")
    n = int(r["n"] or 0) if r else 0
    settled = int(r["settled"] or 0) if r else 0
    base = float(r["base"]) if r and r["base"] is not None else None
    # Effective sample size, which is not the row count.
    #
    # Each contract is re-forecast roughly six times a day, so 452 settled
    # rows were only 74 distinct contracts, and a bootstrap over CONTRACTS
    # put the aggregate skill interval at [-0.018, +0.198] - straddling zero.
    # The verdict tile used to read "beating climatology" off the row count
    # alone, which is the single boldest claim on the page and one the
    # evidence does not support.
    contracts = int(one(conn, """SELECT COUNT(*) n FROM (
                            SELECT DISTINCT ticker, strike FROM probability_log
                            WHERE settled_yes IS NOT NULL)""")["n"] or 0)

    d["forecasts"] = {
        "logged": n, "settled": settled, "contracts": contracts,
        "brier": float(r["brier"]) if r and r["brier"] is not None else None,
        "mean_forecast": float(r["mean_p"]) if r and r["mean_p"] is not None else None,
        "base_rate": base,
        "climatology": base * (1 - base) if base is not None else None,
    }

    # Which bot made these forecasts?
    #
    # On 21 September the lead-time gate went in and the bot stopped trading
    # anything more than six hours out. Everything logged before that came
    # from a materially different machine, and averaging the two into one
    # number describes a bot that no longer exists. So the record is split,
    # loudly, and every panel says which side of the line it is standing on.
    before = int(one(conn, """SELECT COUNT(*) n FROM probability_log
                             WHERE settled_yes IS NOT NULL AND ts < ?""",
                     REGIME_BOUNDARY)["n"] or 0)
    since = settled - before
    d["regime"] = {"boundary": REGIME_BOUNDARY, "change": REGIME_LABEL,
                   "before": before, "since": since}

    # Reliability: what we said, against what happened. The one chart that
    # tells you whether a forecaster is honest.
    #
    # The threshold used to be five. Five outcomes cannot estimate a
    # frequency - three buckets of six, seven and two rows were being drawn
    # at full size next to a bucket of 329, all three reading 100%, and the
    # curve looked broken when it was only thin. Each point now carries the
    # Wilson interval on its observed rate, so a thin bucket LOOKS thin.
    d["reliability"] = [
        {"bucket": int(x["b"]), "n": int(x["n"]),
         "forecast": float(x["p"]), "observed": float(x["obs"]),
         **_wilson(float(x["obs"]), int(x["n"]))}
        for x in q(conn, """SELECT CAST(our_probability*10 AS INT) b, COUNT(*) n,
                                   AVG(our_probability) p, AVG(settled_yes) obs
                            FROM probability_log WHERE settled_yes IS NOT NULL
                            GROUP BY b HAVING n >= ? ORDER BY b""",
                  MIN_BUCKET_N)]
    d["reliability_dropped"] = [
        {"bucket": int(x["b"]), "n": int(x["n"])}
        for x in q(conn, """SELECT CAST(our_probability*10 AS INT) b, COUNT(*) n
                            FROM probability_log WHERE settled_yes IS NOT NULL
                            GROUP BY b HAVING n < ? ORDER BY b""",
                  MIN_BUCKET_N)]

    # Skill, not raw Brier.
    #
    # Brier is not comparable across cities. Miami's questions are harder than
    # New York's - a higher base rate means a higher floor - so Miami reading
    # 0.1997 against New York's 0.0831 overstates the gap and hides the sign.
    # Against each city's OWN climatology: New York +0.414, Miami -0.184. One
    # of those is skill and the other is worse than a climate table, which the
    # raw numbers never said out loud.
    d["by_series"] = _skill(conn, "series", "COALESCE(NULLIF(series,''), '?')")
    d["by_lead"] = _skill(conn, "bucket", LEAD_BUCKET_SQL,
                          where="lead_hours IS NOT NULL")
    order = ["0-6h", "6-12h", "12-24h", "24h+"]
    d["by_lead"].sort(key=lambda r: order.index(r["key"])
                      if r["key"] in order else 99)
    for r in d["by_lead"]:
        r["traded"] = r["key"] in TRADED_LEAD_BUCKETS

    # Forecast error in degrees, which is where a station problem shows up
    # as itself rather than as a bad probability.
    d["bias"] = _bias(conn)

    # ---- money, from real fills only --------------------------------
    flows: dict = defaultdict(lambda: {"bought": 0, "sold": 0, "spent": 0.0,
                                       "got": 0.0, "fees": 0.0, "last": "",
                                       "settled": 0})
    for x in q(conn, "SELECT * FROM leg_orders WHERE filled > 0 ORDER BY id"):
        n_f = int(x["filled"] or 0)
        px = float(x["avg_price_cents"] or x["limit_price_cents"] or 0) / 100.0
        f = flows[(x["ticker"], x["side"])]
        f["fees"] += float(x["fee_usd"] or 0)
        f["last"] = x["ts"] or ""
        if x["action"] == "buy":
            f["bought"] += n_f; f["spent"] += n_f * px
        else:
            f["sold"] += n_f; f["got"] += n_f * px
            # A settlement is not a sale. It closes the line at 100c or 0c
            # with no order sent, and saying "sold at 100c" would describe a
            # trade that never happened. Counted in the money exactly the
            # same way; labelled differently.
            if str(x["state"] or "") == "SETTLED":
                f["settled"] += n_f

    realised, spent, got, fees = 0.0, 0.0, 0.0, 0.0
    per_market, open_rows = [], []
    for (ticker, side), f in sorted(flows.items()):
        spent += f["spent"]; got += f["got"]; fees += f["fees"]
        net_qty = f["bought"] - f["sold"]
        if f["bought"] and f["sold"]:
            matched = min(f["bought"], f["sold"])
            pnl = matched * (f["got"] / f["sold"] - f["spent"] / f["bought"])
            realised += pnl
            per_market.append({"ticker": ticker, "side": side, "pnl": pnl,
                               "contracts": matched,
                               "buy": 100 * f["spent"] / f["bought"],
                               "sell": 100 * f["got"] / f["sold"],
                               "settled": f["settled"] > 0})
        if net_qty:
            open_rows.append({
                "ticker": ticker, "side": side, "qty": net_qty,
                "avg": 100 * f["spent"] / f["bought"] if f["bought"] else 0.0,
                "cost": f["spent"] - f["got"], "last": f["last"][:19]})
    per_market.sort(key=lambda r: r["pnl"])
    d["per_market"] = per_market
    d["open"] = sorted(open_rows, key=lambda r: -abs(r["qty"]))
    d["money"] = {"realised": realised, "spent": spent, "received": got,
                  "fees": fees, "net_cash": got - spent,
                  "open_cost": sum(r["cost"] for r in open_rows if r["qty"] > 0)}

    # ---- why it did not trade ---------------------------------------
    d["rejections"] = [
        {"key": x["reject_reason"] or "(none given)", "n": int(x["n"])}
        for x in q(conn, """SELECT reject_reason, COUNT(*) n FROM opportunities
                            WHERE decision != 'TRADE' GROUP BY reject_reason
                            ORDER BY n DESC LIMIT 10""")]
    tally = one(conn, """SELECT SUM(decision='TRADE') t, COUNT(*) n FROM opportunities""")
    d["opportunities"] = {"total": int(tally["n"] or 0) if tally else 0,
                          "accepted": int(tally["t"] or 0) if tally else 0}

    # ---- is it alive -------------------------------------------------
    run = one(conn, "SELECT * FROM runs ORDER BY id DESC LIMIT 1")
    scan = one(conn, "SELECT * FROM scans ORDER BY id DESC LIMIT 1")
    pending = one(conn, """SELECT COUNT(*) n FROM probability_log
                           WHERE settled_yes IS NULL""")
    d["health"] = {
        "run_started": (run["started_ts"][:19] if run and run["started_ts"] else None),
        "run_ended": (run["ended_ts"][:19] if run and run["ended_ts"] else None),
        "mode": run["mode"] if run else None,
        "last_scan": (scan["ts"][:19] if scan and scan["ts"] else None),
        "last_scan_priced": int(scan["priced"] or 0) if scan else 0,
        "last_scan_seconds": float(scan["seconds"] or 0) if scan else 0.0,
        "unsettled": int(pending["n"] or 0) if pending else 0,
    }
    d["scans"] = [{"ts": x["ts"][:19], "priced": int(x["priced"] or 0),
                   "traded": int(x["traded"] or 0),
                   "seconds": float(x["seconds"] or 0)}
                  for x in q(conn, "SELECT * FROM scans ORDER BY id DESC LIMIT 40")][::-1]
    return d


# ----------------------------------------------------------------------
def render(d: dict, template: Path) -> str:
    page = template.read_text()
    return page.replace("/*__DATA__*/null",
                        json.dumps(d, default=str, indent=None))


def main() -> int:
    ap = argparse.ArgumentParser(description="Build Prometheus, the kalshibot dashboard")
    ap.add_argument("--db", default=str(ROOT / "data" / "journal.db"))
    ap.add_argument("--out", default=str(ROOT / "data" / "dashboard.html"))
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"\nNo journal at {db}.\nStart the bot once, then build the "
              f"dashboard.\n", file=sys.stderr)
        return 1

    conn = open_journal(db)
    data = gather(conn)
    conn.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(data, ROOT / "scripts" / "dashboard_template.html"))

    f = data["forecasts"]
    print(f"\n  Prometheus written to {out}")
    print(f"  {f['logged']} forecasts logged, {f['settled']} settled")
    if f["brier"] is not None and f["climatology"] is not None:
        edge = f["climatology"] - f["brier"]
        print(f"  Brier {f['brier']:.4f} vs climatology {f['climatology']:.4f} "
              f"({edge:+.4f})")
    print(f"  realised ${data['money']['realised']:+.2f}, "
          f"{len(data['open'])} open lines\n")

    if not args.no_open:
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
