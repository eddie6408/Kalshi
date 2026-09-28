#!/usr/bin/env python3
"""Package 8: do the fees Kalshi CHARGED match the schedule it publishes?

    .venv/bin/python scripts/fee_check.py

Reads the demo account's fills (read only) and each series' published
schedule, and compares every fill's fee_cost with the formula. A mismatch
means the fee model is wrong somewhere - which is what this exists to catch.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kalshibot.auth import Signer  # noqa: E402
from kalshibot.clients import MarketDataClient, PortfolioClient  # noqa: E402
from kalshibot.config import load_settings  # noqa: E402
from kalshibot.fee_schedule import FeeSchedule, series_of  # noqa: E402


def main() -> int:
    s = load_settings(ROOT / "config.yaml")
    signer = Signer.from_settings(s)
    port = PortfolioClient(s.rest_base, signer)
    sched = FeeSchedule(MarketDataClient(s.rest_base, signer), enforce=False)
    fills = (port._c._request("GET", "/portfolio/fills", params={"limit": 1000}) or {}).get("fills") or []
    by = defaultdict(lambda: {"n": 0, "match": 0, "charged": 0.0, "model": 0.0, "odd": []})
    for f in fills:
        side = f.get("side")
        px = f.get(f"{side}_price_dollars")
        fee = f.get("fee_cost")
        if px is None or fee is None:
            continue
        cents, n = float(px) * 100, int(round(float(f.get("count_fp") or 0)))
        sf = sched.get(series_of(f.get("ticker")))
        model = sf.taker_fee_usd(cents, n) if f.get("is_taker") else sf.maker_fee_usd(cents, n)
        b = by[(sf.series, sf.version, "taker" if f.get("is_taker") else "maker")]
        b["n"] += 1
        b["charged"] += float(fee)
        b["model"] += model
        if abs(float(fee) - model) < 0.011:
            b["match"] += 1
        else:
            b["odd"].append((f.get("ticker"), cents, n, fee, round(model, 2)))
    print(f"{'series':<14}{'schedule':<44}{'kind':<7}{'fills':>6}{'match':>7}"
          f"{'charged':>10}{'model':>9}")
    for (series, version, kind), b in sorted(by.items()):
        print(f"{series:<14}{version:<44}{kind:<7}{b['n']:>6}{b['match']:>7}"
              f"{b['charged']:>10.2f}{b['model']:>9.2f}")
        for o in b["odd"][:3]:
            print(f"    mismatch: {o}")
    total = sum(b["n"] for b in by.values())
    good = sum(b["match"] for b in by.values())
    print(f"\n{good} of {total} fills charged exactly what the published schedule says.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
