"""Core domain types.

Price convention: all prices are CENTS (float, 0-100). A Kalshi binary market has
one YES price; the NO price is 100 - YES. Positions are always *long* a side:

    direction UP   -> long YES  (profits when the YES price rises)
    direction DOWN -> long NO   (profits when the YES price falls)

Buying NO is Kalshi's real, supported mechanism for downside exposure. There is
no naked short selling of YES; you can only sell YES you hold. The bot never
simulates anything else in SHADOW/LIVE.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class Direction(str, Enum):
    UP = "UP"
    DOWN = "DOWN"

    @property
    def side(self) -> str:
        return "yes" if self is Direction.UP else "no"


class Env(str, Enum):
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    LIVE = "LIVE"
    BACKTEST = "BACKTEST"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def now() -> float:
    return time.time()


@dataclass
class BookLevel:
    price: float  # cents
    size: float   # contracts


@dataclass
class OrderBook:
    """Kalshi returns only bids for each side. YES asks are implied by NO bids."""
    ts: float
    yes_bids: list[BookLevel] = field(default_factory=list)  # best first (highest)
    no_bids: list[BookLevel] = field(default_factory=list)   # best first (highest)

    def best_yes_bid(self) -> BookLevel | None:
        return self.yes_bids[0] if self.yes_bids else None

    def best_yes_ask(self) -> BookLevel | None:
        if not self.no_bids:
            return None
        b = self.no_bids[0]
        return BookLevel(round(100 - b.price, 4), b.size)

    def asks_for(self, side: str) -> list[BookLevel]:
        """Levels you can BUY `side` from, best (cheapest) first."""
        opp = self.no_bids if side == "yes" else self.yes_bids
        return [BookLevel(round(100 - lv.price, 4), lv.size) for lv in opp]

    def bids_for(self, side: str) -> list[BookLevel]:
        """Levels you can SELL `side` into, best (highest) first."""
        return list(self.yes_bids if side == "yes" else self.no_bids)

    def depth_within(self, levels: list[BookLevel], best: float, window: float) -> float:
        return sum(lv.size for lv in levels if abs(lv.price - best) <= window + 1e-9)


@dataclass
class MarketMeta:
    ticker: str
    event_ticker: str = ""
    series_ticker: str = ""
    title: str = ""
    category: str = ""
    sport: str = "OTHER"
    market_type: str = "binary"
    status: str = ""
    close_ts: float | None = None
    expected_end_ts: float | None = None   # expected_expiration_time (e.g. scheduled game end)
    fee_type: str | None = None
    fee_multiplier: float | None = None
    fee_waiver_until: float | None = None
    tick_size: float = 1.0
    is_multivariate: bool = False

    def effective_end_ts(self) -> float | None:
        """Earliest of close time and expected expiration: trading can stop at either."""
        ts = [t for t in (self.close_ts, self.expected_end_ts) if t]
        return min(ts) if ts else None


@dataclass
class MarketSnapshot:
    ticker: str
    ts: float                     # local receipt time (the only clock the strategy may use)
    yes_bid: float | None
    yes_ask: float | None
    last_price: float | None = None
    volume: float | None = None   # cumulative contracts traded
    volume_24h: float | None = None
    open_interest: float | None = None
    yes_bid_size: float | None = None
    yes_ask_size: float | None = None
    status: str = "active"
    source: str = "kalshi_rest"
    exchange_ts: float | None = None
    book: OrderBook | None = None

    @property
    def mid(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return self.last_price
        return (self.yes_bid + self.yes_ask) / 2.0

    @property
    def spread(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.yes_bid

    def bid_for(self, side: str) -> float | None:
        if side == "yes":
            return self.yes_bid
        return None if self.yes_ask is None else round(100 - self.yes_ask, 4)

    def ask_for(self, side: str) -> float | None:
        if side == "yes":
            return self.yes_ask
        return None if self.yes_bid is None else round(100 - self.yes_bid, 4)

    def ask_size_for(self, side: str) -> float | None:
        return self.yes_ask_size if side == "yes" else self.yes_bid_size

    def bid_size_for(self, side: str) -> float | None:
        return self.yes_bid_size if side == "yes" else self.yes_ask_size


@dataclass
class TradePrint:
    ticker: str
    ts: float
    yes_price: float
    count: float
    taker_side: str = ""
    trade_id: str = ""


@dataclass
class SportsContext:
    event_ticker: str
    sport: str
    ts: float
    status: str = ""             # e.g. pre, live, final
    competitors: list[str] = field(default_factory=list)
    score: dict[str, Any] = field(default_factory=dict)
    period: str = ""
    clock: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    source: str = ""


@dataclass
class Signal:
    ts: float
    ticker: str
    strategy: str                # DIP | MOMENTUM | REVERSAL | DOWNTREND
    strategy_version: str
    direction: Direction
    action: str                  # BUY | NO_TRADE
    entry_ref: float             # side-space ask at signal time (cents)
    target: float                # side-space
    stop: float                  # side-space
    score: float
    expected_gross: float        # cents per contract
    expected_costs: float        # cents per contract
    expected_net: float
    reasons: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    score_components: dict[str, float] = field(default_factory=dict)
    max_hold_seconds: float = 900
    entry_style: str = "taker"
    id: str = field(default_factory=lambda: new_id("sig"))
    accepted: bool | None = None
    reject_reasons: list[str] = field(default_factory=list)

    @property
    def side(self) -> str:
        return self.direction.side

    def explanation(self, market_label: str = "") -> str:
        d = self.details
        lines = [
            f"STRATEGY: {self.strategy} ({self.strategy_version})",
            f"MARKET: {market_label or self.ticker}",
            f"SIDE: {self.side.upper()} ({self.direction.value})",
            f"PRICE: {self.entry_ref:.1f}c",
        ]
        for k in ("recent_high", "recent_low", "decline_pct", "rebound_cents", "move_cents",
                  "velocity", "volatility_regime", "spread", "liquidity"):
            if k in d:
                v = d[k]
                lines.append(f"{k.upper()}: {v:.2f}" if isinstance(v, float) else f"{k.upper()}: {v}")
        lines += [
            f"EXPECTED GROSS/COSTS/NET: {self.expected_gross:.1f}c / {self.expected_costs:.1f}c / {self.expected_net:.1f}c",
            f"SIGNAL SCORE: {self.score:.0f}",
            f"ACTION: {self.action}",
            "REASON: " + "; ".join(self.reasons),
        ]
        if self.reject_reasons:
            lines.append("REJECTED: " + "; ".join(self.reject_reasons))
        return "\n".join(lines)

    def to_record(self) -> dict[str, Any]:
        r = asdict(self)
        r["direction"] = self.direction.value
        r["side"] = self.side
        return r


class OrderStatus(str, Enum):
    PENDING_SUBMIT = "PENDING_SUBMIT"   # persisted intent, not yet sent (duplicate-order guard)
    SUBMITTED = "SUBMITTED"             # sent, not yet acknowledged
    RESTING = "RESTING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"               # IOC remainder / TTL / manual
    REJECTED = "REJECTED"
    MISSED = "MISSED"                   # paper: zero fill because price moved away
    UNKNOWN = "UNKNOWN"                 # live: state could not be confirmed -> fail closed

    @property
    def terminal(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.MISSED)


@dataclass
class Order:
    env: str
    ticker: str
    side: str            # yes | no
    action: str          # buy | sell
    count: int
    limit_price: float   # side-space cents
    purpose: str         # ENTRY | EXIT
    style: str = "taker"  # taker (IOC) | maker (post-only resting)
    reason: str = ""
    signal_id: str | None = None
    position_id: str | None = None
    ref_price: float | None = None  # side-space reference price at decision time (for slippage)
    id: str = field(default_factory=lambda: new_id("ord"))
    client_order_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    exchange_order_id: str | None = None
    status: OrderStatus = OrderStatus.PENDING_SUBMIT
    filled: int = 0
    avg_fill_price: float | None = None
    fees: float = 0.0          # cents
    reject_reason: str = ""
    expires_ts: float | None = None
    # latency chain
    event_ts: float | None = None
    data_ts: float | None = None
    signal_ts: float | None = None
    created_ts: float = field(default_factory=now)
    submitted_ts: float | None = None
    ack_ts: float | None = None
    first_fill_ts: float | None = None
    final_ts: float | None = None
    payload: dict[str, Any] | None = None

    @property
    def remaining(self) -> int:
        return self.count - self.filled


@dataclass
class Fill:
    order_id: str
    env: str
    ticker: str
    side: str
    action: str
    count: int
    price: float       # side-space cents
    fee: float         # cents (total for this fill)
    is_taker: bool
    ts: float
    ref_price: float | None = None
    id: str = field(default_factory=lambda: new_id("fil"))
    exchange_fill_id: str | None = None

    @property
    def slippage(self) -> float:
        """Adverse cents per contract versus the decision reference price."""
        if self.ref_price is None:
            return 0.0
        return (self.price - self.ref_price) if self.action == "buy" else (self.ref_price - self.price)


@dataclass
class Position:
    env: str
    ticker: str
    event_ticker: str
    side: str
    direction: Direction
    strategy: str
    strategy_version: str
    signal_id: str | None
    target: float
    stop: float
    max_hold_seconds: float
    entry_reason: str = ""
    signal_score: float = 0.0
    sport: str = "OTHER"
    category: str = ""
    regime: str = ""
    id: str = field(default_factory=lambda: new_id("pos"))
    qty: int = 0
    avg_entry: float = 0.0
    entry_cost: float = 0.0        # cents, sum(price*count) of buys
    exit_proceeds: float = 0.0     # cents, sum(price*count) of sells
    bought: int = 0
    sold: int = 0
    fees: float = 0.0              # cents
    slippage_cost: float = 0.0     # cents (total adverse slippage across fills)
    status: str = "OPENING"        # OPENING | OPEN | CLOSING | CLOSED
    opened_ts: float = field(default_factory=now)
    closed_ts: float | None = None
    exit_reason: str = ""
    peak_mark: float | None = None     # best side-space bid seen
    trough_mark: float | None = None   # worst side-space bid seen
    last_mark: float | None = None
    entry_features: dict[str, Any] = field(default_factory=dict)

    def apply_fill(self, fill: Fill) -> None:
        if fill.action == "buy":
            self.entry_cost += fill.price * fill.count
            self.bought += fill.count
            self.qty += fill.count
            self.avg_entry = self.entry_cost / self.bought if self.bought else 0.0
        else:
            self.exit_proceeds += fill.price * fill.count
            self.sold += fill.count
            self.qty -= fill.count
        self.fees += fill.fee
        self.slippage_cost += fill.slippage * fill.count

    @property
    def realized_gross(self) -> float:
        """Cents realized on the contracts already sold (gross of fees)."""
        return self.exit_proceeds - self.avg_entry * self.sold

    def unrealized(self, bid: float | None) -> float:
        if bid is None or self.qty <= 0:
            return 0.0
        return (bid - self.avg_entry) * self.qty

    def net_pnl(self, bid: float | None = None) -> float:
        return self.realized_gross + self.unrealized(bid) - self.fees
