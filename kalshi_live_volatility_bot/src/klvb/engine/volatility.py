"""Volatility engine: rolling, look-ahead-free market metrics.

Each market keeps a time-ordered buffer of observations. Every metric is computed
from observations with ts <= the evaluation time, so live trading and replay see
exactly the same information.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, asdict, field
from typing import Any

from ..models import MarketSnapshot, OrderBook, TradePrint

BUFFER_SECONDS = 1500


@dataclass
class Obs:
    ts: float
    bid: float | None
    ask: float | None
    mid: float
    last: float | None
    volume: float | None
    bid_size: float | None
    ask_size: float | None


@dataclass
class Features:
    ticker: str
    ts: float
    samples: int
    price: float | None = None
    bid: float | None = None
    ask: float | None = None
    spread: float | None = None
    spread_pct: float | None = None
    last: float | None = None
    recent_high: float | None = None
    recent_low: float | None = None
    change_medium: float = 0.0
    change_pct_medium: float = 0.0
    range_long: float = 0.0
    realized_vol: float = 0.0          # cents / sqrt(min), medium window
    realized_vol_long: float = 0.0
    velocity_short: float = 0.0        # cents / min
    velocity_medium: float = 0.0
    acceleration: float = 0.0          # change in short velocity, cents / min per short window
    volume_window: float = 0.0         # contracts traded in the medium window
    volume_rate: float = 0.0           # contracts / min, short window
    volume_rate_baseline: float = 0.0  # contracts / min, long window
    volume_accel: float = 0.0          # short rate / baseline rate
    volume_24h: float | None = None
    open_interest: float | None = None
    bid_depth: float | None = None     # YES bid size within depth window
    ask_depth: float | None = None     # YES ask size within depth window
    imbalance: float | None = None     # (bid - ask) / (bid + ask) depth, +ve = buying pressure
    update_freq: float = 0.0           # mid changes per minute (medium window)
    time_since_move: float | None = None
    data_age: float | None = None
    book_age: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items()}

    def side_bid(self, side: str) -> float | None:
        if side == "yes":
            return self.bid
        return None if self.ask is None else 100 - self.ask

    def side_ask(self, side: str) -> float | None:
        if side == "yes":
            return self.ask
        return None if self.bid is None else 100 - self.bid

    def side_ask_depth(self, side: str) -> float | None:
        return self.ask_depth if side == "yes" else self.bid_depth

    def side_bid_depth(self, side: str) -> float | None:
        return self.bid_depth if side == "yes" else self.ask_depth


def slope_per_min(points: list[tuple[float, float]]) -> float:
    """OLS slope of price vs time in cents per minute."""
    n = len(points)
    if n < 2:
        return 0.0
    t0 = points[0][0]
    xs = [(p[0] - t0) / 60.0 for p in points]
    ys = [p[1] for p in points]
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 1e-12:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx


def realized_vol(points: list[tuple[float, float]]) -> float:
    """sqrt(sum of squared price changes / elapsed minutes): cents per sqrt(minute)."""
    if len(points) < 2:
        return 0.0
    elapsed = (points[-1][0] - points[0][0]) / 60.0
    if elapsed <= 0:
        return 0.0
    ss = sum((b[1] - a[1]) ** 2 for a, b in zip(points, points[1:]))
    return math.sqrt(ss / elapsed)


class MarketSeries:
    def __init__(self, ticker: str):
        self.ticker = ticker
        self.obs: deque[Obs] = deque()
        self.trades: deque[TradePrint] = deque()
        self.book: OrderBook | None = None
        self.last_snapshot: MarketSnapshot | None = None
        self.last_move_ts: float | None = None

    def add_snapshot(self, s: MarketSnapshot) -> bool:
        """Returns False if the snapshot is out of order or unusable."""
        mid = s.mid
        if mid is None:
            self.last_snapshot = s
            return False
        if self.obs and s.ts < self.obs[-1].ts:
            return False
        if self.obs and abs(mid - self.obs[-1].mid) >= 1e-9:
            self.last_move_ts = s.ts
        elif not self.obs:
            self.last_move_ts = s.ts
        self.obs.append(Obs(s.ts, s.yes_bid, s.yes_ask, mid, s.last_price, s.volume, s.yes_bid_size, s.yes_ask_size))
        if s.book is not None:
            self.book = s.book
        self.last_snapshot = s
        cutoff = s.ts - BUFFER_SECONDS
        while self.obs and self.obs[0].ts < cutoff:
            self.obs.popleft()
        return True

    def add_book(self, book: OrderBook) -> None:
        if self.book is None or book.ts >= self.book.ts:
            self.book = book

    def add_trades(self, trades: list[TradePrint]) -> None:
        seen = {t.trade_id for t in self.trades if t.trade_id}
        for t in trades:
            if t.trade_id and t.trade_id in seen:
                continue
            self.trades.append(t)
        if self.obs:
            cutoff = self.obs[-1].ts - BUFFER_SECONDS
            while self.trades and self.trades[0].ts < cutoff:
                self.trades.popleft()

    # ---- side-space windows used by strategies ---------------------------
    def window(self, seconds: float, at: float, side: str = "yes") -> list[tuple[float, float]]:
        lo = at - seconds
        pts = [(o.ts, o.mid) for o in self.obs if lo <= o.ts <= at]
        if side == "no":
            pts = [(t, 100 - p) for t, p in pts]
        return pts

    def volume_between(self, t0: float, t1: float) -> float:
        """Contracts traded in (t0, t1]; uses cumulative volume, trade prints as fallback."""
        inside = [o for o in self.obs if t0 <= o.ts <= t1 and o.volume is not None]
        if len(inside) >= 2:
            return max(0.0, inside[-1].volume - inside[0].volume)
        return sum(t.count for t in self.trades if t0 < t.ts <= t1)

    def trades_between(self, t0: float, t1: float) -> list[TradePrint]:
        return [t for t in self.trades if t0 < t.ts <= t1]


class VolatilityEngine:
    def __init__(self, vol_cfg, depth_window_cents: float = 3.0):
        self.cfg = vol_cfg
        self.depth_window = depth_window_cents
        self.series: dict[str, MarketSeries] = {}

    def get(self, ticker: str) -> MarketSeries:
        s = self.series.get(ticker)
        if s is None:
            s = self.series[ticker] = MarketSeries(ticker)
        return s

    def on_snapshot(self, snap: MarketSnapshot) -> bool:
        return self.get(snap.ticker).add_snapshot(snap)

    def on_book(self, ticker: str, book: OrderBook) -> None:
        self.get(ticker).add_book(book)

    def on_trades(self, ticker: str, trades: list[TradePrint]) -> None:
        self.get(ticker).add_trades(trades)

    def drop(self, ticker: str) -> None:
        self.series.pop(ticker, None)

    def features(self, ticker: str, at: float) -> Features | None:
        s = self.series.get(ticker)
        if s is None or not s.obs:
            return None
        c = self.cfg
        obs = [o for o in s.obs if o.ts <= at]
        if not obs:
            return None
        cur = obs[-1]
        f = Features(ticker=ticker, ts=at, samples=len(obs))
        f.price, f.bid, f.ask, f.last = cur.mid, cur.bid, cur.ask, cur.last
        if cur.bid is not None and cur.ask is not None:
            f.spread = cur.ask - cur.bid
            f.spread_pct = f.spread / cur.mid if cur.mid else None
        f.data_age = at - cur.ts
        snap = s.last_snapshot
        if snap is not None and snap.ts <= at:
            f.volume_24h, f.open_interest = snap.volume_24h, snap.open_interest

        long_pts = s.window(c.long_window_seconds, at)
        med_pts = s.window(c.medium_window_seconds, at)
        short_pts = s.window(c.short_window_seconds, at)
        prev_short = s.window(c.short_window_seconds, at - c.short_window_seconds)

        if long_pts:
            prices = [p for _, p in long_pts]
            f.recent_high, f.recent_low = max(prices), min(prices)
            f.range_long = f.recent_high - f.recent_low
        if len(med_pts) >= 2:
            f.change_medium = med_pts[-1][1] - med_pts[0][1]
            f.change_pct_medium = f.change_medium / med_pts[0][1] if med_pts[0][1] else 0.0
            changes = sum(1 for a, b in zip(med_pts, med_pts[1:]) if abs(b[1] - a[1]) > 1e-9)
            span = max((med_pts[-1][0] - med_pts[0][0]) / 60.0, 1e-9)
            f.update_freq = changes / span
        f.realized_vol = realized_vol(med_pts)
        f.realized_vol_long = realized_vol(long_pts)
        f.velocity_short = slope_per_min(short_pts)
        f.velocity_medium = slope_per_min(med_pts)
        f.acceleration = f.velocity_short - slope_per_min(prev_short) if len(prev_short) >= 2 else 0.0

        f.volume_window = s.volume_between(at - c.medium_window_seconds, at)
        short_vol = s.volume_between(at - c.short_window_seconds, at)
        long_vol = s.volume_between(at - c.long_window_seconds, at)
        long_span_min = min(c.long_window_seconds, max(at - obs[0].ts, 1.0)) / 60.0
        f.volume_rate = short_vol / (c.short_window_seconds / 60.0)
        f.volume_rate_baseline = long_vol / long_span_min if long_span_min > 0 else 0.0
        f.volume_accel = (f.volume_rate / f.volume_rate_baseline) if f.volume_rate_baseline > 0 else 0.0

        book = s.book if (s.book is not None and s.book.ts <= at) else None
        if book is not None:
            f.book_age = at - book.ts
            yb = book.best_yes_bid()
            ya = book.best_yes_ask()
            f.bid_depth = book.depth_within(book.yes_bids, yb.price, self.depth_window) if yb else 0.0
            f.ask_depth = book.depth_within(book.asks_for("yes"), ya.price, self.depth_window) if ya else 0.0
        else:
            f.bid_depth, f.ask_depth = cur.bid_size, cur.ask_size
        if f.bid_depth is not None and f.ask_depth is not None and (f.bid_depth + f.ask_depth) > 0:
            f.imbalance = (f.bid_depth - f.ask_depth) / (f.bid_depth + f.ask_depth)
        move_ts = obs[0].ts
        for a, b in zip(reversed(obs[:-1]), reversed(obs)):
            if abs(b.mid - a.mid) >= c.meaningful_move_cents - 1e-9:
                move_ts = b.ts
                break
        f.time_since_move = at - move_ts
        return f
