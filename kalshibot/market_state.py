"""One shared view of the exchange, read by every worker.

Until now each loop asked the REST API for what it needed, so the scan and the
position worker could be looking at two different prices for the same market
in the same second - and a fifteen-minute sweep re-read eighty thousand
markets to find the handful that had moved.

This is the store they share. It holds order books and market records, it
knows HOW OLD each one is and WHERE it came from, and it will not hand out
anything it does not trust.

    scan loop  ----\\
                    >---- MarketState ----+---- WebSocket feed (pushes)
    position worker/                      |
                                          +---- REST client (read-through)

Two rules govern everything here, because a wrong book is worse than no book:

  1. NOTHING STALE IS SERVED AS FRESH. Every read carries a maximum age. Past
     it the cache is a miss and the REST client is asked, exactly as before.

  2. A BOOK THAT MISSED AN UPDATE IS NOT A BOOK. Kalshi numbers its delta
     messages. One gap means the book on this side no longer matches the book
     on theirs, and no amount of subsequent deltas repairs it. The entry is
     marked untrusted immediately, which makes every read a REST read until a
     fresh snapshot arrives.

Neither rule is an optimisation. A book that is quietly wrong prices a trade
wrongly, sizes it wrongly, and reports a profit that was never there.
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .depth import DepthBook, Level
from .models import Market

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Source(str):
    REST = "rest"
    WS = "ws"


@dataclass
class BookEntry:
    """One market's book, plus everything needed to decide whether to trust it."""
    ticker: str
    #: price in cents -> resting contracts, for each side
    yes: dict[int, int] = field(default_factory=dict)
    no: dict[int, int] = field(default_factory=dict)
    updated_at: datetime = field(default_factory=_now)
    source: str = Source.REST
    #: the sequence number of the last message applied, when the feed gives one
    seq: int | None = None
    #: False once a gap is seen; nothing is served until a snapshot replaces it
    trusted: bool = True
    untrusted_reason: str = ""

    def age_seconds(self, now: datetime | None = None) -> float:
        return ((now or _now()) - self.updated_at).total_seconds()

    def to_book(self) -> DepthBook:
        return DepthBook(
            self.ticker,
            [Level(p, q) for p, q in sorted(self.yes.items(), reverse=True) if q > 0],
            [Level(p, q) for p, q in sorted(self.no.items(), reverse=True) if q > 0],
        )


@dataclass
class MarketEntry:
    market: Market
    updated_at: datetime = field(default_factory=_now)
    source: str = Source.REST

    def age_seconds(self, now: datetime | None = None) -> float:
        return ((now or _now()) - self.updated_at).total_seconds()


@dataclass
class StateStats:
    book_hits: int = 0
    book_misses: int = 0
    book_stale: int = 0
    book_untrusted: int = 0
    market_hits: int = 0
    market_misses: int = 0
    snapshots: int = 0
    deltas: int = 0
    gaps: int = 0
    evictions: int = 0
    rest_errors: int = 0

    @property
    def book_hit_rate(self) -> float:
        total = self.book_hits + self.book_misses
        return self.book_hits / total if total else 0.0

    def summary(self) -> str:
        return (f"books {self.book_hits}/{self.book_hits + self.book_misses} from cache "
                f"({self.book_hit_rate * 100:.0f}%), {self.snapshots} snapshots, "
                f"{self.deltas} deltas, {self.gaps} gaps, "
                f"{self.book_stale} stale, {self.book_untrusted} untrusted")


