"""Kalshi public REST market-data provider (no credentials required).

This is the primary PAPER/SHADOW data source: real, live Kalshi markets via the
unauthenticated market-data endpoints. It never places orders and never loads
any key.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from ..models import MarketMeta, MarketSnapshot, OrderBook, TradePrint
from .base import DataUnavailable, MarketDataProvider, RateLimiter
from .parsing import classify_sport, parse_market_meta, parse_market_snapshot, parse_orderbook, parse_trade

log = logging.getLogger("klvb.data.rest")

SERIES_REFRESH_SECONDS = 3600


class KalshiRestClient:
    """Thin async HTTP client with rate limiting, retries and backoff."""

    def __init__(self, base_url: str, rate_limiter: RateLimiter, timeout: float = 8.0, max_retries: int = 3,
                 transport: httpx.AsyncBaseTransport | None = None, auth=None):
        self.base_url = base_url.rstrip("/")
        self.limiter = rate_limiter
        self.max_retries = max_retries
        self.auth = auth  # optional callable(method, path) -> headers (LIVE / data-key only)
        self.client = httpx.AsyncClient(timeout=timeout, transport=transport,
                                        headers={"User-Agent": "kalshi-live-volatility-bot/0.1"})
        self.last_ok_ts: float | None = None
        self.last_error: str | None = None
        self.rate_limited_count = 0
        self.error_count = 0

    async def close(self) -> None:
        await self.client.aclose()

    def _path(self, path: str) -> str:
        # Kalshi signs the full path including /trade-api/v2
        from urllib.parse import urlparse
        return urlparse(self.base_url).path + path

    async def request(self, method: str, path: str, params: dict | None = None, json: Any = None) -> dict:
        url = self.base_url + path
        attempt = 0
        while True:
            await self.limiter.acquire()
            headers = self.auth(method, self._path(path)) if self.auth else None
            try:
                r = await self.client.request(method, url, params=params, json=json, headers=headers)
            except httpx.HTTPError as e:
                self.error_count += 1
                self.last_error = f"{type(e).__name__}: {e}"
                if attempt >= self.max_retries:
                    raise DataUnavailable(self.last_error) from e
                attempt += 1
                await asyncio.sleep(min(8.0, 0.5 * 2 ** attempt))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                self.error_count += 1
                if r.status_code == 429:
                    self.rate_limited_count += 1
                self.last_error = f"HTTP {r.status_code}"
                if attempt >= self.max_retries:
                    raise DataUnavailable(f"{method} {path}: HTTP {r.status_code}")
                attempt += 1
                await asyncio.sleep(min(10.0, 1.0 * 2 ** attempt))
                continue
            if r.status_code >= 400:
                self.error_count += 1
                self.last_error = f"HTTP {r.status_code}: {r.text[:200]}"
                raise ExchangeHTTPError(r.status_code, r.text[:500])
            self.last_ok_ts = time.time()
            return r.json() if r.content else {}

    async def get(self, path: str, params: dict | None = None) -> dict:
        return await self.request("GET", path, params=params)


class ExchangeHTTPError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


class KalshiPublicRestProvider(MarketDataProvider):
    name = "kalshi_rest"

    def __init__(self, cfg, client: KalshiRestClient | None = None):
        self.cfg = cfg
        k = cfg.kalshi
        self.client = client or KalshiRestClient(
            k.rest_base_url, RateLimiter(k.max_requests_per_second), k.request_timeout_seconds, k.max_retries)
        self.series_info: dict[str, dict] = {}
        self.series_loaded_ts = 0.0
        self.categories = list(cfg.scanner.categories)

    async def stop(self) -> None:
        await self.client.close()

    def healthy(self) -> bool:
        ok = self.client.last_ok_ts
        return ok is None or time.time() - ok < 120

    def status(self) -> dict[str, Any]:
        c = self.client
        return {"name": self.name, "last_ok_ts": c.last_ok_ts, "last_error": c.last_error,
                "errors": c.error_count, "rate_limited": c.rate_limited_count,
                "req_per_sec": round(c.limiter.observed_rate(), 2)}

    async def _load_series(self) -> None:
        if time.time() - self.series_loaded_ts < SERIES_REFRESH_SECONDS and self.series_info:
            return
        info: dict[str, dict] = {}
        for cat in self.categories:
            try:
                data = await self.client.get("/series", {"category": cat})
            except Exception as e:  # noqa: BLE001 - series metadata is best-effort
                log.warning("series load failed for %s: %s", cat, e)
                continue
            for s in data.get("series") or []:
                info[s.get("ticker", "")] = s
        if info:
            self.series_info = info
            self.series_loaded_ts = time.time()

    async def discover_markets(self) -> list[tuple[MarketMeta, MarketSnapshot]]:
        await self._load_series()
        d = self.cfg.data
        now_ = time.time()
        params: dict[str, Any] = {
            "status": "open", "limit": 1000,
            "min_close_ts": int(now_ + self.cfg.scanner.min_time_to_close_seconds),
            "max_close_ts": int(now_ + d.discovery_max_close_days * 86400),
        }
        if self.cfg.scanner.exclude_multivariate:
            params["mve_filter"] = "exclude"
        out: list[tuple[MarketMeta, MarketSnapshot]] = []
        cursor = None
        for _ in range(int(d.discovery_max_pages)):
            if cursor:
                params["cursor"] = cursor
            data = await self.client.get("/markets", params)
            ts = time.time()
            for m in data.get("markets") or []:
                try:
                    meta = parse_market_meta(m, self.series_info, classify_sport)
                    snap = parse_market_snapshot(m, ts)
                except (KeyError, TypeError, ValueError) as e:
                    log.debug("skip malformed market: %s", e)
                    continue
                out.append((meta, snap))
            cursor = data.get("cursor")
            if not cursor:
                break
        return out

    async def snapshots(self, tickers: list[str]) -> list[MarketSnapshot]:
        out: list[MarketSnapshot] = []
        for i in range(0, len(tickers), 50):
            chunk = tickers[i:i + 50]
            data = await self.client.get("/markets", {"tickers": ",".join(chunk), "limit": len(chunk)})
            ts = time.time()
            for m in data.get("markets") or []:
                out.append(parse_market_snapshot(m, ts))
        return out

    async def orderbook(self, ticker: str) -> OrderBook | None:
        data = await self.client.get(f"/markets/{ticker}/orderbook", {"depth": int(self.cfg.data.orderbook_depth)})
        return parse_orderbook(data, time.time())

    async def trades(self, ticker: str, since_ts: float) -> list[TradePrint]:
        data = await self.client.get("/markets/trades", {"ticker": ticker, "min_ts": int(since_ts), "limit": 1000})
        out = []
        for t in data.get("trades") or []:
            tp = parse_trade(t)
            if tp and tp.ts >= since_ts:
                tp.ticker = tp.ticker or ticker
                out.append(tp)
        out.sort(key=lambda t: t.ts)
        return out
