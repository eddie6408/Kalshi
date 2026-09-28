"""Provider interfaces. The strategy only ever sees normalized objects
(MarketInfo, Quote, Depth, EventContext), so a provider can be swapped or
chained without touching the trading engine (sections 10 and 11)."""
from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from typing import Optional

from lvt.models import Depth, MarketInfo, Quote

log = logging.getLogger("lvt.data")


class ProviderError(Exception):
    pass


class MarketDataProvider(abc.ABC):
    name = "abstract"

    @abc.abstractmethod
    def list_open_markets(self, pause_s: float = 0.0, series_ticker: str | None = None) -> list[MarketInfo]:
        ...

    @abc.abstractmethod
    def series_catalog(self) -> dict[str, dict]:
        """series_ticker -> {category, fee_type, fee_multiplier, title}"""

    @abc.abstractmethod
    def quotes(self, tickers: list[str]) -> dict[str, Quote]:
        ...

    @abc.abstractmethod
    def depth(self, ticker: str, levels: int) -> Depth:
        ...

    def healthy(self) -> bool:
        return True


@dataclass
class EventContext:
    """Normalized live-event state. Context only - never a trading signal on
    its own (section 29)."""
    event_ticker: str
    provider: str
    sport: str
    status: str                        # scheduled | live | final | unknown
    fetched_ts: float
    event_ts: float = 0.0              # provider's own update time, if any
    competitors: list[str] = field(default_factory=list)
    score: str = ""
    period: str = ""                   # set / quarter / inning / half
    clock: str = ""
    detail: dict = field(default_factory=dict)   # sport-specific (server, outs, ...)

    def summary(self) -> dict:
        return {"status": self.status, "score": self.score, "period": self.period,
                "clock": self.clock, "competitors": self.competitors, **self.detail}


class SportsEventDataProvider(abc.ABC):
    name = "abstract"

    @abc.abstractmethod
    def refresh_index(self) -> None:
        """Rebuild the event_ticker -> provider-id mapping."""

    @abc.abstractmethod
    def covers(self, event_ticker: str) -> bool:
        ...

    @abc.abstractmethod
    def context(self, event_ticker: str) -> Optional[EventContext]:
        ...


class FallbackMarketData(MarketDataProvider):
    """Try providers in order; the first healthy one that answers wins.

    Kalshi prices only exist at Kalshi, so today the chain is the public
    REST provider (optionally with a second base URL). A signed WebSocket
    provider can be put in front once a key is available.
    """
    name = "fallback"

    def __init__(self, providers: list[MarketDataProvider]):
        if not providers:
            raise ValueError("need at least one provider")
        self.providers = providers
        self.active = providers[0].name

    def _call(self, fn: str, *a, **kw):
        last: Exception | None = None
        for p in self.providers:
            if not p.healthy():
                continue
            try:
                out = getattr(p, fn)(*a, **kw)
                if self.active != p.name:
                    log.warning("market data now served by %s", p.name)
                    self.active = p.name
                return out
            except Exception as exc:        # noqa: BLE001 - try the next one
                last = exc
                log.warning("provider %s.%s failed: %s", p.name, fn, exc)
        raise ProviderError(f"all market-data providers failed for {fn}: {last}")

    def list_open_markets(self, pause_s: float = 0.0, series_ticker: str | None = None):
        return self._call("list_open_markets", pause_s=pause_s, series_ticker=series_ticker)

    def series_catalog(self):
        return self._call("series_catalog")

    def quotes(self, tickers):
        return self._call("quotes", tickers)

    def depth(self, ticker, levels):
        return self._call("depth", ticker, levels)


class CompositeContext(SportsEventDataProvider):
    """Primary provider first, then fallbacks for events it does not cover
    or when it fails."""
    name = "composite"

    def __init__(self, providers: list[SportsEventDataProvider]):
        self.providers = providers

    def refresh_index(self) -> None:
        for p in self.providers:
            try:
                p.refresh_index()
            except Exception as exc:        # noqa: BLE001
                log.warning("context provider %s index refresh failed: %s", p.name, exc)

    def covers(self, event_ticker: str) -> bool:
        return any(p.covers(event_ticker) for p in self.providers)

    def context(self, event_ticker: str) -> Optional[EventContext]:
        for p in self.providers:
            if not p.covers(event_ticker):
                continue
            try:
                ctx = p.context(event_ticker)
                if ctx is not None:
                    return ctx
            except Exception as exc:        # noqa: BLE001
                log.warning("context provider %s failed for %s: %s", p.name, event_ticker, exc)
        return None
