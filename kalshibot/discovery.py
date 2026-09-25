"""Market discovery and normalization across ALL Kalshi markets.

Not weather. Not any hard-coded series. Everything the exchange is currently
running, grouped into events, with the metadata that proves a structural
relationship rather than one inferred from ticker names.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .models import Market

log = logging.getLogger(__name__)


@dataclass
class NormalizedEvent:
    event_ticker: str
    series_ticker: str = ""
    title: str = ""
    mutually_exclusive: bool = False
    markets: list[Market] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def n_markets(self) -> int:
        return len(self.markets)

    @property
    def activity(self) -> int:
        """Total traded interest across the event, from the LIST response.

        The list endpoint reports bid/ask as 0 on this deployment, but volume
        and open interest are real. A market nobody has ever traded almost
        certainly has no resting orders either, so this is the cheapest
        available filter - it costs no extra API calls.
        """
        return sum(m.volume + m.open_interest for m in self.markets)

    def most_active(self, n: int) -> list[Market]:
        return sorted(self.markets,
                      key=lambda m: -(m.volume + m.open_interest + m.liquidity))[:n]

    def quoted_markets(self) -> list[Market]:
        return [m for m in self.markets if m.yes_bid or m.yes_ask
                or m.no_bid or m.no_ask]

    def earliest_close(self) -> datetime | None:
        times = [m.close_time for m in self.markets if m.close_time]
        return min(times) if times else None

    def minutes_to_close(self) -> float:
        t = self.earliest_close()
        if not t:
            return float("inf")
        return (t - datetime.now(timezone.utc)).total_seconds() / 60.0


@dataclass
class DiscoveryStats:
    events_seen: int = 0
    markets_seen: int = 0
    markets_quoted: int = 0
    events_exclusive: int = 0
    events_kept: int = 0
    markets_kept: int = 0
    dropped_no_quotes: int = 0
    dropped_no_activity: int = 0
    markets_with_activity: int = 0
    dropped_closing_soon: int = 0
    dropped_too_few_markets: int = 0
    #: filled in after order books are fetched
    books_fetched: int = 0
    markets_with_depth: int = 0
    truncated: bool = False
    page_limit: int = 0

    def summary(self) -> str:
        return (f"{self.events_seen} events / {self.markets_seen} markets seen | "
                f"{self.markets_quoted} quoted in the list response | "
                f"{self.events_exclusive} exclusive | "
                f"kept {self.events_kept} events / {self.markets_kept} markets")

    def activity_summary(self) -> str:
        return (f"{self.markets_with_activity} markets have traded volume or "
                f"open interest | {self.dropped_no_activity} dropped as never traded")

    def liquidity_summary(self) -> str:
        return (f"{self.books_fetched} order books fetched | "
                f"{self.markets_with_depth} have real depth")


class MarketDiscovery:
    def __init__(self, market_data_client, cfg: dict | None = None):
        self.client = market_data_client
        c = cfg or {}
        self.min_minutes_to_close = float(c.get("min_minutes_to_close", 15))
        self.max_hours_to_close = float(c.get("max_hours_to_close", 24 * 14))
        # DEFAULT FALSE, deliberately.
        #
        # Kalshi's /markets LIST endpoint frequently returns yes_bid/yes_ask as
        # 0 even for markets that have a perfectly good order book. Filtering
        # on those fields throws away most of the exchange before a single
        # order book is fetched - which is exactly what was hiding the weather
        # markets. Liquidity is judged from the real order book instead, after
        # it has been pulled. Set this true only to save API calls when you
        # already know the list endpoint is populated.
        self.require_quotes = bool(c.get("require_quotes", False))
        self.max_events = int(c.get("max_events", 0))  # 0 = no cap
        self.series_filter = [s.upper() for s in (c.get("series_filter") or [])]
        self.max_market_pages = int(c.get("max_market_pages", 120))
        #: skip events with no traded interest at all
        self.min_event_activity = int(c.get("min_event_activity", 0))

    # ---------------------------------------------------------------
    def discover(self) -> tuple[list[NormalizedEvent], DiscoveryStats]:
        stats = DiscoveryStats()

        exclusive_flags, event_meta = self._event_metadata()
        markets = self.client.get_markets(status="open", max_pages=self.max_market_pages)
        stats.markets_seen = len(markets)
        stats.page_limit = self.max_market_pages
        # 200 markets per page: hitting the cap exactly means there is more
        # exchange we never looked at.
        stats.truncated = len(markets) >= self.max_market_pages * 200

        by_event: dict[str, list[Market]] = {}
        for m in markets:
            if m.yes_bid or m.yes_ask or m.no_bid or m.no_ask:
                stats.markets_quoted += 1
            if m.volume or m.open_interest:
                stats.markets_with_activity += 1
            key = m.event_ticker or m.ticker.rsplit("-", 1)[0]
            by_event.setdefault(key, []).append(m)
        stats.events_seen = len(by_event)

        events: list[NormalizedEvent] = []
        for event_ticker, ms in by_event.items():
            if self.series_filter:
                series = (ms[0].series_ticker or event_ticker).upper()
                if not any(series.startswith(f) for f in self.series_filter):
                    continue

            usable = ms
            if self.require_quotes:
                usable = [m for m in ms if (m.yes_ask and m.no_ask)
                          or (m.yes_bid and m.no_bid)]
                stats.dropped_no_quotes += len(ms) - len(usable)
            if not usable:
                stats.dropped_too_few_markets += len(ms)
                continue

            meta = event_meta.get(event_ticker, {})
            ev = NormalizedEvent(
                event_ticker=event_ticker,
                series_ticker=usable[0].series_ticker or meta.get("series_ticker", ""),
                title=meta.get("title", "") or usable[0].title,
                mutually_exclusive=bool(exclusive_flags.get(event_ticker, False)),
                markets=usable,
                raw=meta,
            )
            if ev.mutually_exclusive:
                stats.events_exclusive += 1

            if self.min_event_activity and ev.activity < self.min_event_activity:
                stats.dropped_no_activity += len(usable)
                continue

            mins = ev.minutes_to_close()
            if mins < self.min_minutes_to_close:
                stats.dropped_closing_soon += len(usable)
                continue
            if mins > self.max_hours_to_close * 60:
                continue

            events.append(ev)
            stats.events_kept += 1
            stats.markets_kept += len(usable)

        # Look at the most promising events first, because the order book
        # budget will run out long before the exchange does:
        #   1. exchange-confirmed mutually exclusive (basket candidates)
        #   2. more markets in the event (more ladder pairs to compare)
        #   3. closing sooner (more likely to be actively quoted)
        events.sort(key=lambda e: (not e.mutually_exclusive,
                                   -e.n_markets,
                                   e.minutes_to_close()))
        if self.max_events:
            events = events[: self.max_events]
        return events, stats

    # ---------------------------------------------------------------
    def discover_series(self, series: list[str]) -> tuple[list, DiscoveryStats]:
        """Only the markets in the named series. Asked for directly.

        `discover()` reads the whole exchange and then filters. That is the
        right shape for structural arbitrage, which does not know in advance
        where a mispricing will be - but it is absurd for a forecast strategy
        that can name its six series. The first live run spent twenty minutes
        paging eighty thousand markets to reach four hundred it already knew
        the tickers of, and the scan that came out of it took 2.8 hours.

        Kalshi will filter server-side, so this is a handful of small calls.
        """
        stats = DiscoveryStats()
        if not series:
            return [], stats

        markets = []
        for name in series:
            try:
                got = self.client.get_markets(status="open", series_ticker=name,
                                              max_pages=self.max_market_pages)
            except Exception as exc:  # noqa: BLE001
                # One series failing must not cost us the other five.
                log.warning("DISCOVERY  could not read series %s: %s", name, exc)
                continue
            markets.extend(got)

        if not markets:
            log.warning("DISCOVERY  the exchange returned nothing for %s",
                        ", ".join(series))
        return self._build_events(markets, stats)

    def _build_events(self, markets, stats) -> tuple[list, DiscoveryStats]:
        """Markets -> events, with the same filters `discover()` applies."""
        exclusive_flags, event_meta = self._event_metadata()
        stats.markets_seen = len(markets)
        stats.page_limit = self.max_market_pages

        by_event: dict[str, list[Market]] = {}
        for m in markets:
            if m.yes_bid or m.yes_ask or m.no_bid or m.no_ask:
                stats.markets_quoted += 1
            if m.volume or m.open_interest:
                stats.markets_with_activity += 1
            key = m.event_ticker or m.ticker.rsplit("-", 1)[0]
            by_event.setdefault(key, []).append(m)
        stats.events_seen = len(by_event)

        events = []
        for event_ticker, ms in by_event.items():
            usable = ms
            if self.require_quotes:
                usable = [m for m in ms if (m.yes_ask and m.no_ask)
                          or (m.yes_bid and m.no_bid)]
                stats.dropped_no_quotes += len(ms) - len(usable)
            if not usable:
                stats.dropped_too_few_markets += len(ms)
                continue
            meta = event_meta.get(event_ticker, {})
            ev = NormalizedEvent(
                event_ticker=event_ticker,
                series_ticker=usable[0].series_ticker or meta.get("series_ticker", ""),
                title=meta.get("title", "") or usable[0].title,
                mutually_exclusive=bool(exclusive_flags.get(event_ticker, False)),
                markets=usable, raw=meta)
            if ev.mutually_exclusive:
                stats.events_exclusive += 1
            mins = ev.minutes_to_close()
            if mins < self.min_minutes_to_close:
                stats.dropped_closing_soon += len(usable)
                continue
            if mins > self.max_hours_to_close * 60:
                continue
            events.append(ev)
            stats.events_kept += 1
            stats.markets_kept += len(usable)

        events.sort(key=lambda e: (not e.mutually_exclusive, -e.n_markets,
                                   e.minutes_to_close()))
        return events, stats

    # ---------------------------------------------------------------
    def _event_metadata(self) -> tuple[dict[str, bool], dict[str, dict]]:
        """Pull events so we know which ones the exchange says are mutually
        exclusive. Never guessed - if the flag is absent, the basket detector
        stays off for that event."""
        flags: dict[str, bool] = {}
        meta: dict[str, dict] = {}
        try:
            for ev in self.client.get_events(status="open"):
                ticker = ev.get("event_ticker") or ""
                if not ticker:
                    continue
                meta[ticker] = ev
                for key in ("mutually_exclusive", "mutually_exclusive_markets",
                            "is_mutually_exclusive"):
                    if key in ev:
                        flags[ticker] = bool(ev[key])
                        break
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read event metadata (%s); mutually-exclusive "
                        "baskets will stay disabled", exc)
        return flags, meta
