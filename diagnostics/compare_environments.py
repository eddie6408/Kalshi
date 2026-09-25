#!/usr/bin/env python3
"""Is there anything actually tradable? Compare demo against production.

    python scripts/compare_environments.py

Market data is public, so this works with a demo key against both exchanges.
It answers one question: are these markets being quoted by anyone?

A market with no bid and no ask has no order book. You cannot buy it at any
price. If every market on an exchange looks like that, no trading strategy
will ever fire there - the problem is the venue, not your model.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.auth import Signer                       # noqa: E402
from kalshibot.client import KalshiClient, KalshiError  # noqa: E402
from kalshibot.config import ENDPOINTS, load_settings   # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
G, R, Y, B, D, X = "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[2m", "\033[0m"


def survey(base: str, signer, prefixes: list[str]) -> dict:
    client = KalshiClient(base, signer)
    out = {"markets": 0, "quoted": 0, "tight": 0, "examples": []}
    for prefix in prefixes:
        try:
            markets = client.get_markets(series_ticker=prefix, status="open")
        except KalshiError:
            continue
        for m in markets:
            out["markets"] += 1
            if m.yes_bid and m.yes_ask:
                out["quoted"] += 1
                if m.spread_cents <= 4:
                    out["tight"] += 1
                if len(out["examples"]) < 4:
                    out["examples"].append(
                        f"{m.ticker:<26} {m.yes_bid:>3}/{m.yes_ask:<3}c  "
                        f"spread {m.spread_cents}c  vol {m.volume}")
    return out


def main() -> int:
    s = load_settings(str(ROOT / "config.yaml"))
    signer = Signer.from_settings(s)
    prefixes = [x["ticker_prefix"] for x in (s.get("weather.series") or [])]

    print(f"\n{B}Comparing weather markets on both Kalshi exchanges{X}")
    print(f"{D}A market with no bid AND no ask cannot be traded at any price.{X}\n")

    results = {}
    for env, urls in ENDPOINTS.items():
        print(f"{B}{env.upper()}{X}  {D}{urls['rest']}{X}")
        r = survey(urls["rest"], signer, prefixes)
        results[env] = r
        if r["markets"] == 0:
            print(f"  {R}no markets readable here{X}\n")
            continue
        pct = 100.0 * r["quoted"] / r["markets"]
        colour = G if pct > 50 else (Y if pct > 5 else R)
        print(f"  {r['markets']:>3} open markets")
        print(f"  {colour}{r['quoted']:>3} with a two-sided quote ({pct:.0f}%){X}")
        print(f"  {r['tight']:>3} with a spread of 4c or less "
              f"{D}(the bot's default limit){X}")
        for ex in r["examples"]:
            print(f"    {D}{ex}{X}")
        print()

    demo, prod = results.get("demo", {}), results.get("prod", {})
    print(f"{B}{'=' * 66}{X}")
    if demo.get("tight", 0) > 0:
        print(f"{G}  The demo exchange has tradable markets. Stay on demo.{X}")
    elif prod.get("tight", 0) > 0:
        print(f"{Y}  The demo exchange has nothing to trade, but production does.{X}\n")
        print("  The useful configuration is PAPER MODE against PRODUCTION prices:")
        print("  real order books, real spreads, simulated fills, zero money at risk.")
        print(f"\n  In {B}.env{X}:          KALSHI_ENV=prod")
        print(f"  In {B}config.yaml{X}:   execution:")
        print("                    mode: paper")
        print(f"\n  {D}Market data is public, so your demo key reads production prices")
        print(f"  fine. In paper mode the bot never calls a trading endpoint at all,")
        print(f"  so it cannot place an order even if it wanted to.{X}")
        print(f"\n  {R}Do NOT set mode to 'demo' or 'live' with KALSHI_ENV=prod.{X}")
        print(f"  {D}The bot blocks 'live' outright without an explicit confirmation flag.{X}")
    else:
        print(f"{R}  Neither exchange is quoting these markets right now.{X}")
        print("  Weather markets are thin outside US trading hours. Try again later,")
        print("  or widen edge.max_spread_cents in config.yaml.")
    print(f"{B}{'=' * 66}{X}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