class MarketState:
    """Thread-safe shared cache of books and markets, with a REST fallback.

    `client` is whatever the rest of the bot already uses to read the
    exchange: anything with `get_orderbook(ticker)` and `get_market(ticker)`.
    Passing None makes this a pure store with no fallback, which is what the
    tests use.
    """

    def __init__(self, client=None, *, max_book_age: float = 20.0,
                 max_market_age: float = 300.0, capacity: int = 4000):
        self.client = client
        self.max_book_age = max_book_age
        self.max_market_age = max_market_age
        self.capacity = capacity
        self.stats = StateStats()
        self._lock = threading.RLock()
        self._books: OrderedDict[str, BookEntry] = OrderedDict()
        self._markets: OrderedDict[str, MarketEntry] = OrderedDict()
        #: tickers the feed is subscribed to, so a reconnect can restore them
        self.subscribed: set[str] = set()
        #: Set by the feed: True while its connection is up. A book the live
        #: feed maintains is CURRENT however long it has been quiet - the
        #: exchange would have sent a delta if it had changed - so it is served
        #: past max_book_age. Without this, a quiet demo market went back to
        #: REST every 20 seconds and the feed could not raise the hit rate.
        #: A dropped connection or a sequence gap distrusts the book anyway.
        self.feed_live = None
        #: ...up to this age, as a backstop against a feed that is up but
        #: somehow not delivering.
        self.max_fed_age = 3600.0

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------
    def get_depth(self, ticker: str, depth: int = 50,
                  max_age: float | None = None) -> DepthBook:
        """The book, from cache when it can be trusted and REST when it cannot.

        Signature matches the REST client's so this can stand in for it
        wherever a book is read.
        """
        limit = self.max_book_age if max_age is None else max_age
        with self._lock:
            entry = self._books.get(ticker)
            if entry is not None:
                if not entry.trusted:
                    self.stats.book_untrusted += 1
                elif not self._fresh(entry, limit):
                    self.stats.book_stale += 1
                else:
                    self._books.move_to_end(ticker)
                    self.stats.book_hits += 1
                    return entry.to_book()
            self.stats.book_misses += 1

        # Fetch OUTSIDE the lock: a REST call can take a second, and holding
        # the lock across it would stall every other worker for that second.
        book = self._fetch_book(ticker, depth)
        if book is None:
            return DepthBook.empty(ticker)
        self.put_book(book, source=Source.REST)
        return book

    def get_market(self, ticker: str, max_age: float | None = None, **kw) -> Market | None:
        limit = self.max_market_age if max_age is None else max_age
        with self._lock:
            entry = self._markets.get(ticker)
            # A limit of zero means never serve from the cache. Checked
            # explicitly: two reads in the same microsecond age 0.0,
            # which is not > 0, and a flaky self-test once said so.
            if entry is not None and limit > 0 and entry.age_seconds() <= limit:
                self._markets.move_to_end(ticker)
                self.stats.market_hits += 1
                return entry.market
            self.stats.market_misses += 1

        if self.client is None:
            return None
        try:
            market = self.client.get_market(ticker)
        except Exception as exc:  # noqa: BLE001
            self.stats.rest_errors += 1
            log.debug("STATE  could not read market %s: %s", ticker, exc)
            return None
        self.put_market(market, source=Source.REST)
        return market

    def _fresh(self, entry, limit: float) -> bool:
        """May this cached book be served? A limit of 0 always says no."""
        if limit <= 0:
            return False
        age = entry.age_seconds()
        if age <= limit:
            return True
        return (entry.source == Source.WS and entry.ticker in self.subscribed
                and self.feed_live is not None and bool(self.feed_live())
                and age <= self.max_fed_age)

    def get_depths(self, tickers, depth: int = 50, workers: int = 8,
                   max_age: float | None = None) -> dict:
        """Many books at once - the call a scan actually makes.

        This is where the saving is. `get_depth` one market at a time is the
        position worker's pattern; a sweep asks for hundreds, and fetching the
        ones that have not moved is most of a four-minute scan. Cached entries
        are answered here and only the misses reach the network, in the same
        batched, parallel call as before.
        """
        limit = self.max_book_age if max_age is None else max_age
        out: dict[str, DepthBook] = {}
        misses: list[str] = []
        with self._lock:
            for ticker in tickers:
                entry = self._books.get(ticker)
                if entry is None:
                    misses.append(ticker)
                elif not entry.trusted:
                    self.stats.book_untrusted += 1
                    misses.append(ticker)
                elif not self._fresh(entry, limit):
                    self.stats.book_stale += 1
                    misses.append(ticker)
                else:
                    self._books.move_to_end(ticker)
                    self.stats.book_hits += 1
                    out[ticker] = entry.to_book()
            self.stats.book_misses += len(misses)

        if not misses or self.client is None:
            return out

        fetched = {}
        try:
            fetched = self.client.get_depths(misses, depth=depth, workers=workers)
        except AttributeError:
            for ticker in misses:
                book = self._fetch_book(ticker, depth)
                if book is not None:
                    fetched[ticker] = book
        except Exception as exc:  # noqa: BLE001
            self.stats.rest_errors += 1
            log.debug("STATE  bulk book read failed: %s", exc)
            return out

        for ticker, book in fetched.items():
            self.put_book(book, source=Source.REST)
            out[ticker] = book
        return out

    def _fetch_book(self, ticker: str, depth: int) -> DepthBook | None:
        """One book over REST, whichever method this client offers."""
        if self.client is None:
            return None
        for name in ("get_depth", "get_orderbook"):
            fn = getattr(self.client, name, None)
            if fn is None:
                continue
            try:
                return fn(ticker, depth=depth)
            except TypeError:
                try:
                    return fn(ticker)
                except Exception as exc:  # noqa: BLE001
                    self.stats.rest_errors += 1
                    log.debug("STATE  could not read book %s: %s", ticker, exc)
                    return None
            except Exception as exc:  # noqa: BLE001
                self.stats.rest_errors += 1
                log.debug("STATE  could not read book %s: %s", ticker, exc)
                return None
        return None

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------
    def put_book(self, book, source: str = Source.REST,
                 seq: int | None = None) -> None:
        """Replace a market's book wholesale. This is what makes it trusted again."""
        with self._lock:
            entry = BookEntry(
                ticker=book.ticker,
                yes={lv.price_cents: lv.contracts for lv in book.yes_bids if lv.contracts > 0},
                no={lv.price_cents: lv.contracts for lv in book.no_bids if lv.contracts > 0},
                source=source, seq=seq, trusted=True)
            self._books[book.ticker] = entry
            self._books.move_to_end(book.ticker)
            self._evict()

    def put_market(self, market: Market, source: str = Source.REST) -> None:
        with self._lock:
            self._markets[market.ticker] = MarketEntry(market, source=source)
            self._markets.move_to_end(market.ticker)
            self._evict()

    def apply_snapshot(self, ticker: str, yes_levels, no_levels,
                       seq: int | None = None) -> None:
        """A full book from the feed. Always accepted - a snapshot IS the truth.

        This is also the only way an untrusted entry becomes trusted again,
        which is why a gap asks for one.
        """
        with self._lock:
            self._books[ticker] = BookEntry(
                ticker=ticker,
                yes={int(p): int(q) for p, q in yes_levels if int(q) > 0},
                no={int(p): int(q) for p, q in no_levels if int(q) > 0},
                source=Source.WS, seq=seq, trusted=True)
            self._books.move_to_end(ticker)
            self.stats.snapshots += 1
            self._evict()

    def apply_delta(self, ticker: str, side: str, price_cents: int,
                    delta: int, seq: int | None = None) -> bool:
        """Apply one incremental change. Returns False if it could not be trusted.

        A False return is the signal to ask for a fresh snapshot.
        """
        side = side.lower()
        if side not in ("yes", "no"):
            log.debug("STATE  ignoring delta for unknown side %r", side)
            return False

        with self._lock:
            entry = self._books.get(ticker)
            if entry is None:
                # A delta with no snapshot behind it cannot be applied to
                # anything. Not an error - just not usable.
                return False

            if seq is not None and entry.seq is not None and seq != entry.seq + 1:
                if seq <= entry.seq:
                    # A repeat after a reconnect. Already applied; ignore it
                    # rather than counting it twice.
                    return True
                self._distrust(entry, f"sequence gap: expected {entry.seq + 1}, got {seq}")
                self.stats.gaps += 1
                return False

            levels = entry.yes if side == "yes" else entry.no
            new_qty = levels.get(price_cents, 0) + delta
            if new_qty > 0:
                levels[price_cents] = new_qty
            else:
                levels.pop(price_cents, None)
                if new_qty < 0:
                    # The book says less than nothing rests here, so our copy
                    # and theirs disagree. Clamping and carrying on would hide
                    # that; the whole point of the check is not to.
                    self._distrust(entry, f"{side} {price_cents}c went negative")
                    return False

            entry.seq = seq if seq is not None else entry.seq
            entry.updated_at = _now()
            entry.source = Source.WS
            self.stats.deltas += 1
            self._books.move_to_end(ticker)
            return True

    def _distrust(self, entry: BookEntry, why: str, quiet: bool = False) -> bool:
        was = entry.trusted
        if was and not quiet:
            log.warning("STATE  %s is no longer trusted - %s; falling back to "
                        "REST until a fresh snapshot arrives", entry.ticker, why)
        entry.trusted = False
        entry.untrusted_reason = why
        return was

    def invalidate(self, ticker: str, why: str = "invalidated") -> None:
        with self._lock:
            entry = self._books.get(ticker)
            if entry is not None:
                self._distrust(entry, why)

    def invalidate_all(self, why: str = "feed disconnected") -> None:
        """Called when the feed drops. Every cached book is now suspect.

        Deliberately NOT a clear(): the entries stay so a reconnect can see
        what to resubscribe to. They are simply not served any more.
        """
        with self._lock:
            n = sum(self._distrust(entry, why, quiet=True)
                    for entry in self._books.values())
        # One line, not one per book: a reconnect distrusts hundreds at once.
        if n:
            log.warning("STATE  %d cached books no longer trusted - %s; REST "
                        "until fresh snapshots arrive", n, why)

    def _evict(self) -> None:
        """Keep the store bounded. The exchange has 80,000 markets; we do not
        need all of them, and an unbounded cache in a process that runs for
        days is a memory leak with extra steps."""
        while len(self._books) > self.capacity:
            self._books.popitem(last=False)
            self.stats.evictions += 1
        while len(self._markets) > self.capacity:
            self._markets.popitem(last=False)
            self.stats.evictions += 1

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def entry(self, ticker: str) -> BookEntry | None:
        with self._lock:
            return self._books.get(ticker)

    def health(self) -> dict:
        with self._lock:
            now = _now()
            ages = [e.age_seconds(now) for e in self._books.values()]
            untrusted = [e.ticker for e in self._books.values() if not e.trusted]
            return {
                "books": len(self._books),
                "markets": len(self._markets),
                "untrusted": len(untrusted),
                "untrusted_tickers": untrusted[:10],
                "oldest_seconds": max(ages) if ages else 0.0,
                "median_seconds": sorted(ages)[len(ages) // 2] if ages else 0.0,
                "subscribed": len(self.subscribed),
                "hit_rate": self.stats.book_hit_rate,
            }

    def hot_tickers(self, limit: int = 500) -> list[str]:
        """The markets most recently looked at, newest first.

        Reads move an entry to the end of the store, so this is simply the
        working set - which is exactly what a feed should be following. The
        exchange has 80,000 markets and we care about the handful a strategy
        is actually pricing.
        """
        with self._lock:
            return list(reversed(list(self._books.keys())))[:limit]

    def __len__(self) -> int:
        with self._lock:
            return len(self._books)

    # ------------------------------------------------------------------
    def __getattr__(self, name: str):
        """Anything this store does not implement is the client's job.

        The store REPLACES the market client wherever books and markets are
        read, so it has to answer `exchange_status()`, `get_markets()` and the
        rest as well. Forwarding is explicit about that rather than requiring
        every call site to know which of two objects to ask.

        Only reached for attributes that do not exist here, so it can never
        shadow get_depth or get_market.
        """
        client = self.__dict__.get("client")
        if client is None:
            raise AttributeError(
                f"MarketState has no {name!r} and no client to forward it to")
        return getattr(client, name)
