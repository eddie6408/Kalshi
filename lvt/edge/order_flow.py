"""Order flow and order-book confirmation (edge-engine phase 6, sections 13-15).

Order flow comes from public trade prints (taker_side = the side the taker
bought), in YES terms: a taker buying YES is +volume, a taker buying NO
(= selling YES) is -volume. Everything is reported side-relative so a NO
position reads it the same way a YES position does.

Book state comes from the depth ladders (YES bids and NO bids) or, when no
fresh depth exists, the top-of-book sizes.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from lvt.models import Snapshot


@dataclass
class Flow:
    buy_volume: float = 0.0          # taker buying the side
    sell_volume: float = 0.0         # taker selling the side (buying the other)
    buy_trades: int = 0
    sell_trades: int = 0
    avg_buy_size: float = 0.0
    avg_sell_size: float = 0.0
    large_trades: int = 0
    signed: float = 0.0              # buy - sell
    signed_short: float = 0.0        # very short window
    signed_prev_short: float = 0.0   # the window before it
    acceleration: float = 0.0        # signed_short - signed_prev_short
    shock: bool = False              # short burst far above the window's typical pace
    sustained: bool = False          # same direction on short and long windows

    def describe(self) -> str:
        if self.buy_trades + self.sell_trades == 0:
            return "no trades"
        d = "buying" if self.signed > 0 else "selling" if self.signed < 0 else "balanced"
        tag = " (temporary shock)" if self.shock else " (sustained)" if self.sustained else ""
        acc = "accelerating" if self.acceleration * self.signed > 0 else "weakening"
        return f"net {d} {abs(self.signed):.0f} contracts{tag}, {acc}"


class FlowTracker:
    def __init__(self, short_s: float = 10.0, long_s: float = 120.0, large: float = 500.0, shock_mult: float = 3.0):
        self.short_s, self.long_s = short_s, long_s
        self.large = large
        self.shock_mult = shock_mult
        self.trades: dict[str, deque] = {}          # ticker -> (ts, signed_yes, size)
        self.book_prev: dict[str, tuple[float, float, float]] = {}   # ticker -> (ts, bid_depth, ask_depth)

    def add(self, trades) -> None:
        for t in trades:
            dq = self.trades.setdefault(t.ticker, deque(maxlen=5000))
            sgn = 1.0 if t.taker_side == "yes" else -1.0 if t.taker_side == "no" else 0.0
            dq.append((t.ts, sgn * t.count, t.count))

    def flow(self, ticker: str, side: str, now: float) -> Flow:
        f = Flow()
        dq = self.trades.get(ticker)
        if not dq:
            return f
        k = 1.0 if side == "yes" else -1.0
        cutoff = now - self.long_s
        while dq and dq[0][0] < now - 3 * self.long_s:
            dq.popleft()
        for ts, sv, size in dq:
            if ts < cutoff or ts > now:
                continue
            s = sv * k
            if s > 0:
                f.buy_volume += size; f.buy_trades += 1
            elif s < 0:
                f.sell_volume += size; f.sell_trades += 1
            if size >= self.large:
                f.large_trades += 1
            if ts >= now - self.short_s:
                f.signed_short += s
            elif ts >= now - 2 * self.short_s:
                f.signed_prev_short += s
        f.signed = f.buy_volume - f.sell_volume
        f.avg_buy_size = f.buy_volume / f.buy_trades if f.buy_trades else 0.0
        f.avg_sell_size = f.sell_volume / f.sell_trades if f.sell_trades else 0.0
        f.acceleration = f.signed_short - f.signed_prev_short
        per_window = (f.buy_volume + f.sell_volume) * self.short_s / self.long_s
        f.shock = per_window > 0 and abs(f.signed_short) >= self.shock_mult * per_window
        f.sustained = f.signed_short * f.signed > 0 and abs(f.signed) >= 0.5 * (f.buy_volume + f.sell_volume)
        return f

    def book(self, snap: Snapshot, side: str, depth_max_age: float) -> dict:
        """Side-relative book: bids = contracts wanting to buy `side`."""
        bids = sum(s for _, s in snap.bid_levels(side, depth_max_age))
        asks = sum(s for _, s in snap.ask_levels(side, depth_max_age))
        tot = bids + asks
        key = f"{snap.ticker}:{side}"
        prev = self.book_prev.get(key)
        self.book_prev[key] = (snap.ts, bids, asks)
        d_bid = (bids - prev[1]) if prev else 0.0
        d_ask = (asks - prev[2]) if prev else 0.0
        return {"bid_depth": round(bids, 1), "ask_depth": round(asks, 1),
                "ratio": round(bids / asks, 3) if asks > 0 else None,
                "imbalance": round((bids - asks) / tot, 3) if tot > 0 else 0.0,
                "bid_change": round(d_bid, 1), "ask_change": round(d_ask, 1),
                "bid_replenishing": d_bid > 0 and bids > 0,
                "ask_replenishing": d_ask > 0 and asks > 0}
