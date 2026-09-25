#!/usr/bin/env python3
"""Does the bot's fill record agree with what Kalshi says we hold?

    python diagnostics/reconcile.py            # report only
    python diagnostics/reconcile.py --book     # also close out settled markets

This exists because of what the 19-20 September fills turned out to contain.
Five sell orders were sent against positions that were already closed. On
Kalshi such an order does not fail and does not do nothing - it OPENS the
opposite position. Four of those accidental shorts were still open days later,
on markets that had already settled.

The clamp in position_manager stops new ones from being created. This finds
the ones that already exist, and it never places an order to fix a ledger: the
worst way to resolve a disagreement about what we hold is to trade on it.

Two sources, neither of them authoritative alone:

    the exchange   authoritative about what exists, stale about what just
                   changed - /portfolio/positions lags a fill by seconds
    our fills      current to the millisecond, blind to anything done by hand

So every row is one of five verdicts, and only the last two need a human.
"""
from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.auth import Signer                          # noqa: E402
from kalshibot.client import KalshiError                   # noqa: E402
from kalshibot.clients import MarketDataClient, PortfolioClient   # noqa: E402
from kalshibot.config import load_settings                 # noqa: E402
from kalshibot.settlement import resolution_of             # noqa: E402

B = "\033[1m"; D = "\033[2m"; G = "\033[32m"; R = "\033[31m"
Y = "\033[33m"; C = "\033[36m"; X = "\033[0m"


def held_from_fills(conn) -> dict[tuple[str, str], int]:
    """Net contracts per (ticker, side) from the fills we actually made."""
    net: dict[tuple[str, str], int] = defaultdict(int)
    for r in conn.execute(
            "SELECT ticker, side, action, filled FROM leg_orders "
            "WHERE filled > 0 ORDER BY id"):
        n = int(r["filled"] or 0)
        net[(r["ticker"], r["side"])] += n if r["action"] == "buy" else -n
    return dict(net)


