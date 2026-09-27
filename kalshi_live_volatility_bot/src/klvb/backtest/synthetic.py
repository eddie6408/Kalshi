"""SYNTHETIC market generator - for tests and plumbing demos ONLY.

Synthetic paths exercise the code (dips, collapses, momentum, chop, spread
blowouts); they say NOTHING about real profitability. Anything produced from
them is stored with source='synthetic' and must never be reported as evidence.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from ..models import BookLevel, MarketMeta, MarketSnapshot, OrderBook


@dataclass
class Segment:
    seconds: float
    drift_per_min: float    # cents / minute
    noise: float = 0.3      # cents per step std


PATTERNS: dict[str, list[Segment]] = {
    # 50 -> ~40 dip, stabilizes, recovers
    "dip_recover": [Segment(240, 0.0, 0.2), Segment(120, -5.0, 0.3), Segment(60, 0.0, 0.2), Segment(240, 2.5, 0.3),
                    Segment(300, 0.0, 0.3)],
    # relentless collapse: must NOT be bought
    "collapse": [Segment(240, 0.0, 0.2), Segment(420, -4.0, 0.3), Segment(300, 0.0, 0.3)],
    # steady momentum up
    "momentum_up": [Segment(300, 0.0, 0.2), Segment(300, 3.0, 0.2), Segment(120, 0.5, 0.2), Segment(300, -1.0, 0.3)],
    # spike up then roll over (REVERSAL on NO)
    "spike_fade": [Segment(240, 0.0, 0.2), Segment(120, 5.0, 0.3), Segment(60, 0.0, 0.2), Segment(240, -2.5, 0.3),
                   Segment(300, 0.0, 0.3)],
    # sustained decline (DOWNTREND on NO)
    "momentum_down": [Segment(300, 0.0, 0.2), Segment(300, -3.0, 0.2), Segment(120, -0.5, 0.2), Segment(300, 1.0, 0.3)],
    "chop": [Segment(900, 0.0, 0.8)],
}


def generate(ticker: str, pattern: str, start_ts: float, start_price: float = 50.0, step_seconds: float = 2.0,
             seed: int = 0, spread: float = 1.0, depth: float = 400.0, volume_per_step: float = 30.0,
             event_ticker: str = "SYNTH-EVENT", sport: str = "BASKETBALL", close_in_hours: float = 6.0
             ) -> tuple[MarketMeta, list[MarketSnapshot]]:
    rng = random.Random(seed)
    meta = MarketMeta(ticker=ticker, event_ticker=event_ticker, series_ticker="SYNTH", title=f"SYNTHETIC {pattern}",
                      category="Sports", sport=sport, status="active", close_ts=start_ts + close_in_hours * 3600,
                      fee_type="quadratic", fee_multiplier=1.0)
    t, p, vol = start_ts, start_price, 0.0
    snaps: list[MarketSnapshot] = []
    for seg in PATTERNS[pattern]:
        steps = int(seg.seconds / step_seconds)
        for _ in range(steps):
            p += seg.drift_per_min * step_seconds / 60.0 + rng.gauss(0, seg.noise)
            p = min(95.0, max(5.0, p))
            mid = round(p)
            bid = float(mid - int(spread // 2)) if spread > 1 else float(mid)
            ask = bid + spread
            active = 1.5 if abs(seg.drift_per_min) > 1 else 1.0
            vol += volume_per_step * active * (0.5 + rng.random())
            book = OrderBook(ts=t,
                             yes_bids=[BookLevel(bid - i, depth * (1 + i)) for i in range(5)],
                             no_bids=[BookLevel(100 - ask - i, depth * (1 + i)) for i in range(5)])
            snaps.append(MarketSnapshot(ticker=ticker, ts=t, yes_bid=bid, yes_ask=ask, last_price=float(mid),
                                        volume=round(vol), volume_24h=50_000, open_interest=20_000,
                                        yes_bid_size=depth, yes_ask_size=depth, status="active",
                                        source="synthetic", book=book))
            t += step_seconds
    return meta, snaps
