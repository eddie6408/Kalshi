"""Paper fill model (sections 25, 26).

An order is sent at decision time t0 and ARRIVES at t0 + latency. It is
resolved against the first snapshot observed at or after arrival - never
the snapshot the decision was made on - so price movement between signal
and execution is paid for, and nothing after that snapshot is used.

Orders are marketable limit IOC (exactly what LIVE sends):
  * walk the ladder a taker would trade against, best price first;
  * only levels at or inside the limit;
  * assume we get `depth_participation` of the displayed size (others are
    competing for the same liquidity);
  * with probability `touch_miss_probability` someone beats us and the
    order misses entirely;
  * whatever is not filled is cancelled (IOC). Outcomes: FILLED, PARTIAL,
    NO_FILL (a missed trade) or REJECTED.
Each fill is charged the Kalshi taker fee for its level.

The random draw is seeded from the client order id, so replays are
deterministic.
"""
from __future__ import annotations

import random
import zlib

from lvt.fees import fee_usd
from lvt.models import FillEvent, MarketInfo, OrderRequest, OrderUpdate, Snapshot


def order_rng(client_order_id: str) -> random.Random:
    return random.Random(zlib.crc32(client_order_id.encode()))


def latency_s(client_order_id: str, base_ms: float, jitter_ms: float) -> float:
    r = order_rng(client_order_id + ":lat")
    return max(0.0, (base_ms + r.uniform(-jitter_ms, jitter_ms))) / 1000.0


def resolve(req: OrderRequest, snap: Snapshot, info: MarketInfo, now: float, cfg,
            depth_max_age: float) -> OrderUpdate:
    """cfg is the [execution] section."""
    coid = req.client_order_id
    if snap.quote.status and snap.quote.status != "active":
        return OrderUpdate(coid, "REJECTED", now, reason=f"market status {snap.quote.status}")
    if not (1 <= req.limit_price <= 99) or req.count < 1:
        return OrderUpdate(coid, "REJECTED", now, reason=f"invalid order {req.count}@{req.limit_price}")
    if now - snap.ts > cfg.reject_if_data_older_than_s:
        return OrderUpdate(coid, "REJECTED", now, reason=f"no fresh data at arrival ({now - snap.ts:.1f}s)")

    buy = req.action == "buy"
    levels = snap.ask_levels(req.side, depth_max_age) if buy else snap.bid_levels(req.side, depth_max_age)
    usable = [(p, s) for p, s in levels if (p <= req.limit_price + 1e-9 if buy else p >= req.limit_price - 1e-9)]
    if not usable:
        best = levels[0][0] if levels else None
        why = ("no liquidity on that side" if best is None else
               f"price moved: best {'ask' if buy else 'bid'} {best:.1f}c vs limit {req.limit_price:.1f}c")
        return OrderUpdate(coid, "NO_FILL", now, reason=why)

    rng = order_rng(coid)
    if rng.random() < cfg.touch_miss_probability:
        return OrderUpdate(coid, "NO_FILL", now, reason="beaten to the touch (simulated queue loss)")

    remaining = req.count
    fills: list[FillEvent] = []
    for i, (price, size) in enumerate(usable):
        avail = int(size * cfg.depth_participation)
        take = min(avail, remaining)
        if take <= 0:
            continue
        fills.append(FillEvent(coid, now, price, take,
                               fee_usd(take, price, info.fee_type, info.fee_multiplier),
                               exchange_fill_id=f"paper-{coid}-{i}"))
        remaining -= take
        if remaining == 0:
            break
    if not fills:
        return OrderUpdate(coid, "NO_FILL", now, reason="displayed size too small after participation haircut")
    status = "FILLED" if remaining == 0 else "PARTIAL"
    return OrderUpdate(coid, status, now, fills=fills,
                       reason="" if remaining == 0 else f"IOC remainder {remaining} cancelled")
