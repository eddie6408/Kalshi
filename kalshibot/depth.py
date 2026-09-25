"""Order book depth. What a trade ACTUALLY costs, not what the top of book
advertises.

Kalshi's orderbook endpoint returns two lists of RESTING BIDS:

    {"orderbook": {"yes": [[price, qty], ...],   <- people bidding for YES
                   "no":  [[price, qty], ...]}}  <- people bidding for NO

There is no separate "ask" list, because on a binary market an ask is just a
bid on the other side:

    a resting NO bid at 38c  ==  YES offered at 62c
    a resting YES bid at 60c ==  NO  offered at 40c

So all four executable ladders are derived from those two lists:

    BUY  YES  -> consume NO bids,  cheapest-YES-equivalent first
    BUY  NO   -> consume YES bids, cheapest-NO-equivalent first
    SELL YES  -> consume YES bids, highest first
    SELL NO   -> consume NO bids,  highest first

Getting this wrong is the difference between an arbitrage and a loss, so
every conversion is done in exactly one place: here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True)
class Level:
    price_cents: int
    contracts: int


@dataclass
class Walk:
    """Result of consuming the book for a requested quantity."""
    requested: int
    filled: int
    total_cost_cents: int
    levels: list[Level] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.filled >= self.requested and self.requested > 0

    @property
    def vwap_cents(self) -> float:
        return (self.total_cost_cents / self.filled) if self.filled else 0.0

    @property
    def total_cost_usd(self) -> float:
        return self.total_cost_cents / 100.0

    @property
    def worst_price_cents(self) -> int:
        return max((l.price_cents for l in self.levels), default=0)


class DepthBook:
    """Executable view of one market's order book."""

    #: which response shape this book was parsed from ("dollars"|"cents"|"empty")
    source_format: str = "unknown"

    def __init__(self, ticker: str, yes_bids: Iterable[Level], no_bids: Iterable[Level]):
        self.ticker = ticker
        self.yes_bids = sorted(yes_bids, key=lambda l: -l.price_cents)
        self.no_bids = sorted(no_bids, key=lambda l: -l.price_cents)
        # Derived offer ladders, cheapest first.
        self.yes_asks = sorted(
            (Level(100 - l.price_cents, l.contracts) for l in self.no_bids),
            key=lambda l: l.price_cents)
        self.no_asks = sorted(
            (Level(100 - l.price_cents, l.contracts) for l in self.yes_bids),
            key=lambda l: l.price_cents)

    # ---------------------------------------------------------------
    @classmethod
    def from_api(cls, ticker: str, payload: dict[str, Any]) -> "DepthBook":
        """Parse whichever orderbook shape this Kalshi deployment returns.

        Kalshi has shipped more than one format and both are still seen:

          legacy   {"orderbook": {"yes": [[42, 13], ...],
                                  "no":  [[56, 17], ...]}}
                   prices are INTEGER CENTS, quantities are integers

          current  {"orderbook_fp": {"yes_dollars": [["0.4200", "13.00"], ...],
                                     "no_dollars":  [["0.5600", "17.00"], ...]}}
                   prices are DOLLAR STRINGS, quantities are fixed-point STRINGS

        Parsing only the legacy shape against a current deployment yields an
        empty book for every market, which looks exactly like a dead exchange.
        It is not. Both are handled here, and `source_format` records which one
        was actually found so a diagnostic can say so out loud.
        """
        book = None
        fmt = "unknown"
        for key, yes_key, no_key, scale in (
            ("orderbook_fp", "yes_dollars", "no_dollars", "dollars"),
            ("orderbook", "yes", "no", "cents"),
            ("orderbook", "yes_dollars", "no_dollars", "dollars"),
        ):
            node = payload.get(key)
            if isinstance(node, dict) and (yes_key in node or no_key in node):
                # Recognised the SHAPE. The arrays may still be empty, which
                # means a quiet market - not a parsing failure. Conflating the
                # two sends you hunting a bug that is not there.
                book, fmt = node, scale
                yk, nk = yes_key, no_key
                break

        if book is None:
            # Some responses put the arrays at the top level.
            for yes_key, no_key, scale in (("yes_dollars", "no_dollars", "dollars"),
                                           ("yes", "no", "cents")):
                if yes_key in payload or no_key in payload:
                    book, fmt, yk, nk = payload, scale, yes_key, no_key
                    break

        if book is None:
            out = cls(ticker, [], [])
            out.source_format = "unrecognised"
            return out

        out = cls(ticker, cls._levels(book.get(yk), fmt),
                  cls._levels(book.get(nk), fmt))
        # Shape understood. Say so, and say separately whether anyone is
        # actually quoting.
        out.source_format = fmt if (out.yes_bids or out.no_bids) else f"{fmt}-empty"
        return out

    @staticmethod
    def _levels(rows, scale: str) -> list["Level"]:
        """One row -> one Level, in integer cents, whatever came in."""
        out: list[Level] = []
        for row in rows or []:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            try:
                raw_price, raw_qty = row[0], row[1]
                if scale == "dollars":
                    price = int(round(float(raw_price) * 100))
                else:
                    price = int(round(float(raw_price)))
                qty = int(float(raw_qty))
            except (TypeError, ValueError):
                continue
            if 0 < price < 100 and qty > 0:
                out.append(Level(price, qty))
        return out

    @classmethod
    def empty(cls, ticker: str) -> "DepthBook":
        return cls(ticker, [], [])

    # ---------------------------------------------------------------
    def ladder(self, side: str, action: str) -> list[Level]:
        side, action = side.lower(), action.lower()
        if action == "buy":
            return self.yes_asks if side == "yes" else self.no_asks
        return self.yes_bids if side == "yes" else self.no_bids

    def best_price(self, side: str, action: str) -> int | None:
        rungs = self.ladder(side, action)
        return rungs[0].price_cents if rungs else None

    def total_size(self, side: str, action: str, limit_price: int | None = None) -> int:
        rungs = self.ladder(side, action)
        if limit_price is None:
            return sum(l.contracts for l in rungs)
        if action == "buy":
            return sum(l.contracts for l in rungs if l.price_cents <= limit_price)
        return sum(l.contracts for l in rungs if l.price_cents >= limit_price)

    # ---------------------------------------------------------------
    def walk(self, side: str, action: str, quantity: int,
             limit_price: int | None = None) -> Walk:
        """Consume the book for `quantity` contracts and report the true cost.

        For a buy, `limit_price` is the most we will pay per contract; levels
        above it are not consumed. For a sell it is the least we will accept.
        """
        if quantity <= 0:
            return Walk(requested=max(0, quantity), filled=0, total_cost_cents=0)

        remaining = quantity
        cost = 0
        taken: list[Level] = []
        for rung in self.ladder(side, action):
            if limit_price is not None:
                if action == "buy" and rung.price_cents > limit_price:
                    break
                if action == "sell" and rung.price_cents < limit_price:
                    break
            take = min(remaining, rung.contracts)
            if take <= 0:
                continue
            taken.append(Level(rung.price_cents, take))
            cost += take * rung.price_cents
            remaining -= take
            if remaining == 0:
                break

        return Walk(requested=quantity, filled=quantity - remaining,
                    total_cost_cents=cost, levels=taken)

    def cost_to_buy(self, side: str, quantity: int,
                    limit_price: int | None = None) -> Walk:
        return self.walk(side, "buy", quantity, limit_price)

    def max_fillable(self, side: str, action: str,
                     limit_price: int | None = None) -> int:
        return self.total_size(side, action, limit_price)

    def __repr__(self) -> str:
        return (f"DepthBook({self.ticker}, yes_ask={self.best_price('yes','buy')}, "
                f"no_ask={self.best_price('no','buy')})")
