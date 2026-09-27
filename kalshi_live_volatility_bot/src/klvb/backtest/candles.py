"""Import real Kalshi 1-minute candlesticks (the JSON shape returned by
GET /series/{s}/markets/{t}/candlesticks) into a replay database.

Each candle becomes, at its end_period_ts:
  * one snapshot: YES bid/ask = the candle's bid/ask CLOSE (known at that instant),
    last = price close (or previous), cumulative volume, open interest;
  * up to two trade prints just before it, at the minute's traded HIGH and LOW,
    each carrying half the minute's volume. Resting (maker) paper orders can only
    fill from these real prints, and only if they were placed in an EARLIER minute.

Depth is not in candles. If the file carries captured order books, the median
displayed best-level size is used for that market; otherwise `assumed_depth`.
Snapshots are stored with source='kalshi_candles_1m' (real data, 1-minute fidelity).
"""
from __future__ import annotations

import glob
import json
import os
import statistics
from typing import Any

from ..data.parsing import classify_sport, count_field, dollars_to_cents, num, parse_orderbook
from ..models import MarketMeta, MarketSnapshot
from ..storage.db import Database
from .replay import meta_row, snapshot_row

SPORTS_SERIES_HINTS = ("GAME", "MATCH", "FIGHT", "TOTAL", "SPREAD", "MLB", "NFL", "NBA", "NHL", "NCAA", "WNBA",
                       "ATP", "WTA", "EPL", "MLS", "UFC", "UCL")


def _close(c: dict, name: str) -> float | None:
    o = c.get(name) or {}
    v = dollars_to_cents(o.get("close_dollars"))
    return v if v is not None else num(o.get("close"))


def _hl(c: dict, name: str) -> tuple[float | None, float | None]:
    o = c.get(name) or {}
    return dollars_to_cents(o.get("high_dollars")), dollars_to_cents(o.get("low_dollars"))


def _median_depth(books: list[dict]) -> float | None:
    sizes = []
    for b in books or []:
        ob = parse_orderbook(b.get("orderbook") or {}, 0)
        yb, ya = ob.best_yes_bid(), ob.best_yes_ask()
        if yb and ya:
            sizes.append(min(yb.size, ya.size))
    return statistics.median(sizes) if sizes else None


def import_candles(paths: list[str], db: Database, assumed_depth: float = 200.0,
                   series_filter: set[str] | None = None) -> dict[str, Any]:
    stats = {"files": 0, "markets": 0, "snapshots": 0, "prints": 0, "depth_from_books": 0}
    for path in paths:
        try:
            d = json.load(open(path))
        except (OSError, ValueError):
            continue
        stats["files"] += 1
        ticker = d.get("ticker") or os.path.basename(path).rsplit(".", 1)[0]
        series = d.get("series_ticker") or ticker.split("-")[0]
        if series_filter and series not in series_filter:
            continue
        candles = sorted(d.get("candlesticks") or [], key=lambda c: c.get("end_period_ts", 0))
        if len(candles) < 10:
            continue
        is_sport = any(h in series.upper() for h in SPORTS_SERIES_HINTS)
        category = "Sports" if is_sport else ""
        sport = classify_sport(series, "", [], "", "sports") if is_sport else "OTHER"
        depth = _median_depth(d.get("books") or [])
        if depth:
            stats["depth_from_books"] += 1
        depth = depth or assumed_depth
        event = ticker.rsplit("-", 1)[0] if ticker.count("-") >= 2 else ticker
        last_ts = float(candles[-1]["end_period_ts"])
        meta = MarketMeta(ticker=ticker, event_ticker=event, series_ticker=series, title=ticker, category=category,
                          sport=sport, close_ts=last_ts + 60, fee_type=None, fee_multiplier=None)
        db.upsert("markets", meta_row(meta, last_ts), key="ticker")
        cum = 0.0
        last_px = None
        snaps, prints = [], []
        for c in candles:
            ts = float(c["end_period_ts"])
            vol = count_field(c, "volume") or 0.0
            px = _close(c, "price")
            prev = dollars_to_cents((c.get("price") or {}).get("previous_dollars"))
            last_px = px if px is not None else (last_px if last_px is not None else prev)
            if vol > 0:
                hi, lo = _hl(c, "price")
                for i, p in enumerate({hi, lo} - {None}):
                    prints.append({"trade_id": f"{ticker}-{int(ts)}-{i}", "ticker": ticker, "ts": ts - 1 + 0.1 * i,
                                   "yes_price": p, "count": vol / (2 if hi != lo else 1), "taker_side": ""})
            cum += vol
            bid, ask = _close(c, "yes_bid"), _close(c, "yes_ask")
            bid = bid if bid is not None and bid > 0 else None
            ask = ask if ask is not None and ask < 100 else None
            if bid is None or ask is None or ask <= bid:
                continue
            snaps.append(snapshot_row(MarketSnapshot(
                ticker=ticker, ts=ts, yes_bid=bid, yes_ask=ask, last_price=last_px, volume=cum,
                volume_24h=None, open_interest=count_field(c, "open_interest"), yes_bid_size=depth,
                yes_ask_size=depth, status="active", source="kalshi_candles_1m")))
        db.insert_many("snapshots", snaps)
        db.insert_many("trade_prints", prints, ignore=True)
        stats["markets"] += 1
        stats["snapshots"] += len(snaps)
        stats["prints"] += len(prints)
    return stats


def import_candle_dir(directory: str, db: Database, **kw) -> dict[str, Any]:
    return import_candles(sorted(glob.glob(os.path.join(directory, "*.json"))), db, **kw)
