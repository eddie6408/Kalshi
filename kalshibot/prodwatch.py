"""Package 3a: the PRODUCTION book and trade tape for the markets we price.

The market-skill gate compares the model with the demo market, and the demo
market is not the one that decides whether this strategy can make money. The
same weather tickers trade on production with real depth and a real tape, and
Kalshi publishes both without authentication. This worker reads them for the
markets the forecast strategy is pricing, on its own clock and its own request
budget, so nothing in the trading path ever waits on a production request.

    watch(ticker, info)     the strategy says "I priced this" (every pass)
    latest(ticker)          the newest production book, and when it was read
    mid_at(ticker, t)       the production mid near time t (recent history)
    run_pass()              the worker: books every book_interval_seconds,
                            trade prints every trades_interval_seconds

Listeners (the recorder, C2 M0's maker shadow) are told about every book and
every batch of new prints.

Read only, through LiveBookReader: GET, unauthenticated, path-allowlisted in
env_guard. There is no credential here and no method that could send an order.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from .recorder import book_levels

log = logging.getLogger(__name__)


@dataclass
class WatchConfig:
    enabled: bool = True
    book_interval_seconds: float = 120.0
    trades_interval_seconds: float = 300.0
    #: shared budget for every production request this worker makes
    max_requests_per_second: float = 2.0
    #: stop reading a market the strategy has not priced for this long
    forget_after_minutes: float = 30.0
    #: how much book history to keep in memory per market (for mid_at)
    history_minutes: float = 240.0

    @classmethod
    def from_dict(cls, d: dict | None) -> "WatchConfig":
        d = d or {}
        return cls(
            enabled=bool(d.get("enabled", True)),
            book_interval_seconds=float(d.get("book_interval_seconds", 120.0)),
            trades_interval_seconds=float(d.get("trades_interval_seconds", 300.0)),
            max_requests_per_second=float(d.get("max_requests_per_second", 2.0)),
            forget_after_minutes=float(d.get("forget_after_minutes", 30.0)),
            history_minutes=float(d.get("history_minutes", 240.0)),
        )


@dataclass
class Quote:
    ticker: str
    book: object
    read_at: float
    yes_bid: int | None = None
    yes_ask: int | None = None

    @classmethod
    def of(cls, ticker: str, book, read_at: float) -> "Quote":
        return cls(ticker, book, read_at,
                   book.best_price("yes", "sell") if book else None,
                   book.best_price("yes", "buy") if book else None)

    def mid(self, side: str = "yes") -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        m = (self.yes_bid + self.yes_ask) / 2.0
        return m if side == "yes" else 100.0 - m


@dataclass
class Trade:
    trade_id: str
    ts: float
    yes_cents: int | None
    no_cents: int | None
    count: int
    taker_side: str

    def price(self, side: str) -> int | None:
        return self.yes_cents if side == "yes" else self.no_cents


def _cents(dollars, cents) -> int | None:
    try:
        if dollars is not None:
            return int(round(float(dollars) * 100))
        if cents is not None:
            return int(round(float(cents)))
    except (TypeError, ValueError):
        pass
    return None


def _ts(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_trade(raw: dict) -> Trade | None:
    ts = _ts(raw.get("created_time"))
    count = raw.get("count_fp", raw.get("count"))
    try:
        n = int(round(float(count)))
    except (TypeError, ValueError):
        return None
    side = str(raw.get("taker_side") or "").lower()
    if ts is None or n <= 0 or side not in ("yes", "no"):
        return None
    yes = _cents(raw.get("yes_price_dollars"), raw.get("yes_price"))
    no = _cents(raw.get("no_price_dollars"), raw.get("no_price"))
    if yes is None and no is not None:
        yes = 100 - no
    if no is None and yes is not None:
        no = 100 - yes
    return Trade(str(raw.get("trade_id") or f"{ts}:{yes}:{n}:{side}"), ts, yes, no, n, side)


class RateLimiter:
    """At most `rate` requests a second, spaced evenly. Blocks the WORKER,
    which is the point: it is the only thread that calls production."""

    def __init__(self, rate: float, clock=time.monotonic, sleep=time.sleep):
        self.gap = 1.0 / rate if rate > 0 else 0.0
        self.clock = clock
        self.sleep = sleep
        self._next = 0.0

    def wait(self) -> None:
        now = self.clock()
        if now < self._next:
            self.sleep(self._next - now)
            now = self._next
        self._next = now + self.gap


@dataclass
class _Watched:
    info: dict = field(default_factory=dict)
    seen_at: float = 0.0
    book_at: float = 0.0
    trades_at: float = 0.0
    last_trade_ts: float | None = None
    seen_ids: deque = field(default_factory=lambda: deque(maxlen=2000))
    history: deque = field(default_factory=deque)


class ProductionWatch:
    def __init__(self, reader, recorder=None, cfg: WatchConfig | None = None,
                 clock=time.time, limiter: RateLimiter | None = None):
        self.reader = reader
        self.recorder = recorder
        self.cfg = cfg or WatchConfig()
        self.clock = clock
        self.limiter = limiter or RateLimiter(self.cfg.max_requests_per_second)
        self._lock = threading.Lock()
        self._w: dict[str, _Watched] = {}
        self._latest: dict[str, Quote] = {}
        #: objects with on_book(ticker, quote, info) and/or
        #: on_trades(ticker, trades, complete_until)
        self.listeners: list = []
        self.books_read = 0
        self.trades_read = 0
        self.errors = 0
        self.backoff_until = 0.0

    # ---- called from the strategy's thread ------------------------------
    def watch(self, ticker: str, info: dict | None = None) -> None:
        now = self.clock()
        with self._lock:
            w = self._w.get(ticker)
            if w is None:
                w = self._w[ticker] = _Watched()
                # Start the tape from now: history before we cared about a
                # market is not what this is for.
                w.last_trade_ts = now - self.cfg.trades_interval_seconds
            w.seen_at = now
            if info is not None:
                w.info = dict(info, at=now)

    def latest(self, ticker: str, max_age: float | None = None) -> Quote | None:
        with self._lock:
            q = self._latest.get(ticker)
        if q is None:
            return None
        if max_age is not None and self.clock() - q.read_at > max_age:
            return None
        return q

    def mid_at(self, ticker: str, ts: float, side: str = "yes",
               tolerance: float = 180.0) -> float | None:
        """The production mid from the book read nearest to `ts`."""
        with self._lock:
            w = self._w.get(ticker)
            hist = list(w.history) if w else []
        best = None
        for q in hist:
            d = abs(q.read_at - ts)
            if d <= tolerance and (best is None or d < best[0]):
                best = (d, q)
        return best[1].mid(side) if best else None

    def refresh_now(self) -> int:
        """Operator refresh: forget any back-off and re-read every watched
        market's book and prints on the next pass. Returns how many."""
        with self._lock:
            self.backoff_until = 0.0
            for w in self._w.values():
                w.book_at = 0.0
                w.trades_at = 0.0
            return len(self._w)

    def watched(self) -> list[str]:
        with self._lock:
            return list(self._w)

    # ---- the worker ------------------------------------------------------
    def run_pass(self) -> dict:
        now = self.clock()
        stats = {"books": 0, "trade_polls": 0, "prints": 0, "errors": 0}
        if now < self.backoff_until:
            return stats
        forget = self.cfg.forget_after_minutes * 60
        with self._lock:
            for t in [t for t, w in self._w.items() if now - w.seen_at > forget]:
                del self._w[t]
                self._latest.pop(t, None)
            due_books = [t for t, w in self._w.items()
                         if now - w.book_at >= self.cfg.book_interval_seconds]
            due_trades = [t for t, w in self._w.items()
                          if now - w.trades_at >= self.cfg.trades_interval_seconds]
        for t in due_books:
            if not self._read_book(t, stats):
                break
        for t in due_trades:
            if not self._read_trades(t, stats):
                break
        if stats["books"] or stats["trade_polls"]:
            log.debug("PRODWATCH  %s", stats)
        return stats

    def _failed(self, what: str, exc: Exception, stats: dict) -> bool:
        """Count a failure; on 429 back off for a minute. False = stop the pass."""
        self.errors += 1
        stats["errors"] += 1
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 429:
            self.backoff_until = self.clock() + 60.0
            log.warning("PRODWATCH  rate limited (429) on %s; backing off 60s", what)
            return False
        log.debug("PRODWATCH  %s failed: %s", what, exc)
        return True

    def _read_book(self, ticker: str, stats: dict) -> bool:
        self.limiter.wait()
        try:
            book = self.reader.orderbook(ticker)
        except Exception as exc:  # noqa: BLE001
            return self._failed(f"book {ticker}", exc, stats)
        now = self.clock()
        q = Quote.of(ticker, book, now)
        horizon = now - self.cfg.history_minutes * 60
        with self._lock:
            w = self._w.get(ticker)
            if w is None:
                return True
            w.book_at = now
            info = dict(w.info)
            if book is not None:
                self._latest[ticker] = q
                w.history.append(q)
                while w.history and w.history[0].read_at < horizon:
                    w.history.popleft()
        self.books_read += 1
        stats["books"] += 1
        if self.recorder is not None:
            self.recorder.record("prod_book", {"m": ticker, "b": book_levels(book)})
        if book is not None:
            for li in self.listeners:
                fn = getattr(li, "on_book", None)
                if fn is not None:
                    try:
                        fn(ticker, q, info)
                    except Exception as exc:  # noqa: BLE001
                        log.error("PRODWATCH  listener failed on %s: %s", ticker, exc)
        return True

    def _read_trades(self, ticker: str, stats: dict) -> bool:
        with self._lock:
            w = self._w.get(ticker)
            since = w.last_trade_ts if w else None
        self.limiter.wait()
        asked_at = self.clock()
        try:
            raw = self.reader.trades(ticker, min_ts=int(since) - 1 if since else None)
        except Exception as exc:  # noqa: BLE001
            return self._failed(f"trades {ticker}", exc, stats)
        new: list[Trade] = []
        with self._lock:
            w = self._w.get(ticker)
            if w is None:
                return True
            for r in raw:
                tr = parse_trade(r)
                if tr is None or tr.trade_id in w.seen_ids:
                    continue
                if since is not None and tr.ts < since - 1:
                    continue
                w.seen_ids.append(tr.trade_id)
                new.append(tr)
            new.sort(key=lambda tr: tr.ts)
            if new:
                w.last_trade_ts = max(w.last_trade_ts or 0.0, new[-1].ts)
            w.trades_at = asked_at
        self.trades_read += len(new)
        stats["trade_polls"] += 1
        stats["prints"] += len(new)
        if self.recorder is not None:
            for tr in new:
                self.recorder.record("prod_trades", {
                    "m": ticker, "id": tr.trade_id, "ts": tr.ts, "yes": tr.yes_cents,
                    "n": tr.count, "taker": tr.taker_side})
        # Prints published a few seconds late would be missed by a check
        # that trusted the request time exactly; allow for it.
        complete_until = asked_at - 5.0
        for li in self.listeners:
            fn = getattr(li, "on_trades", None)
            if fn is not None:
                try:
                    fn(ticker, new, complete_until)
                except Exception as exc:  # noqa: BLE001
                    log.error("PRODWATCH  listener failed on %s trades: %s", ticker, exc)
        return True

    def summary(self) -> str:
        return (f"{len(self._w)} markets watched, {self.books_read} books, "
                f"{self.trades_read} prints, {self.errors} errors")
