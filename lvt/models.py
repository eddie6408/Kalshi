"""Core data types.

Price conventions (read this before touching anything that trades):

* All prices are CENTS as floats (45.0 = $0.45). Kalshi now quotes dollars as
  fixed-point strings and some books tick in tenths of a cent, so floats.
* Every contract is YES or NO. The exchange book is one YES book:
      yes bid  = best YES bid
      yes ask  = 100 - best NO bid
      no  bid  = 100 - yes ask
      no  ask  = 100 - yes bid
* "Side-relative" price means the price of the side we hold or would buy.
  Downside exposure on YES is expressed by BUYING NO - Kalshi has no short
  sale, and a live order must correspond exactly to what the exchange offers.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

YES, NO = "yes", "no"
SIDES = (YES, NO)


def other(side: str) -> str:
    return NO if side == YES else YES


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def dollars_to_cents(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return round(float(v) * 100.0, 4)
    except (TypeError, ValueError):
        return None


def fnum(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


SPORT_PREFIXES = [
    # longest first; matched against the series ticker
    ("KXATPCHALLENGER", "TENNIS"), ("KXATP", "TENNIS"), ("KXWTA", "TENNIS"),
    ("KXITF", "TENNIS"), ("KXTENNIS", "TENNIS"), ("KXUSOPEN", "TENNIS"),
    ("KXWIMBLEDON", "TENNIS"), ("KXFRENCHOPEN", "TENNIS"), ("KXAUSOPEN", "TENNIS"),
    ("KXNBA", "BASKETBALL"), ("KXWNBA", "BASKETBALL"), ("KXNCAAMB", "BASKETBALL"),
    ("KXNCAAWB", "BASKETBALL"), ("KXEUROLEAGUE", "BASKETBALL"), ("KXBASKETBALL", "BASKETBALL"),
    ("KXNFL", "FOOTBALL"), ("KXNCAAF", "FOOTBALL"), ("KXCFB", "FOOTBALL"), ("KXSB", "FOOTBALL"),
    ("KXHEISMAN", "FOOTBALL"), ("KXCFL", "FOOTBALL"),
    ("KXMLB", "BASEBALL"), ("KXNPB", "BASEBALL"), ("KXKBO", "BASEBALL"), ("KXBASEBALL", "BASEBALL"),
    ("KXEPL", "SOCCER"), ("KXLALIGA", "SOCCER"), ("KXSERIEA", "SOCCER"), ("KXBUNDESLIGA", "SOCCER"),
    ("KXLIGUE1", "SOCCER"), ("KXUCL", "SOCCER"), ("KXUEL", "SOCCER"), ("KXUEFA", "SOCCER"),
    ("KXMLS", "SOCCER"), ("KXSOCCER", "SOCCER"), ("KXINTLFRIENDLY", "SOCCER"), ("KXFIFA", "SOCCER"),
    ("KXWC", "SOCCER"), ("KXEREDIVISIE", "SOCCER"), ("KXLIGAMX", "SOCCER"),
    ("KXNHL", "HOCKEY"), ("KXKHL", "HOCKEY"),
    ("KXODI", "CRICKET"), ("KXWODI", "CRICKET"), ("KXT20", "CRICKET"), ("KXIPL", "CRICKET"), ("KXCRICKET", "CRICKET"),
    ("KXUFC", "MMA"), ("KXMMA", "MMA"), ("KXBOXING", "BOXING"),
    ("KXPGA", "GOLF"), ("KXDPWORLDTOUR", "GOLF"), ("KXPRESCUP", "GOLF"), ("KXLIV", "GOLF"), ("KXRYDER", "GOLF"),
    ("KXNASCAR", "MOTORSPORT"), ("KXF1", "MOTORSPORT"), ("KXINDYCAR", "MOTORSPORT"),
    ("KXVALORANT", "ESPORTS"), ("KXCS2", "ESPORTS"), ("KXLOL", "ESPORTS"), ("KXDOTA", "ESPORTS"),
]

# Sports the spec asks us to compare (section 47); everything else is OTHER.
REPORT_SPORTS = ("TENNIS", "BASKETBALL", "FOOTBALL", "BASEBALL", "SOCCER")


def classify_sport(series_ticker: str, category: str) -> str:
    s = (series_ticker or "").upper()
    for prefix, sport in sorted(SPORT_PREFIXES, key=lambda x: -len(x[0])):
        if s.startswith(prefix):
            return sport
    if (category or "").lower() == "sports":
        return "OTHER_SPORT"
    return "NON_SPORT"


def report_bucket(sport: str) -> str:
    return sport if sport in REPORT_SPORTS else "OTHER"


@dataclass
class MarketInfo:
    ticker: str
    event_ticker: str = ""
    series_ticker: str = ""
    category: str = ""
    sport: str = "NON_SPORT"
    title: str = ""
    sub_title: str = ""
    close_ts: float = 0.0
    occurrence_ts: float = 0.0
    status: str = "active"
    volume_24h: float = 0.0
    fee_type: str = "quadratic"
    fee_multiplier: float = 1.0
    can_close_early: bool = True
    result: str = ""


@dataclass
class Quote:
    """Top of book + activity for one market, as fetched."""
    ticker: str
    ts: float
    yes_bid: Optional[float]
    yes_ask: Optional[float]
    yes_bid_size: float = 0.0
    yes_ask_size: float = 0.0
    last: Optional[float] = None
    volume: float = 0.0
    open_interest: float = 0.0
    status: str = "active"
    result: str = ""

    def key(self) -> tuple:
        return (self.yes_bid, self.yes_ask, self.yes_bid_size, self.yes_ask_size,
                self.last, self.volume, self.status)


@dataclass
class Depth:
    """Order book ladders, best price first. yes = YES bids, no = NO bids."""
    ticker: str
    ts: float
    yes: list[tuple[float, float]] = field(default_factory=list)
    no: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class Snapshot:
    ticker: str
    ts: float                         # when WE observed it (fetch completed)
    quote: Quote
    depth: Optional[Depth] = None     # only attached while fresh

    @property
    def yes_bid(self): return self.quote.yes_bid

    @property
    def yes_ask(self): return self.quote.yes_ask

    def bid(self, side: str) -> Optional[float]:
        q = self.quote
        if side == YES:
            return q.yes_bid
        return None if q.yes_ask is None else round(100.0 - q.yes_ask, 4)

    def ask(self, side: str) -> Optional[float]:
        q = self.quote
        if side == YES:
            return q.yes_ask
        return None if q.yes_bid is None else round(100.0 - q.yes_bid, 4)

    def mid(self, side: str = YES) -> Optional[float]:
        b, a = self.bid(side), self.ask(side)
        if b is None or a is None:
            return None
        return (b + a) / 2.0

    def spread(self) -> Optional[float]:
        q = self.quote
        if q.yes_bid is None or q.yes_ask is None:
            return None
        return round(q.yes_ask - q.yes_bid, 4)

    def bid_size(self, side: str) -> float:
        return self.quote.yes_bid_size if side == YES else self.quote.yes_ask_size

    def ask_size(self, side: str) -> float:
        return self.quote.yes_ask_size if side == YES else self.quote.yes_bid_size

    # -- ladders a taker would trade against --------------------------------
    def ask_levels(self, side: str, max_depth_age: float) -> list[tuple[float, float]]:
        """Prices/sizes a BUYER of `side` can lift, best (lowest) first."""
        d = self.depth if (self.depth and self.ts - self.depth.ts <= max_depth_age) else None
        if d is not None:
            src = d.no if side == YES else d.yes
            return [(round(100.0 - p, 4), s) for p, s in src if s > 0]
        a = self.ask(side)
        return [(a, self.ask_size(side))] if a is not None else []

    def bid_levels(self, side: str, max_depth_age: float) -> list[tuple[float, float]]:
        """Prices/sizes a SELLER of `side` can hit, best (highest) first."""
        d = self.depth if (self.depth and self.ts - self.depth.ts <= max_depth_age) else None
        if d is not None:
            src = d.yes if side == YES else d.no
            return [(p, s) for p, s in src if s > 0]
        b = self.bid(side)
        return [(b, self.bid_size(side))] if b is not None else []

    def liquidity_usd(self) -> float:
        """Notional resting at the touch on the thinner side."""
        q = self.quote
        if q.yes_bid is None or q.yes_ask is None:
            return 0.0
        bid_side = q.yes_bid_size * q.yes_bid / 100.0
        ask_side = q.yes_ask_size * (100.0 - q.yes_ask) / 100.0
        return min(bid_side, ask_side)


@dataclass
class Signal:
    id: str
    ts: float
    ticker: str
    event_ticker: str
    strategy: str
    strategy_version: str
    side: str
    action: str                       # BUY | WATCH | NO_TRADE
    stage: str                        # SETUP | CONFIRMED | REJECTED_PATTERN
    score: float = 0.0
    bid: Optional[float] = None       # side-relative
    ask: Optional[float] = None
    mid: Optional[float] = None
    spread: Optional[float] = None
    target: Optional[float] = None    # side-relative BID we aim to exit at
    stop: Optional[float] = None      # side-relative BID that kills the thesis
    expected_net_cents: Optional[float] = None
    reward_risk: Optional[float] = None
    state: str = ""
    regime: str = ""
    components: dict = field(default_factory=dict)
    explanation: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    anchor: str = ""                  # identifies the pattern instance (dedupe)
    data_ts: float = 0.0
    category: str = ""
    sport: str = ""
    decision: str = ""                # TAKEN | REJECTED | WATCH
    reject_reason: str = ""

    def text(self) -> str:
        e = self.explanation
        lines = [
            f"STRATEGY: {self.strategy} ({self.strategy_version})",
            f"MARKET: {self.ticker} [{self.sport or self.category}]",
            f"SIDE: BUY {self.side.upper()}",
            f"PRICE: bid {_c(self.bid)} / ask {_c(self.ask)}  spread {_c(self.spread)}",
        ]
        for k in ("recent_high", "recent_low", "decline_pct", "reversal_pct", "rise_cents",
                  "velocity", "volatility", "liquidity", "volume"):
            if k in e:
                lines.append(f"{k.upper().replace('_', ' ')}: {e[k]}")
        lines += [
            f"TARGET: {_c(self.target)}  STOP: {_c(self.stop)}  "
            f"EXPECTED NET: {_c(self.expected_net_cents)}/contract  R:R {self.reward_risk}",
            f"STATE: {self.state}  REGIME: {self.regime}",
            f"SIGNAL SCORE: {self.score:.0f}",
            f"ACTION: {self.action}",
            "REASON: " + "; ".join(self.reasons),
        ]
        return "\n".join(lines)


def _c(v) -> str:
    return "-" if v is None else f"{v:.1f}c"


@dataclass
class OrderRequest:
    client_order_id: str
    ticker: str
    event_ticker: str
    action: str                       # buy | sell
    side: str                         # yes | no
    count: int
    limit_price: float                # side-relative cents
    purpose: str                      # ENTRY | EXIT | TAKE_PROFIT
    ref_price: float                  # side ask (buy) / bid (sell) at decision time
    created_ts: float
    position_id: str = ""
    signal_id: str = ""
    exit_reason: str = ""
    data_ts: float = 0.0
    signal_ts: float = 0.0
    event_ts: float = 0.0
    strategy: str = ""
    # maker orders: post_only never crosses; GTC rests until filled, cancelled
    # or expire_ts (bot-managed expiry). Taker orders are IOC.
    post_only: bool = False
    tif: str = "IOC"                  # IOC | GTC
    expire_ts: float = 0.0

    @property
    def is_maker(self) -> bool:
        return self.post_only


@dataclass
class Trade:
    """One public trade print. taker_side is the side the TAKER bought."""
    ticker: str
    ts: float
    yes_price: float                  # cents
    count: float
    taker_side: str                   # yes | no
    trade_id: str = ""

    def side_price(self, side: str) -> float:
        return self.yes_price if side == YES else 100.0 - self.yes_price


@dataclass
class FillEvent:
    client_order_id: str
    ts: float
    price: float                      # side-relative cents
    count: int
    fee_usd: float
    exchange_fill_id: str = ""


@dataclass
class OrderUpdate:
    """What a venue reports back about an order."""
    client_order_id: str
    status: str                       # ACKED | PARTIALLY_FILLED (still working) | FILLED | PARTIAL | NO_FILL
    #                                   | REJECTED | CANCELLED | UNKNOWN
    ts: float
    fills: list[FillEvent] = field(default_factory=list)
    exchange_order_id: str = ""
    reason: str = ""


TERMINAL_ORDER_STATES = ("FILLED", "PARTIAL", "NO_FILL", "REJECTED", "CANCELLED")


def now() -> float:
    return time.time()
