#!/usr/bin/env python3
"""Is a market family worth building a strategy for?

    python diagnostics/strategy_scout.py fed rate fomc
    python diagnostics/strategy_scout.py bitcoin
    python diagnostics/strategy_scout.py            # the built-in shortlist

`find_series.py` answers "does this series exist". That is only half the
question, and the cheaper half. On 21 September the demo board turned out to
carry KXSOFRD - seven markets, and **not one of them had a book**. A series
that exists and nobody quotes is a strategy with nothing to trade.

So this asks three things in order, and stops as soon as the answer is no:

    1. does the exchange list it?
    2. are the markets quoted at all?
    3. is the spread narrow enough to cross?

That third one is the lesson of 19 September, when a book quoting 14c against
83c cost $24 in a day. A family whose spreads are all 40c wide is not
tradeable however good the forecast is.
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.auth import Signer                          # noqa: E402
from kalshibot.clients import MarketDataClient             # noqa: E402
from kalshibot.client import KalshiError                   # noqa: E402
from kalshibot.config import load_settings                 # noqa: E402

B = "\033[1m"; D = "\033[2m"; G = "\033[32m"; R = "\033[31m"
Y = "\033[33m"; C = "\033[36m"; X = "\033[0m"

#: What we would consider building next, so the bare command is useful.
SHORTLIST = ["fed", "fomc", "rate", "cpi", "inflation", "bitcoin", "ethereum",
             "nasdaq", "s&p", "gold", "oil"]

#: Books wider than this are not worth crossing. Matches forecast.max_spread_cents.
MAX_SPREAD = 15
#: How many books to pull per series. Enough to judge, cheap enough to be quick.
SAMPLE = 6


def main() -> int:
    words = [w.lower() for w in sys.argv[1:]] or SHORTLIST
    root = Path(__file__).resolve().parents[1]
    s = load_settings(str(root / "config.yaml"))
    market = MarketDataClient(s.rest_base, Signer.from_settings(s))

    print(f"\n{B}STRATEGY SCOUT{X}  {D}{s.kalshi_env} exchange{X}")
    print(f"{D}  looking for: {', '.join(words)}{X}\n")

    try:
        markets = market.get_markets(status="open", max_pages=60)
    except KalshiError as exc:
        print(f"{R}Could not read the market list: {exc}{X}\n")
        return 1
    print(f"{D}  {len(markets)} open markets on the board{X}\n")

    hits: dict[str, list] = defaultdict(list)
    for m in markets:
        blob = f"{m.ticker} {m.title} {m.subtitle}".lower()
        if any(w in blob for w in words):
            hits[m.series_ticker or m.ticker.split("-")[0]].append(m)

    if not hits:
        print(f"{Y}  Nothing on this exchange matches any of those words.{X}")
        print(f"{D}  That is a real answer: there is nothing here to trade,")
        print(f"  and a strategy for it could not be tested.{X}\n")
        return 0

    print(f"{B}{'SERIES':<20}{'MARKETS':>8}{'QUOTED':>8}{'BOOKS':>7}"
          f"{'MEDIAN SPREAD':>15}   VERDICT{X}")
    print("-" * 96)

    verdicts = Counter()
    for series, ms in sorted(hits.items(), key=lambda kv: -len(kv[1])):
        quoted = [m for m in ms if m.yes_bid or m.yes_ask or m.no_bid or m.no_ask]
        spreads: list[int] = []
        with_book = 0
        for m in ms[:SAMPLE]:
            try:
                book = market.get_depth(m.ticker, depth=5)
            except Exception:            # noqa: BLE001
                continue
            bid = book.best_price("yes", "sell")
            ask = book.best_price("yes", "buy")
            if bid or ask:
                with_book += 1
            if bid and ask:
                spreads.append(int(ask) - int(bid))

        spreads.sort()
        median = spreads[len(spreads) // 2] if spreads else None
        if not quoted and not with_book:
            verdict, colour = "no book - nothing to trade", R
        elif not spreads:
            verdict, colour = "one-sided only", Y
        elif median > MAX_SPREAD:
            verdict, colour = f"too wide to cross (> {MAX_SPREAD}c)", Y
        else:
            verdict, colour = "TRADEABLE", G
        verdicts[verdict.split(" ")[0]] += 1

        print(f"{series:<20}{len(ms):>8}{len(quoted):>8}{with_book:>7}"
              f"{(str(median) + 'c') if median is not None else '-':>15}"
              f"   {colour}{verdict}{X}")
        print(f"{D}    e.g. {ms[0].ticker} | "
              f"{(ms[0].title or ms[0].subtitle)[:60]}{X}")

    print()
    if verdicts.get("TRADEABLE"):
        print(f"{G}  {verdicts['TRADEABLE']} series look tradeable.{X} "
              f"{D}Worth building for.{X}")
    else:
        print(f"{Y}  Nothing here is tradeable right now.{X}")
        print(f"{D}  A series that exists but nobody quotes is a strategy with")
        print(f"  nothing to trade - and on the demo board that is common,")
        print(f"  because its liquidity is synthetic.{X}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
