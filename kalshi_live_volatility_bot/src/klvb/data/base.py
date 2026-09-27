"""Provider interfaces. The trading engine only ever talks to these abstractions,
so data sources can be swapped or chained without touching strategy code.
"""
from __future__ import annotations

import abc
import asyncio
import time
from typing import Any

from ..models import MarketMeta, MarketSnapshot, OrderBook, SportsContext, TradePrint


class DataUnavailable(RuntimeError):
    """Raised when a provider cannot supply trustworthy data (callers fail closed)."""


class MarketDataProvider(abc.ABC):
    name: str = "base"

    async def start(self) -> None:  # pragma: no cover - trivial
        pass

    async def stop(self) -> None:  # pragma: no cover - trivial
        pass

    @abc.abstractmethod
    async def discover_markets(self) -> list[tuple[MarketMeta, MarketSnapshot]]:
        """All currently tradable candidate markets with a fresh top-of-book snapshot."""

    @abc.abstractmethod
    async def snapshots(self, tickers: list[str]) -> list[MarketSnapshot]:
        """Fresh top-of-book snapshots for the given tickers."""

    @abc.abstractmethod
    async def orderbook(self, ticker: str) -> OrderBook | None:
        ...

    @abc.abstractmethod
    async def trades(self, ticker: str, since_ts: float) -> list[TradePrint]:
        ...

    def status(self) -> dict[str, Any]:
        return {"name": self.name}

    def healthy(self) -> bool:
        return True


class SportsEventDataProvider(abc.ABC):
    name: str = "base_sports"

    @abc.abstractmethod
    async def contexts(self) -> dict[str, SportsContext]:
        """event_ticker -> latest normalized sports context for currently live/near-live events."""

    def status(self) -> dict[str, Any]:
        return {"name": self.name}


class NullSportsProvider(SportsEventDataProvider):
    name = "none"

    async def contexts(self) -> dict[str, SportsContext]:
        return {}


class RateLimiter:
    """Async token bucket. Keeps us far below exchange limits (shared host IP)."""

    def __init__(self, rate_per_sec: float, burst: float | None = None):
        self.rate = rate_per_sec
        self.capacity = burst if burst is not None else max(1.0, rate_per_sec)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()
        self.count = 0
        self.window_start = time.monotonic()

    async def acquire(self) -> None:
        async with self.lock:
            while True:
                t = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (t - self.updated) * self.rate)
                self.updated = t
                if self.tokens >= 1:
                    self.tokens -= 1
                    self.count += 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)

    def observed_rate(self) -> float:
        el = time.monotonic() - self.window_start
        r = self.count / el if el > 0 else 0.0
        if el > 60:
            self.count, self.window_start = 0, time.monotonic()
        return r


class FailoverMarketDataProvider(MarketDataProvider):
    """Tries providers in order; the first that answers without error wins.

    Fallback logic: a provider that raises (or reports unhealthy) is skipped for
    this call and the next one is tried. If all fail, DataUnavailable propagates
    and the engine treats every market as STALE_DATA (no new trades).
    """
    name = "failover"

    def __init__(self, providers: list[MarketDataProvider]):
        if not providers:
            raise ValueError("need at least one provider")
        self.providers = providers
        self.active: str = providers[0].name
        self.failures: dict[str, int] = {p.name: 0 for p in providers}

    async def start(self) -> None:
        for p in self.providers:
            await p.start()

    async def stop(self) -> None:
        for p in self.providers:
            await p.stop()

    async def _call(self, method: str, *args):
        last: Exception | None = None
        for p in self.providers:
            if not p.healthy():
                continue
            try:
                out = await getattr(p, method)(*args)
                self.active = p.name
                return out
            except Exception as e:  # noqa: BLE001 - any provider failure triggers fallback
                self.failures[p.name] = self.failures.get(p.name, 0) + 1
                last = e
        raise DataUnavailable(f"all providers failed for {method}: {last}")

    async def discover_markets(self):
        return await self._call("discover_markets")

    async def snapshots(self, tickers):
        return await self._call("snapshots", tickers)

    async def orderbook(self, ticker):
        return await self._call("orderbook", ticker)

    async def trades(self, ticker, since_ts):
        return await self._call("trades", ticker, since_ts)

    def healthy(self) -> bool:
        return any(p.healthy() for p in self.providers)

    def status(self) -> dict[str, Any]:
        return {"name": self.name, "active": self.active, "failures": dict(self.failures),
                "providers": [p.status() for p in self.providers]}


async def market_raw(provider: MarketDataProvider, ticker: str) -> dict[str, Any]:
    """Find a provider in a chain that can return the raw market object."""
    chain = getattr(provider, "providers", [provider])
    for p in chain:
        target = getattr(p, "rest", p)
        if hasattr(target, "market"):
            return await target.market(ticker)
    raise DataUnavailable("no provider can fetch market details")
