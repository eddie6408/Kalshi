"""Kalshi public REST market data (production exchange, no credentials).

Endpoints used (all public):
  GET /series                       category + fee schedule per series
  GET /markets?status=open          discovery sweep (paged, 1000/page)
  GET /markets?tickers=a,b,c        batch top-of-book + volume (hot/probe tiers)
  GET /markets/{t}/orderbook        depth ladders (fixed-point dollars)
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

from lvt.data.http import PublicHttp
from lvt.data.provider import MarketDataProvider
from lvt.models import Depth, MarketInfo, Quote, dollars_to_cents, fnum

log = logging.getLogger("lvt.data.kalshi")


def parse_ts(v) -> float:
    if not v:
        return 0.0
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _price(m: dict, key: str) -> float | None:
    if f"{key}_dollars" in m:
        return dollars_to_cents(m.get(f"{key}_dollars"))
    v = m.get(key)                    # legacy integer cents
    return None if v is None else float(v)


def parse_quote(m: dict, ts: float) -> Quote:
    bid = _price(m, "yes_bid")
    ask = _price(m, "yes_ask")
    if bid is not None and bid <= 0:
        bid = None
    if ask is not None and ask >= 100:
        ask = None
    last = _price(m, "last_price")
    return Quote(
        ticker=m["ticker"], ts=ts, yes_bid=bid, yes_ask=ask,
        yes_bid_size=fnum(m.get("yes_bid_size_fp", m.get("yes_bid_size"))),
        yes_ask_size=fnum(m.get("yes_ask_size_fp", m.get("yes_ask_size"))),
        last=last if last else None,
        volume=fnum(m.get("volume_fp", m.get("volume"))),
        open_interest=fnum(m.get("open_interest_fp", m.get("open_interest"))),
        status=str(m.get("status") or ""),
        result=str(m.get("result") or ""),
    )


def parse_market(m: dict) -> MarketInfo:
    event = m.get("event_ticker") or ""
    series = m.get("series_ticker") or event.split("-")[0]
    return MarketInfo(
        ticker=m["ticker"], event_ticker=event, series_ticker=series,
        title=m.get("title") or "", sub_title=m.get("yes_sub_title") or m.get("subtitle") or "",
        close_ts=parse_ts(m.get("close_time")),
        occurrence_ts=parse_ts(m.get("occurrence_datetime") or m.get("expected_expiration_time")),
        status=str(m.get("status") or ""),
        volume_24h=fnum(m.get("volume_24h_fp", m.get("volume_24h"))),
        can_close_early=bool(m.get("can_close_early", True)),
        result=str(m.get("result") or ""),
    )


def parse_ladder(rows) -> list[tuple[float, float]]:
    """Kalshi returns each side's bids ascending; return best (highest) first."""
    out: list[tuple[float, float]] = []
    for r in rows or []:
        try:
            p, s = r[0], r[1]
        except (TypeError, IndexError):
            continue
        price = dollars_to_cents(p) if isinstance(p, str) else float(p)
        size = fnum(s)
        if price is not None and size > 0:
            out.append((price, size))
    out.sort(key=lambda x: -x[0])
    return out


def parse_depth(ticker: str, d: dict, ts: float) -> Depth:
    ob = d.get("orderbook_fp")
    if ob is not None:
        return Depth(ticker, ts, parse_ladder(ob.get("yes_dollars")), parse_ladder(ob.get("no_dollars")))
    ob = d.get("orderbook") or {}
    return Depth(ticker, ts, parse_ladder(ob.get("yes_dollars") or ob.get("yes")),
                 parse_ladder(ob.get("no_dollars") or ob.get("no")))


class KalshiPublicData(MarketDataProvider):
    name = "kalshi_rest"

    def __init__(self, http: PublicHttp, batch_size: int = 50):
        self.http = http
        self.batch_size = batch_size

    def healthy(self) -> bool:
        return time.time() >= self.http._blocked_until - 30

    def series_catalog(self) -> dict[str, dict]:
        d = self.http.get("/series")
        out = {}
        for s in d.get("series") or []:
            out[s["ticker"]] = {
                "category": s.get("category") or "",
                "fee_type": s.get("fee_type") or "quadratic",
                "fee_multiplier": fnum(s.get("fee_multiplier"), 1.0) or 1.0,
                "title": s.get("title") or "",
            }
        return out

    def list_open_markets(self, pause_s: float = 0.0, series_ticker: str | None = None) -> list[MarketInfo]:
        params = {"status": "open", "limit": 1000, "mve_filter": "exclude"}
        if series_ticker:
            params["series_ticker"] = series_ticker
        out: list[MarketInfo] = []
        cursor = None
        for _ in range(400):
            if cursor:
                params["cursor"] = cursor
            d = self.http.get("/markets", params=params)
            out.extend(parse_market(m) for m in d.get("markets") or [])
            cursor = d.get("cursor")
            if not cursor:
                break
            if pause_s:
                time.sleep(pause_s)
        return out

    def quotes(self, tickers: list[str]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        for i in range(0, len(tickers), self.batch_size):
            chunk = tickers[i:i + self.batch_size]
            d = self.http.get("/markets", params={"tickers": ",".join(chunk), "limit": len(chunk)})
            ts = time.time()
            for m in d.get("markets") or []:
                out[m["ticker"]] = parse_quote(m, ts)
        return out

    def depth(self, ticker: str, levels: int) -> Depth:
        d = self.http.get(f"/markets/{ticker}/orderbook", params={"depth": levels})
        return parse_depth(ticker, d, time.time())

    def market(self, ticker: str) -> dict:
        return self.http.get(f"/markets/{ticker}").get("market") or {}
