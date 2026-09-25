"""Plain data objects. Kalshi speaks in integer CENTS; we keep it that way
internally and only convert to dollars at the edges."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


def parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    s = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Market:
    ticker: str
    event_ticker: str = ""
    series_ticker: str = ""
    title: str = ""
    subtitle: str = ""
    status: str = ""
    yes_bid: int = 0          # cents someone will PAY for YES
    yes_ask: int = 0          # cents you must PAY to BUY YES
    no_bid: int = 0
    no_ask: int = 0
    last_price: int = 0
    volume: int = 0
    open_interest: int = 0
    liquidity: int = 0
    close_time: datetime | None = None
    # Parsed strike information (filled in by the strategy, not the API)
    strike_type: str = ""
    floor_strike: float | None = None
    cap_strike: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Market":
        return cls(
            ticker=d.get("ticker", ""),
            event_ticker=d.get("event_ticker", ""),
            series_ticker=_series_of(d),
            title=d.get("title", "") or d.get("yes_sub_title", ""),
            subtitle=d.get("yes_sub_title", "") or d.get("subtitle", ""),
            status=d.get("status", ""),
            yes_bid=int(d.get("yes_bid") or 0),
            yes_ask=int(d.get("yes_ask") or 0),
            no_bid=int(d.get("no_bid") or 0),
            no_ask=int(d.get("no_ask") or 0),
            last_price=int(d.get("last_price") or 0),
            volume=int(d.get("volume") or 0),
            open_interest=int(d.get("open_interest") or 0),
            liquidity=int(d.get("liquidity") or 0),
            close_time=parse_ts(d.get("close_time")),
            strike_type=d.get("strike_type", "") or "",
            floor_strike=_fnum(d.get("floor_strike")),
            cap_strike=_fnum(d.get("cap_strike")),
            raw=d,
        )

    @property
    def spread_cents(self) -> int:
        if self.yes_ask and self.yes_bid:
            return max(0, self.yes_ask - self.yes_bid)
        return 99

    @property
    def mid_cents(self) -> float:
        if self.yes_ask and self.yes_bid:
            return (self.yes_ask + self.yes_bid) / 2
        return float(self.last_price or 0)

    def minutes_to_close(self, now: datetime | None = None) -> float:
        if not self.close_time:
            return float("inf")
        now = now or datetime.now(timezone.utc)
        return (self.close_time - now).total_seconds() / 60.0


def _series_of(d: dict[str, Any]) -> str:
    """The series a market belongs to, derived when the payload omits it.

    Live Kalshi market payloads carry `ticker` and `event_ticker` but no
    `series_ticker`. Taking the field at face value wrote an empty series on
    every forecast we logged, which made calibration-by-series - the report
    that would have caught Chicago being priced off the wrong station - group
    everything into one nameless bucket.

    A ticker looks like KXHIGHNY-26SEP17-T80, and an event like
    KXHIGHNY-26SEP17. The series is the part before the first dash.
    """
    explicit = str(d.get("series_ticker") or "").strip()
    if explicit:
        return explicit
    for key in ("event_ticker", "ticker"):
        value = str(d.get(key) or "")
        if "-" in value:
            return value.split("-", 1)[0]
        if value:
            return value
    return ""


def _fnum(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


@dataclass
class OrderbookLevel:
    price_cents: int
    contracts: int


@dataclass
class Orderbook:
    ticker: str
    yes: list[OrderbookLevel] = field(default_factory=list)  # resting YES bids
    no: list[OrderbookLevel] = field(default_factory=list)   # resting NO bids

    @classmethod
    def from_api(cls, ticker: str, d: dict[str, Any]) -> "Orderbook":
        book = d.get("orderbook", d) or {}

        def lv(rows):
            out = []
            for row in rows or []:
                if isinstance(row, (list, tuple)) and len(row) >= 2:
                    out.append(OrderbookLevel(int(row[0]), int(row[1])))
            return sorted(out, key=lambda x: -x.price_cents)

        return cls(ticker=ticker, yes=lv(book.get("yes")), no=lv(book.get("no")))

    def best_yes_bid(self) -> OrderbookLevel | None:
        return self.yes[0] if self.yes else None

    def best_no_bid(self) -> OrderbookLevel | None:
        return self.no[0] if self.no else None

    def yes_ask_depth(self, at_or_below_cents: int) -> int:
        """Contracts available to BUY YES at <= given price.

        A resting NO bid at price p is equivalent to a YES offer at (100 - p).
        """
        return sum(l.contracts for l in self.no if (100 - l.price_cents) <= at_or_below_cents)

    def no_ask_depth(self, at_or_below_cents: int) -> int:
        return sum(l.contracts for l in self.yes if (100 - l.price_cents) <= at_or_below_cents)

    def yes_bid_depth(self, at_or_above_cents: int) -> int:
        """Contracts you could SELL YES into at >= given price.

        Selling YES means hitting the resting YES bids, which is a different
        side of the book entirely from buying. Getting this backwards means a
        position you can enter but never exit.
        """
        return sum(l.contracts for l in self.yes if l.price_cents >= at_or_above_cents)

    def no_bid_depth(self, at_or_above_cents: int) -> int:
        return sum(l.contracts for l in self.no if l.price_cents >= at_or_above_cents)

    def depth_for(self, side: str, action: str, price_cents: int) -> int:
        """One entry point so callers cannot pick the wrong side by accident."""
        if action == "buy":
            return (self.yes_ask_depth(price_cents) if side == "yes"
                    else self.no_ask_depth(price_cents))
        return (self.yes_bid_depth(price_cents) if side == "yes"
                else self.no_bid_depth(price_cents))


def _money_cents(d: dict[str, Any], *keys) -> float | None:
    """Return CENTS from whichever key is present.

    A key ending in _dollars is in dollars; the older names were in cents.
    Mixing them up is a factor-of-100 error in a risk number.
    """
    for k in keys:
        v = _fnum(d.get(k))
        if v is not None:
            return v * 100.0 if k.endswith("_dollars") else v
    return None


@dataclass
class Position:
    ticker: str
    contracts: int = 0        # signed: >0 long YES, <0 long NO
    total_cost_cents: int = 0
    realized_pnl_cents: int = 0
    resting_orders: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Position":
        """Read a position row in whatever shape the exchange sent it.

        Kalshi moved this endpoint to fixed-point counts and DOLLAR amounts,
        the same way it moved orders:

            old   position (int contracts), market_exposure / realized_pnl (CENTS)
            new   position_fp ("25.00"),    market_exposure_dollars, realized_pnl_dollars

        Reading only the old names returned 0 for every position, so the bot
        reported "expected +25, exchange says +0" on trades that had actually
        filled. It could not see what it owned.
        """
        contracts = _fnum(d.get("position_fp"))
        if contracts is None:
            contracts = _fnum(d.get("position"))

        exposure_cents = _money_cents(d, "market_exposure_dollars",
                                      "market_exposure", "total_traded_dollars",
                                      "total_traded")
        pnl_cents = _money_cents(d, "realized_pnl_dollars", "realized_pnl")

        if contracts is None and d.get("ticker"):
            log.warning("position row for %s carries no readable contract "
                        "count; keys were %s", d.get("ticker"), sorted(d))
        return cls(
            ticker=d.get("ticker", ""),
            contracts=int(round(contracts)) if contracts is not None else 0,
            total_cost_cents=abs(int(round(exposure_cents or 0))),
            realized_pnl_cents=int(round(pnl_cents or 0)),
            resting_orders=int(_fnum(d.get("resting_orders_count")) or 0),
            raw=d,
        )

    @property
    def side(self) -> str:
        return "yes" if self.contracts > 0 else ("no" if self.contracts < 0 else "flat")

    @property
    def avg_cost_cents(self) -> float:
        n = abs(self.contracts)
        return (self.total_cost_cents / n) if n else 0.0


@dataclass
class TradeIntent:
    """What the strategy wants to do, before risk approval."""
    ticker: str
    side: str                 # "yes" | "no"
    action: str               # "buy" | "sell"
    limit_price_cents: int
    fair_probability: float   # our estimate for the YES outcome
    confidence: float
    edge_after_fees: float
    rationale: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class SizedOrder:
    intent: TradeIntent
    contracts: int
    est_cost_usd: float
    est_fee_usd: float
    est_ev_usd: float
    notes: list[str] = field(default_factory=list)
