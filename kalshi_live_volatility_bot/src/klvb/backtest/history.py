"""Build a replay dataset from Kalshi's PUBLIC historical endpoints (no credentials).

For settled/closed markets in the chosen series we download:
  * 1-minute candlesticks -> real YES bid/ask closes (spread) and volume per minute
  * individual trade prints -> intra-minute price and volume

and write them as snapshots/trade_prints into a replay database. To stay free of
look-ahead, a candle's bid/ask becomes visible only at its end_period_ts; trade
prints between candles update last price/volume but keep the last *known* quote.

FIDELITY WARNING: historical depth is not available, so snapshot sizes are an
ASSUMPTION (`assumed_depth`). Results from this data are lower fidelity than
recorded live PAPER data and are labeled source='history_candles'.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from ..data.parsing import classify_sport, count_field, dollars_to_cents, iso_ts, num, parse_market_meta, parse_trade
from ..models import MarketSnapshot
from ..storage.db import Database
from .replay import meta_row, snapshot_row

log = logging.getLogger("klvb.history")


def _ohlc_close(c: dict, name: str) -> float | None:
    o = c.get(name) or {}
    v = dollars_to_cents(o.get("close_dollars"))
    if v is None:
        v = num(o.get("close"))
    return v


async def fetch_history(provider, db: Database, series: list[str], days: float = 7.0, max_markets: int = 200,
                        assumed_depth: float = 200.0, min_volume: float = 1000.0) -> dict[str, Any]:
    client = provider.client
    await provider._load_series()
    now = time.time()
    stats = {"markets": 0, "snapshots": 0, "trades": 0, "skipped_low_volume": 0}
    for s in series:
        cursor = None
        markets: list[dict] = []
        for status in ("settled", "closed"):
            cursor = None
            for _ in range(10):
                params: dict[str, Any] = {"series_ticker": s, "status": status, "limit": 200,
                                          "min_close_ts": int(now - days * 86400)}
                if cursor:
                    params["cursor"] = cursor
                data = await client.get("/markets", params)
                markets += data.get("markets") or []
                cursor = data.get("cursor")
                if not cursor or len(markets) >= max_markets:
                    break
        for m in markets[:max_markets]:
            vol = count_field(m, "volume") or 0
            if vol < min_volume:
                stats["skipped_low_volume"] += 1
                continue
            meta = parse_market_meta(m, provider.series_info, classify_sport)
            open_ts = iso_ts(m.get("open_time")) or now - days * 86400
            close_ts = iso_ts(m.get("close_time")) or now
            start = int(max(open_ts, now - days * 86400))
            end = int(min(close_ts, now))
            try:
                cd = await client.get(f"/series/{s}/markets/{meta.ticker}/candlesticks",
                                      {"start_ts": start, "end_ts": end, "period_interval": 1})
            except Exception as e:  # noqa: BLE001
                log.warning("candles failed for %s: %s", meta.ticker, e)
                continue
            candles = cd.get("candlesticks") or []
            trades = []
            tcur = None
            for _ in range(50):
                p: dict[str, Any] = {"ticker": meta.ticker, "min_ts": start, "max_ts": end, "limit": 1000}
                if tcur:
                    p["cursor"] = tcur
                td = await client.get("/markets/trades", p)
                trades += [t for t in (parse_trade(x) for x in td.get("trades") or []) if t]
                tcur = td.get("cursor")
                if not tcur:
                    break
            trades.sort(key=lambda t: t.ts)
            meta.close_ts = close_ts
            db.upsert("markets", meta_row(meta, now), key="ticker")
            events: list[tuple[float, str, Any]] = [(float(c.get("end_period_ts", 0)), "c", c) for c in candles]
            events += [(t.ts, "t", t) for t in trades]
            events.sort(key=lambda e: (e[0], e[1]))
            bid = ask = last = None
            cum_vol = 0.0
            rows = []
            for ts, kind, obj in events:
                if kind == "c":
                    b, a = _ohlc_close(obj, "yes_bid"), _ohlc_close(obj, "yes_ask")
                    bid = b if b and b > 0 else bid
                    ask = a if a and a < 100 else ask
                    last = _ohlc_close(obj, "price") or last
                else:
                    last = obj.yes_price
                    cum_vol += obj.count
                if bid is None or ask is None or ask <= bid:
                    continue
                rows.append(snapshot_row(MarketSnapshot(
                    ticker=meta.ticker, ts=ts, yes_bid=bid, yes_ask=ask, last_price=last, volume=cum_vol,
                    volume_24h=vol, open_interest=count_field(m, "open_interest"), yes_bid_size=assumed_depth,
                    yes_ask_size=assumed_depth, status="active", source="history_candles")))
            db.insert_many("snapshots", rows)
            db.insert_many("trade_prints", [{"trade_id": t.trade_id or f"{meta.ticker}-{t.ts}-{t.count}",
                                             "ticker": meta.ticker, "ts": t.ts, "yes_price": t.yes_price,
                                             "count": t.count, "taker_side": t.taker_side} for t in trades],
                           ignore=True)
            stats["markets"] += 1
            stats["snapshots"] += len(rows)
            stats["trades"] += len(trades)
    return stats
