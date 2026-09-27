"""Optional Kalshi websocket market-data provider.

Kalshi requires an authenticated websocket handshake even for public channels,
so this provider is only used when [data].use_websocket = true AND a *data*
credential scope is configured (KALSHI_DATA_*). It subscribes to the public
`ticker` channel and serves top-of-book from a local cache. It never trades.

It is chained in front of the REST provider through FailoverMarketDataProvider:
if the socket is down or a ticker's cache is stale, REST answers instead.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any
from urllib.parse import urlparse

from ..models import MarketSnapshot
from .base import DataUnavailable, MarketDataProvider
from .parsing import count_field, price_field

log = logging.getLogger("klvb.data.ws")


class KalshiWebsocketProvider(MarketDataProvider):
    name = "kalshi_ws"

    def __init__(self, cfg, signer, rest_fallback: MarketDataProvider):
        self.cfg = cfg
        self.signer = signer
        self.rest = rest_fallback
        self.cache: dict[str, MarketSnapshot] = {}
        self.subscribed: set[str] = set()
        self.wanted: set[str] = set()
        self.connected = False
        self.reconnects = 0
        self._task: asyncio.Task | None = None
        self._ws = None
        self._msg_id = 0

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="kalshi-ws")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def healthy(self) -> bool:
        return self.connected

    def status(self) -> dict[str, Any]:
        return {"name": self.name, "connected": self.connected, "reconnects": self.reconnects,
                "subscribed": len(self.subscribed)}

    async def _run(self) -> None:
        import websockets

        url = self.cfg.kalshi.ws_url
        backoff = 1.0
        while True:
            try:
                headers = self.signer.headers("GET", urlparse(url).path)
                async with websockets.connect(url, additional_headers=headers, ping_interval=15) as ws:
                    self._ws, self.connected, backoff = ws, True, 1.0
                    self.subscribed.clear()
                    await self._sync_subscriptions()
                    async for raw in ws:
                        self._handle(raw)
                        if self.wanted - self.subscribed:
                            await self._sync_subscriptions()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - reconnect on any socket failure
                log.warning("websocket error: %s", e)
            self.connected = False
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 2)

    async def _sync_subscriptions(self) -> None:
        new = sorted(self.wanted - self.subscribed)
        if not new or self._ws is None:
            return
        self._msg_id += 1
        await self._ws.send(json.dumps({"id": self._msg_id, "cmd": "subscribe",
                                        "params": {"channels": ["ticker"], "market_tickers": new}}))
        self.subscribed.update(new)

    def _handle(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if msg.get("type") != "ticker":
            return
        m = msg.get("msg") or {}
        t = m.get("market_ticker")
        if not t:
            return
        prev = self.cache.get(t)
        bid = price_field(m, "yes_bid")
        ask = price_field(m, "yes_ask")
        self.cache[t] = MarketSnapshot(
            ticker=t, ts=time.time(),
            yes_bid=bid if bid and bid > 0 else None,
            yes_ask=ask if ask and ask < 100 else None,
            last_price=price_field(m, "price") or (prev.last_price if prev else None),
            volume=count_field(m, "volume") or (prev.volume if prev else None),
            open_interest=count_field(m, "open_interest") or (prev.open_interest if prev else None),
            yes_bid_size=prev.yes_bid_size if prev else None,
            yes_ask_size=prev.yes_ask_size if prev else None,
            source="kalshi_ws", exchange_ts=m.get("ts"),
        )

    async def discover_markets(self):
        return await self.rest.discover_markets()

    async def snapshots(self, tickers: list[str]) -> list[MarketSnapshot]:
        self.wanted.update(tickers)
        if not self.connected:
            raise DataUnavailable("websocket not connected")
        max_age = self.cfg.data.max_data_age_seconds
        now_ = time.time()
        fresh = [self.cache[t] for t in tickers if t in self.cache and now_ - self.cache[t].ts <= max_age / 2]
        missing = [t for t in tickers if t not in {s.ticker for s in fresh}]
        if missing:
            # the ticker channel only pushes on change; quiet markets are refreshed via REST
            for s in await self.rest.snapshots(missing):
                prev = self.cache.get(s.ticker)
                if prev is None or prev.ts < s.ts:
                    self.cache[s.ticker] = s
                fresh.append(s)
        return fresh

    async def orderbook(self, ticker):
        return await self.rest.orderbook(ticker)

    async def trades(self, ticker, since_ts):
        return await self.rest.trades(ticker, since_ts)
