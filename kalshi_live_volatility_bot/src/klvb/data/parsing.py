"""Parse Kalshi REST/WS payloads into domain types.

Kalshi migrated from integer-cent fields (yes_bid, volume) to fixed-point strings
(yes_bid_dollars="0.4300", volume_fp="123.00"). We read the new fields first and
fall back to legacy ones so the bot keeps working through the transition.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from ..models import BookLevel, MarketMeta, MarketSnapshot, OrderBook, TradePrint


def dollars_to_cents(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return round(float(v) * 100.0, 4)
    except (TypeError, ValueError):
        return None


def num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def price_field(d: dict, name: str) -> float | None:
    """`name` like 'yes_bid' -> reads yes_bid_dollars, then legacy integer cents."""
    v = dollars_to_cents(d.get(f"{name}_dollars"))
    if v is None:
        v = num(d.get(name))
    return v


def count_field(d: dict, name: str) -> float | None:
    v = num(d.get(f"{name}_fp"))
    if v is None:
        v = num(d.get(name))
    return v


def iso_ts(v: Any) -> float | None:
    if not v:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def series_from_event(event_ticker: str) -> str:
    return event_ticker.split("-")[0] if event_ticker else ""


def parse_market_snapshot(m: dict, ts: float) -> MarketSnapshot:
    bid = price_field(m, "yes_bid")
    ask = price_field(m, "yes_ask")
    # Kalshi reports an empty side as 0 (bid) / 100 (ask); treat those as "no quote".
    if bid is not None and bid <= 0:
        bid = None
    if ask is not None and ask >= 100:
        ask = None
    last = price_field(m, "last_price")
    if last is not None and last <= 0:
        last = None
    return MarketSnapshot(
        ticker=m["ticker"], ts=ts, yes_bid=bid, yes_ask=ask, last_price=last,
        volume=count_field(m, "volume"), volume_24h=count_field(m, "volume_24h"),
        open_interest=count_field(m, "open_interest"),
        yes_bid_size=count_field(m, "yes_bid_size"), yes_ask_size=count_field(m, "yes_ask_size"),
        status=str(m.get("status", "")), source="kalshi_rest",
    )


def parse_market_meta(m: dict, series_info: dict[str, dict] | None = None,
                      sport_classifier=None) -> MarketMeta:
    event = m.get("event_ticker", "")
    series = m.get("series_ticker") or series_from_event(event)
    s = (series_info or {}).get(series, {})
    category = s.get("category") or m.get("category") or ""
    title = m.get("title", "")
    sport = "OTHER"
    if sport_classifier:
        sport = sport_classifier(series, s.get("title", ""), s.get("tags") or [], title, category)
    return MarketMeta(
        ticker=m["ticker"], event_ticker=event, series_ticker=series, title=title,
        category=category, sport=sport, market_type=m.get("market_type", "binary"),
        status=str(m.get("status", "")), close_ts=iso_ts(m.get("close_time")),
        expected_end_ts=iso_ts(m.get("expected_expiration_time")),
        fee_type=s.get("fee_type"), fee_multiplier=num(s.get("fee_multiplier")),
        fee_waiver_until=iso_ts(m.get("fee_waiver_expiration_time")),
        tick_size=float(m.get("tick_size") or 1),
        is_multivariate=bool(m.get("mve_collection_ticker")),
    )


def _levels(raw: Any, dollars: bool) -> list[BookLevel]:
    out: list[BookLevel] = []
    for row in raw or []:
        try:
            p, c = row[0], row[1]
        except (TypeError, IndexError):
            continue
        price = dollars_to_cents(p) if dollars else num(p)
        size = num(c)
        if price is None or size is None or size <= 0:
            continue
        out.append(BookLevel(price, size))
    out.sort(key=lambda lv: lv.price, reverse=True)  # best bid first
    return out


def parse_orderbook(resp: dict, ts: float) -> OrderBook:
    ob = resp.get("orderbook_fp") or resp.get("orderbook") or resp
    if "yes_dollars" in ob or "no_dollars" in ob:
        yes = _levels(ob.get("yes_dollars"), True)
        no = _levels(ob.get("no_dollars"), True)
    else:
        yes = _levels(ob.get("yes"), False)
        no = _levels(ob.get("no"), False)
    return OrderBook(ts=ts, yes_bids=yes, no_bids=no)


def parse_trade(t: dict) -> TradePrint | None:
    price = price_field(t, "yes_price")
    count = count_field(t, "count")
    ts = iso_ts(t.get("created_time"))
    if price is None or count is None or ts is None:
        return None
    return TradePrint(ticker=t.get("ticker", ""), ts=ts, yes_price=price, count=count,
                      taker_side=t.get("taker_side", ""), trade_id=t.get("trade_id", ""))


SPORT_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("TENNIS", ("tennis", "atp", "wta", "wimbledon", "us open tennis", "roland", "australian open", "itf", "challenger")),
    ("BASKETBALL", ("basketball", "nba", "wnba", "ncaab", "ncaamb", "ncaawb", "euroleague", "march madness")),
    ("FOOTBALL", ("football", "nfl", "ncaaf", "college football", "super bowl", "cfb")),
    ("BASEBALL", ("baseball", "mlb", "world series", "npb", "kbo")),
    ("SOCCER", ("soccer", "premier league", "epl", "la liga", "laliga", "bundesliga", "serie a", "ligue 1",
                "mls", "champions league", "ucl", "uefa", "fifa", "world cup")),
    ("HOCKEY", ("hockey", "nhl")),
]


_SPORT_PATTERNS = [(sport, re.compile(r"\b(" + "|".join(re.escape(k) for k in keys) + r")\b"))
                   for sport, keys in SPORT_KEYWORDS]


def classify_sport(series: str, series_title: str, tags: list[str], title: str, category: str) -> str:
    # Kalshi series tickers look like KXNBAGAME / KXATPMATCH: strip the KX prefix and
    # split the remaining ticker so "NBA" / "ATP" match as whole words.
    if category and category.lower() != "sports":
        return "OTHER"
    ser = series.upper()
    ser = ser[2:] if ser.startswith("KX") else ser
    # Only trust ticker fragments inside the Sports category ("KXCPIINFL" contains "NFL").
    ser_words = "" if category.lower() != "sports" else " ".join(re.findall(r"NCAAMB|NCAAWB|NCAAF|NCAAB|WNBA|NBA|NFL|MLB|NHL|ATP|WTA|EPL|MLS|UCL", ser))
    hay = " ".join([ser_words, series_title, " ".join(tags), title]).lower()
    for sport, pat in _SPORT_PATTERNS:
        if pat.search(hay):
            return sport
    if category.lower() == "sports":
        return "OTHER_SPORT"
    return "OTHER"
