#!/usr/bin/env python3
"""Find the real Kalshi series tickers for a keyword.

Ticker names change. If check_connection.py reports zero markets, run:

    python scripts/find_series.py temperature
    python scripts/find_series.py high
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.auth import Signer                          # noqa: E402
from kalshibot.client import KalshiClient, KalshiError     # noqa: E402
from kalshibot.config import load_settings                 # noqa: E402


def main() -> int:
    keyword = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    s = load_settings(str(Path(__file__).resolve().parents[1] / "config.yaml"))
    client = KalshiClient(s.rest_base, Signer.from_settings(s))

    print(f"\nScanning open markets for {keyword!r} ...\n")
    try:
        markets = client.get_markets(status="open", max_pages=40)
    except KalshiError as exc:
        print("Failed:", exc)
        return 1

    counts: Counter[str] = Counter()
    samples: dict[str, str] = {}
    for m in markets:
        blob = f"{m.ticker} {m.title} {m.subtitle}".lower()
        if keyword and keyword not in blob:
            continue
        series = m.series_ticker or m.ticker.split("-")[0]
        counts[series] += 1
        samples.setdefault(series, f"{m.ticker} | {m.title or m.subtitle}")

    if not counts:
        print("Nothing matched. Try a shorter keyword, or no keyword at all.")
        return 0

    print(f"{'SERIES':<18}{'#':>5}   EXAMPLE")
    print("-" * 100)
    for series, n in counts.most_common(60):
        print(f"{series:<18}{n:>5}   {samples[series][:70]}")
    print(f"\nScanned {len(markets)} open markets total.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
