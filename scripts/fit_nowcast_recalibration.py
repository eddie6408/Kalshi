#!/usr/bin/env python3
"""Fit the nowcast recalibration (kalshibot/recalibration.py) from the journal.

    .venv/bin/python scripts/fit_nowcast_recalibration.py            # fit, report, write
    .venv/bin/python scripts/fit_nowcast_recalibration.py --dry-run

Reads settled nowcast probabilities (probability_log, model='nowcast', the
nowcast@2 record) read-only. One fit per kind (HIGH / LOW). A kind is
enabled only if its out-of-fold Brier (5 folds over whole city-days) beats
the raw probabilities. Writes data/nowcast_recal.json.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from kalshibot import recalibration as rc  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "journal.db"))
    ap.add_argument("--since", default="2026-09-29T05:16")
    ap.add_argument("--model", default="nowcast")
    ap.add_argument("--l2", type=float, default=2.0)
    ap.add_argument("--out", default=str(ROOT / "data" / "nowcast_recal.json"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    c = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True, timeout=30)
    rows = c.execute(
        "SELECT ticker, series, target_date, our_probability, settled_yes FROM probability_log "
        "WHERE estimator='weather_temperature' AND model=? AND settled_yes IS NOT NULL "
        "AND ts >= ?", (a.model, a.since)).fetchall()
    per = Counter(r[0] for r in rows)
    out = {"fitted_at": datetime.now(timezone.utc).isoformat(), "version": rc.VERSION,
           "model": a.model, "since": a.since, "kinds": {}}
    for kind in ("HIGH", "LOW"):
        rs = [r for r in rows if r[1].startswith("KXLOWT") == (kind == "LOW")]
        if not rs:
            continue
        p = np.array([r[3] for r in rs])
        y = np.array([r[4] for r in rs], dtype=float)
        br = np.array([1.0 if "-B" in r[0] else 0.0 for r in rs])
        w = np.array([1.0 / per[r[0]] for r in rs])
        groups = np.array([f"{r[1]}|{r[2]}" for r in rs])
        cv = rc.cross_validate(p, br, y, w, groups, l2=a.l2)
        beta = rc.fit_map(p, br, y, w, a.l2)
        enabled = cv["brier_recal"] < cv["brier_raw"] and cv["events"] >= 10
        contracts = len({r[0] for r in rs})
        out["kinds"][kind] = {"beta": beta.round(5).tolist(), "enabled": bool(enabled),
                              "rows": len(rs), "contracts": contracts, **cv}
        print(f"{kind:4}  {contracts} contracts / {cv['events']} city-days   "
              f"out-of-fold Brier  raw {cv['brier_raw']:.4f} -> recalibrated {cv['brier_recal']:.4f}"
              f"   {'ENABLED' if enabled else 'not enabled'}")
        print(f"      map: a={beta[0]:+.3f}  b={beta[1]:.3f}  range c={beta[2]:+.3f}  d={beta[3]:+.3f}")
        for raw in (0.05, 0.15, 0.25, 0.5, 0.75, 0.9):
            print(f"      {raw:.2f} -> range {rc.apply_map(beta, [raw], [1])[0]:.3f}  "
                  f"threshold {rc.apply_map(beta, [raw], [0])[0]:.3f}")
    if a.dry_run:
        return 0
    Path(a.out).write_text(json.dumps(out, indent=1))
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