def main() -> int:
    book_them = "--book" in sys.argv[1:]
    root = Path(__file__).resolve().parents[1]
    s = load_settings(str(root / "config.yaml"))

    db = s.path("journal.database_path", "data/journal.db")
    if not db.exists():
        print("No journal yet - nothing to reconcile.")
        return 0

    signer = Signer.from_settings(s)
    portfolio = PortfolioClient(s.rest_base, signer)
    market = MarketDataClient(s.rest_base, signer)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    ours = held_from_fills(conn)

    print(f"\n{B}LEDGER RECONCILIATION{X}  {D}{s.kalshi_env} exchange{X}\n")

    try:
        positions = [p for p in portfolio.positions() if p.contracts]
    except KalshiError as exc:
        print(f"  {R}could not read positions: {exc}{X}")
        return 1

    # The exchange reports one signed number per ticker. Ours is per side.
    exchange: dict[str, int] = {p.ticker: p.contracts for p in positions}
    tickers = sorted(set(exchange) | {t for t, _ in ours})

    counts: dict[str, int] = defaultdict(int)
    needs_you: list[tuple[str, str]] = []

    for ticker in tickers:
        signed = exchange.get(ticker, 0)
        ex_side = "yes" if signed > 0 else ("no" if signed < 0 else "flat")
        ex_qty = abs(signed)
        our_yes = ours.get((ticker, "yes"), 0)
        our_no = ours.get((ticker, "no"), 0)

        # A NEGATIVE here is the whole reason this tool exists: it means our
        # own record contains more sells than buys on that side, which is only
        # possible if a sell was sent with no position behind it. Clamping it
        # to zero would hide the four positions this was written to find.
        oversold = min(our_yes, our_no, 0)
        our_side = "yes" if our_yes > 0 else ("no" if our_no > 0 else "flat")
        our_qty = max(our_yes, our_no, 0)

        if oversold < 0:
            over_side = "yes" if our_yes < 0 else "no"
            label, colour = "OVERSOLD", R
            note = (f"our own fills show {abs(oversold)} more {over_side.upper()} "
                    f"sold than bought - a sell was sent with nothing behind "
                    f"it, and on Kalshi that opens the opposite position")
        elif ex_side == our_side and ex_qty == our_qty:
            counts["agree"] += 1
            continue
        else:
            label, colour, note = _verdict(ex_side, ex_qty, our_side, our_qty)
            if label == "AGREE":
                counts["agree"] += 1
                continue

        counts[label] += 1
        settled = _settlement(market, ticker)

        # A market that has already settled explains most disagreements by
        # itself: the exchange paid the position out and closed it. Sending
        # that to a human as an action item is how a report gets ignored.
        if label == "PHANTOM" and settled is not None:
            label, colour = "SETTLED", D
            note = (f"resolved {'YES' if settled else 'NO'} and paid out - our "
                    f"record still lists it, which is bookkeeping, not risk")
            counts["PHANTOM"] -= 1
            counts["SETTLED"] += 1
        elif label in ("OVERSOLD", "ORPHAN SHORT", "PHANTOM"):
            needs_you.append((ticker, note))

        print(f"  {colour}{label:<13}{X} {ticker}")
        print(f"    {D}exchange:{X} {ex_side.upper():<4} x{ex_qty:<4} "
              f"{D}our fills:{X} {our_side.upper():<4} x{our_qty}"
              + (f"  {R}(and {abs(oversold)} oversold){X}" if oversold else ""))
        print(f"    {note}")
        if settled is not None and label != "SETTLED":
            print(f"    {D}this market has settled {'YES' if settled else 'NO'}"
                  f" - there is nothing to sell{X}")
        print()

    print(f"  {G}{counts['agree']} agree{X}   "
          f"{D}{counts['SETTLED']} settled   {counts['STALE']} exchange-lag   "
          f"{counts['UNTRACKED']} untracked{X}   "
          f"{R}{counts['OVERSOLD']} oversold   "
          f"{counts['ORPHAN SHORT']} orphan shorts   "
          f"{counts['PHANTOM']} phantom{X}\n")

    if needs_you:
        print(f"  {B}These need you, not the bot:{X}")
        for ticker, note in needs_you:
            print(f"    - {ticker}: {note}")
        print(f"\n  {D}The bot will not trade its way out of a ledger "
              f"disagreement. Close these\n  by hand on the Kalshi site, or "
              f"let them settle if the market already has.{X}\n")
    return 0


def _verdict(ex_side, ex_qty, our_side, our_qty):
    """Name the disagreement. The name is the whole point of this tool."""
    if ex_side == our_side and ex_qty == our_qty:
        return "AGREE", G, ""
    if our_qty == 0 and ex_qty > 0:
        if our_side == "flat":
            return ("UNTRACKED", C,
                    "the exchange holds this and our fills have never seen it "
                    "- opened by hand, or from before this journal")
    if ex_qty == 0 and our_qty > 0:
        return ("PHANTOM", R,
                "our fills think we hold this and the exchange says we do not "
                "- a sell that filled without being recorded, or a settlement")
    if ex_side != our_side and ex_qty and our_qty:
        return ("ORPHAN SHORT", R,
                f"the exchange has us {ex_side.upper()} while our fills say "
                f"{our_side.upper()} - this is the signature of a sell sent "
                f"with no position behind it")
    if ex_side == our_side and ex_qty != our_qty:
        if ex_qty > our_qty:
            return ("STALE", D,
                    f"the exchange is {ex_qty - our_qty} contracts behind our "
                    f"fills - normal for a few seconds after a trade")
        return ("PHANTOM", R,
                f"our fills claim {our_qty - ex_qty} contracts the exchange "
                f"does not have")
    return ("UNTRACKED", C, "cannot classify - inspect by hand")


def _settlement(market, ticker):
    try:
        m = market.get_market(ticker)
    except Exception:       # noqa: BLE001
        return None
    if m is None:
        return None
    return resolution_of(m)


if __name__ == "__main__":
    raise SystemExit(main())
