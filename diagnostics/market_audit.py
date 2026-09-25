#!/usr/bin/env python3
"""Why is the bot not looking at market X?

Answers that question with data instead of guesses. For every series on the
exchange it reports how many markets exist, how many the LIST endpoint claims
are quoted, and how many actually have an order book when you go and fetch it.

Those last two numbers are often very different, and the gap is the single
most common reason a scanner appears to ignore most of the exchange.

    python diagnostics/market_audit.py              # all series
    python diagnostics/market_audit.py KXHIGH       # just weather highs
    python diagnostics/market_audit.py --books 200  # fetch more order books
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.auth import Signer                       # noqa: E402
from kalshibot.clients import MarketDataClient          # noqa: E402
from kalshibot.config import load_settings              # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
B, D, G, R, Y, X = ("\033[1m", "\033[2m", "\033[32m", "\033[31m",
                    "\033[33m", "\033[0m")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("filter", nargs="?", default="",
                    help="only series whose ticker starts with this")
    ap.add_argument("--books", type=int, default=600,
                    help="how many order books to actually fetch")
    ap.add_argument("--per-series", type=int, default=6,
                    help="max books to sample from any one series")
    ap.add_argument("--top", type=int, default=0,
                    help="only audit the N largest series (0 = all)")
    ap.add_argument("--pages", type=int, default=400)
    ap.add_argument("--no-save", action="store_true",
                    help="do not record findings into data/liquid_series.json")
    ap.add_argument("--raw", action="store_true",
                    help="print the raw JSON of one order book, exactly as "
                         "Kalshi sent it")
    args = ap.parse_args()

    s = load_settings(str(ROOT / "config.yaml"))
    client = MarketDataClient(s.rest_base, Signer.from_settings(s))

    print(f"\n{B}MARKET AUDIT{X}  {D}{s.rest_base}{X}\n")
    markets = client.get_markets(status="open", max_pages=args.pages)
    traded = [m for m in markets if m.volume or m.open_interest]
    print(f"  {len(markets)} open markets returned by the list endpoint")
    pct = 100.0 * len(traded) / max(1, len(markets))
    col = G if traded else R
    print(f"  {col}{len(traded)} ({pct:.0f}%) have traded volume or open "
          f"interest{X}")
    if not traded:
        print(f"  {R}The list endpoint reports no volume or open interest "
              f"either, so there{X}")
        print(f"  {R}is no cheap way to pre-filter. Every order book must be "
              f"fetched blind.{X}")
    else:
        top = sorted(traded, key=lambda m: -(m.volume + m.open_interest))[:5]
        print(f"{D}  busiest markets on the board:{X}")
        for m in top:
            print(f"{D}    {m.ticker[:44]:<46} vol {m.volume:>7}  "
                  f"OI {m.open_interest:>7}{X}")
    if len(markets) >= args.pages * 200:
        print(f"  {Y}(that is exactly the page cap - there is more exchange "
              f"beyond it){X}")
    print()

    # ---- what shape does THIS deployment actually send? ----------------
    # Probe several markets, not one. The first market on the board is often
    # an illiquid one, and an empty book from it says nothing about the
    # response FORMAT.
    from kalshibot.depth import DepthBook
    candidates = [m for m in markets if not args.filter
                  or (m.series_ticker or m.ticker).upper().startswith(
                      args.filter.upper())][:400]
    probe, raw, parsed = None, None, None
    for m in candidates[::max(1, len(candidates) // 25)][:25]:
        try:
            r = client._c._request("GET", f"/markets/{m.ticker}/orderbook",
                                   params={"depth": 10},
                                   authed=client._c.signer is not None,
                                   public_ok=True)
        except Exception:                               # noqa: BLE001
            continue
        pb = DepthBook.from_api(m.ticker, r)
        if probe is None:
            probe, raw, parsed = m, r, pb
        if pb.yes_bids or pb.no_bids:                   # found a live one
            probe, raw, parsed = m, r, pb
            break
    if probe is not None:
        raw = client._c._request(
            "GET", f"/markets/{probe.ticker}/orderbook",
            params={"depth": 10}, authed=client._c.signer is not None,
            public_ok=True)
        top = sorted(raw.keys())
        inner = []
        for k in top:
            if isinstance(raw[k], dict):
                inner += list(raw[k].keys())
        fmt_colour = R if parsed.source_format == "unrecognised" else G
        print(f"{B}  ORDER BOOK WIRE FORMAT{X}  {D}(sampled from "
              f"{probe.ticker}){X}")
        print(f"    top-level keys   : {top}")
        print(f"    inner keys       : {sorted(set(inner))}")
        print(f"    parsed as        : {fmt_colour}{parsed.source_format}{X}")
        print(f"    yes bid levels   : {len(parsed.yes_bids)}    "
              f"no bid levels: {len(parsed.no_bids)}")
        if parsed.source_format == "unrecognised":
            print(f"    {R}The parser did not recognise this shape. "
                  f"Send me the keys above.{X}")
        elif parsed.source_format.endswith("-empty"):
            print(f"    {Y}Shape understood; this particular market just has "
                  f"no resting orders.{X}")
            print(f"    {D}That is a quiet market, not a parsing problem.{X}")
        else:
            print(f"    {G}Parsing correctly against a live book.{X}")
        if args.raw:
            import json
            print(f"\n{D}{json.dumps(raw, indent=2)[:1500]}{X}")
        print()

    by_series = defaultdict(list)
    for m in markets:
        series = m.series_ticker or m.ticker.split("-")[0]
        if args.filter and not series.upper().startswith(args.filter.upper()):
            continue
        by_series[series].append(m)

    if not by_series:
        print(f"{R}  Nothing matched {args.filter!r}.{X}\n")
        return 1

    # Sample order books across series so one big series cannot eat the budget.
    ranked = sorted(by_series.items(), key=lambda kv: -len(kv[1]))
    if args.top:
        ranked = ranked[:args.top]
        by_series = dict(ranked)
    budget = args.books
    # Sample proportionally to series size, capped, so a 2,800-market series
    # is not judged on a single order book.
    total_markets = sum(len(v) for v in by_series.values()) or 1
    print(f"{B}  {'series':<24}{'mkts':>6}{'list says':>11}{'books':>7}"
          f"{'real depth':>12}   verdict{X}")
    print(f"  {'-' * 82}")

    totals = {"markets": 0, "list_quoted": 0, "fetched": 0, "depth": 0}
    problem_series: list[str] = []
    liquid_series: list[tuple[str, float, int]] = []
    sampled: dict[str, tuple[int, int]] = {}

    for series, ms in ranked:
        if budget <= 0:
            break
        share = max(1, int(round(args.books * len(ms) / total_markets)))
        n = min(args.per_series, share, len(ms), budget)
        list_quoted = sum(1 for m in ms if m.yes_bid or m.yes_ask)
        # spread the sample across the series rather than taking the first n
        step = max(1, len(ms) // n)
        sample = ms[::step][:n]
        budget -= len(sample)
        fetched = depth = 0
        for m in sample:
            try:
                bk = client.get_depth(m.ticker)
            except Exception:                       # noqa: BLE001
                continue
            fetched += 1
            if bk.best_price("yes", "buy") or bk.best_price("no", "buy"):
                depth += 1

        sampled[series] = (fetched, depth)
        totals["markets"] += len(ms)
        totals["list_quoted"] += list_quoted
        totals["fetched"] += fetched
        totals["depth"] += depth

        if fetched == 0:
            verdict, colour = "no books sampled", D
        elif depth == 0:
            verdict, colour = "quiet", D
        else:
            rate = depth / fetched
            liquid_series.append((series, rate, len(ms)))
            if rate >= 0.5:
                verdict, colour = f"TRADABLE  {rate:.0%} quoted", G
            else:
                verdict, colour = f"partly quoted  {rate:.0%}", Y
            if list_quoted == 0:
                problem_series.append(series)

        print(f"  {series[:22]:<24}{len(ms):>6}{list_quoted:>11}{fetched:>7}"
              f"{depth:>12}   {colour}{verdict}{X}")

    print(f"  {'-' * 82}")
    print(f"  {'TOTAL':<24}{totals['markets']:>6}{totals['list_quoted']:>11}"
          f"{totals['fetched']:>7}{totals['depth']:>12}")

    print(f"\n{B}  WHAT THIS MEANS{X}")
    if problem_series:
        print(f"  {Y}The list endpoint reports 0/0 for bid/ask on "
              f"{len(problem_series)} series{X}")
        print(f"  {Y}that DO have order books. Any scanner filtering on the "
              f"list response{X}")
        print(f"  {Y}would silently skip all of them.{X}")
        print(f"  {D}Affected: {', '.join(problem_series[:8])}"
              f"{' ...' if len(problem_series) > 8 else ''}{X}")
        print(f"  {G}config.yaml -> discovery.require_quotes is already false, "
              f"so the bot{X}")
        print(f"  {G}judges liquidity from the real order book. Nothing is "
              f"being skipped.{X}")
    elif totals["depth"] == 0:
        print(f"  {R}Not one sampled market has an order book.{X}")
        print(f"  {D}The demo exchange often has no resting liquidity at all. "
              f"There is{X}")
        print(f"  {D}genuinely nothing to trade - that is the venue, not the "
              f"bot.{X}")
    else:
        pct = 100.0 * totals["depth"] / max(1, totals["fetched"])
        print(f"  {G}{pct:.0f}% of sampled markets have a real order book.{X}")

    # Hand the findings to the bot so the next scan starts informed instead
    # of blind. This is the whole point of running the audit.
    if not args.no_save:
        from kalshibot.liquidity import LiquidityProfile
        prof = LiquidityProfile(ROOT / "data" / "liquid_series.json")
        for series, ms in ranked:
            checked = sampled.get(series, (0, 0))
            if checked[0]:
                prof.record(series, checked[0], checked[1])
        prof.save()
        found = len(prof.known_liquid())
        print(f"\n{G}  Recorded into data/liquid_series.json - the next scan "
              f"will prioritise{X}")
        print(f"{G}  the {found} series with confirmed order books.{X}")

    if liquid_series:
        liquid_series.sort(key=lambda t: (-t[1], -t[2]))
        print(f"\n{B}  WHERE THE LIQUIDITY IS{X}")
        for series, rate, n in liquid_series[:15]:
            print(f"    {G}{series:<24}{X}{rate:>6.0%} quoted   {n:>5} markets")
        print(f"\n{D}  The bot now learns this on its own and spends its order")
        print(f"  book budget here first (data/liquid_series.json).{X}")
        print(f"\n{D}  To scan ONLY these, put this in config.yaml under "
              f"discovery:{X}")
        names = [s for s, _, _ in liquid_series[:12]]
        print(f"    series_filter: [{', '.join(repr(n) for n in names)}]")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
