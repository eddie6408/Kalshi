"""Paper fill model for resting (maker, post-only) orders.

A resting order sits at price P on one side of the book behind everyone who
was already there. It is only ever filled by evidence that a real trade
would have reached it:

1. ARRIVAL. On the first snapshot after the simulated latency, a post-only
   order that would cross (buy at/above the side ask, sell at/below the side
   bid) is REJECTED, exactly as the exchange would. Otherwise the queue ahead
   of us is the displayed size at P (0 if we improved the price).

2. TRADE PRINTS (public trade feed, the accurate path). A trade where the
   taker hit our side:
     resting BUY  on side S: taker bought the OTHER side, at S-price <= P
     resting SELL on side S: taker bought S,               at S-price >= P
   A trade strictly through P fills the whole remainder at P (everything at
   P, us included, was consumed). A trade AT P first consumes the queue ahead
   of us, and only the excess fills us. Cancellations ahead of us are never
   assumed - the queue only shrinks through trades, which is conservative.

3. TOUCH-THROUGH (snapshot). If the opposite best price reaches P (side ask
   <= P for a buy, side bid >= P for a sell) the level at P must have been
   emptied by trading - the book cannot be locked - so the remainder fills.

4. FALLBACK when no trade feed exists for the market (e.g. tape recorded
   before the feed was added): a rise in cumulative volume while the last
   trade printed on our side of P is treated as trades at that price, with
   only `volume_attribution` of the volume counted (volume also includes
   trades on the other side of the book).

Fills are charged the MAKER fee (0 on plain "quadratic" series). Adverse
selection is captured naturally: the fastest fills are the ones where price
moves through us.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from lvt.fees import fee_usd
from lvt.models import FillEvent, MarketInfo, OrderRequest, OrderUpdate, Snapshot, Trade, other


@dataclass
class RestingOrder:
    req: OrderRequest
    arrival_ts: float
    active: bool = False
    queue_ahead: float = 0.0
    remaining: int = 0
    filled: int = 0
    last_volume: float | None = None
    last_trade_ts: float = 0.0
    seen_trades: set = field(default_factory=set)

    @property
    def price(self) -> float:
        return self.req.limit_price


def displayed_ahead(order: RestingOrder, snap: Snapshot, depth_max_age: float) -> float:
    """Contracts resting at our price (or better) when we join."""
    req, P = order.req, order.price
    if req.action == "buy":
        levels = snap.bid_levels(req.side, depth_max_age)
        return sum(s for p, s in levels if p >= P - 1e-9)
    levels = snap.ask_levels(req.side, depth_max_age)
    return sum(s for p, s in levels if p <= P + 1e-9)


def activate(order: RestingOrder, snap: Snapshot, depth_max_age: float) -> OrderUpdate | None:
    """Arrival at the exchange. Returns a REJECTED update, or None if resting."""
    req, P = order.req, order.price
    if snap.quote.status and snap.quote.status != "active":
        return OrderUpdate(req.client_order_id, "REJECTED", snap.ts, reason=f"market status {snap.quote.status}")
    if req.action == "buy":
        ask = snap.ask(req.side)
        if ask is not None and ask <= P + 1e-9:
            return OrderUpdate(req.client_order_id, "REJECTED", snap.ts,
                               reason=f"post-only would cross: ask {ask:.1f}c <= {P:.1f}c")
    else:
        bid = snap.bid(req.side)
        if bid is not None and bid >= P - 1e-9:
            return OrderUpdate(req.client_order_id, "REJECTED", snap.ts,
                               reason=f"post-only would cross: bid {bid:.1f}c >= {P:.1f}c")
    order.active = True
    order.remaining = req.count
    order.queue_ahead = displayed_ahead(order, snap, depth_max_age)
    order.last_volume = snap.quote.volume
    order.last_trade_ts = snap.ts
    return None


def _consume(order: RestingOrder, qty: float) -> int:
    """A trade of `qty` exactly at our price: queue first, then us."""
    if qty <= order.queue_ahead:
        order.queue_ahead -= qty
        return 0
    excess = qty - order.queue_ahead
    order.queue_ahead = 0.0
    return int(min(order.remaining, excess))


def advance(order: RestingOrder, snap: Snapshot | None, trades: list[Trade], info: MarketInfo,
            has_trade_feed: bool, volume_attribution: float) -> list[FillEvent]:
    """Apply new evidence; returns the fills (at price P, maker fee)."""
    req, P = order.req, order.price
    S = req.side
    buy = req.action == "buy"
    fills: list[FillEvent] = []

    def fill(n: int, ts: float, why: str) -> None:
        n = int(min(n, order.remaining))
        if n <= 0:
            return
        order.remaining -= n
        order.filled += n
        fills.append(FillEvent(req.client_order_id, ts, P, n,
                               fee_usd(n, P, info.fee_type, info.fee_multiplier, maker=True),
                               exchange_fill_id=f"maker-{req.client_order_id}-{order.filled}-{why}"))

    # 2. trade prints
    for t in sorted(trades, key=lambda x: x.ts):
        if order.remaining <= 0:
            break
        if t.ts < order.arrival_ts or (t.trade_id and t.trade_id in order.seen_trades):
            continue
        if t.trade_id:
            order.seen_trades.add(t.trade_id)
        sp = t.side_price(S)
        if buy and t.taker_side == other(S) and sp <= P + 1e-9:
            if sp < P - 1e-9:
                fill(order.remaining, t.ts, "through")
            else:
                fill(_consume(order, t.count), t.ts, "queue")
        elif (not buy) and t.taker_side == S and sp >= P - 1e-9:
            if sp > P + 1e-9:
                fill(order.remaining, t.ts, "through")
            else:
                fill(_consume(order, t.count), t.ts, "queue")
        order.last_trade_ts = max(order.last_trade_ts, t.ts)

    if snap is None or order.remaining <= 0:
        return fills

    # 3. touch-through on the snapshot
    if buy:
        ask = snap.ask(S)
        if ask is not None and ask <= P + 1e-9:
            fill(order.remaining, snap.ts, "touch")
    else:
        bid = snap.bid(S)
        if bid is not None and bid >= P - 1e-9:
            fill(order.remaining, snap.ts, "touch")

    # 4. volume fallback (no trade feed for this market)
    vol = snap.quote.volume
    if not has_trade_feed and order.remaining > 0 and order.last_volume is not None and vol > order.last_volume:
        dv = (vol - order.last_volume) * volume_attribution
        last = snap.quote.last
        if last is not None:
            lp = last if S == "yes" else 100.0 - last
            if buy and lp <= P + 1e-9:
                fill(order.remaining if lp < P - 1e-9 else _consume(order, dv), snap.ts, "vol")
            elif (not buy) and lp >= P - 1e-9:
                fill(order.remaining if lp > P + 1e-9 else _consume(order, dv), snap.ts, "vol")
    order.last_volume = vol
    return fills
