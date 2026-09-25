"""What would this order actually have cost, on a book that is real?

The one question a demo exchange cannot answer. Demo liquidity is synthetic,
so every execution number this bot has measured - spread, depth, slippage,
whether 25 contracts move the price - describes a fiction. The forecasting
edge is measured and, in the traded band, now looks real. The cost of taking
it is not measured at all, and the difference between the two is the whole
question of whether any of this makes money.

This compares the SAME market at the SAME instant on both books:

    demo  the authenticated book the bot actually trades against
    live  the public production book, read without any credential

and walks a realistic order size down each, recording what it would pay.

It is a measurement, not a strategy. Nothing it computes is returned to the
engine, nothing it reads can influence a trading decision, and it places no
orders. The live book reaches this module through LiveBookReader, which has
no signer and no method that can send one.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _side_view(book, side: str, action: str, size: int) -> dict:
    """Top of book, depth there, and what `size` would actually pay."""
    best = book.best_price(side, action)
    if best is None:
        return {"best": None, "depth_at_best": 0, "vwap": None,
                "worst": None, "filled": 0, "complete": False}
    ladder = book.ladder(side, action)
    walk = book.walk(side, action, size)
    return {
        "best": best,
        "depth_at_best": ladder[0].contracts if ladder else 0,
        "vwap": walk.vwap_cents if walk.filled else None,
        "worst": walk.worst_price_cents if walk.filled else None,
        "filled": walk.filled,
        "complete": bool(walk.complete),
    }


def compare(ticker: str, demo_book, live_book, size: int = 25,
            side: str = "yes", action: str = "buy") -> dict | None:
    """One row of evidence about how different the real book is.

    Returns None when either book is missing - a comparison needs both, and a
    half-comparison recorded as a whole one is how a measurement starts lying.
    """
    if demo_book is None or live_book is None:
        return None

    d = _side_view(demo_book, side, action, size)
    v = _side_view(live_book, side, action, size)
    if d["best"] is None or v["best"] is None:
        return None

    def spread(book):
        bid = book.best_price(side, "sell")
        ask = book.best_price(side, "buy")
        return (ask - bid) if (bid is not None and ask is not None) else None

    # Slippage is what the depth costs beyond the headline price. It is the
    # number the bot has never been able to see: on a synthetic book the top
    # rung is usually deep enough that slippage reads zero, which is exactly
    # the illusion being measured here.
    d_slip = (d["vwap"] - d["best"]) if d["vwap"] is not None else None
    v_slip = (v["vwap"] - v["best"]) if v["vwap"] is not None else None

    return {
        "ticker": ticker, "side": side, "action": action, "size": size,
        "demo_best_cents": d["best"], "demo_spread_cents": spread(demo_book),
        "demo_depth_at_best": d["depth_at_best"], "demo_vwap_cents": d["vwap"],
        "demo_filled": d["filled"], "demo_complete": d["complete"],
        "demo_slippage_cents": d_slip,
        "live_best_cents": v["best"], "live_spread_cents": spread(live_book),
        "live_depth_at_best": v["depth_at_best"], "live_vwap_cents": v["vwap"],
        "live_filled": v["filled"], "live_complete": v["complete"],
        "live_slippage_cents": v_slip,
        # The two numbers the fortnight exists to produce.
        "cost_gap_cents": (v["vwap"] - d["vwap"])
                          if (v["vwap"] is not None and d["vwap"] is not None)
                          else None,
        "slippage_gap_cents": (v_slip - d_slip)
                              if (v_slip is not None and d_slip is not None)
                              else None,
    }


def summarise(rows: list[dict]) -> dict:
    """What the collected rows say, in the terms the decision needs."""
    if not rows:
        return {"n": 0}
    full = [r for r in rows if r.get("live_vwap_cents") is not None
            and r.get("demo_vwap_cents") is not None]
    def mean(vals):
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None
    live_incomplete = [r for r in rows if not r.get("live_complete")]
    return {
        "n": len(rows),
        "comparable": len(full),
        "demo_spread": mean(r["demo_spread_cents"] for r in rows),
        "live_spread": mean(r["live_spread_cents"] for r in rows),
        "demo_slippage": mean(r["demo_slippage_cents"] for r in full),
        "live_slippage": mean(r["live_slippage_cents"] for r in full),
        "cost_gap": mean(r["cost_gap_cents"] for r in full),
        # The one that decides whether a 25-lot is even executable.
        "live_could_not_fill": len(live_incomplete),
        "live_fill_rate": 1 - (len(live_incomplete) / len(rows)),
    }
