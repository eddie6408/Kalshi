"""Dynamic exit engine (sections 22, 23). Re-evaluated on every snapshot for
every open position; nothing waits for settlement by default."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from lvt.data.provider import EventContext
from lvt.engine.positions import Position
from lvt.fees import fee_usd
from lvt.engine.volatility import Metrics, side_metrics
from lvt.models import MarketInfo, Snapshot

EXIT_REASONS = ("EMERGENCY_EXIT", "SETTLEMENT_EXIT", "MAX_LOSS_EXIT", "FIXED_PROFIT_TARGET",
                "TRAILING_PROFIT_TARGET", "REVERSAL_EXIT", "MOMENTUM_EXIT", "LIQUIDITY_EXIT",
                "TIME_EXIT")
URGENT = ("EMERGENCY_EXIT", "SETTLEMENT_EXIT", "MAX_LOSS_EXIT", "LIQUIDITY_EXIT")


def breakeven_bid(p: Position, info: MarketInfo) -> float:
    """Bid at which selling everything nets zero after both legs' fees."""
    if p.entry_qty <= 0:
        return p.avg_entry
    entry_fee_c = p.entry_fees_usd * 100.0 / p.entry_qty
    exit_fee_c = fee_usd(max(p.qty, 1), p.avg_entry, info.fee_type, info.fee_multiplier) * 100.0 / max(p.qty, 1)
    return p.avg_entry + entry_fee_c + exit_fee_c


@dataclass
class ExitDecision:
    reason: str
    detail: str
    bid: float
    limit: float


def evaluate_exit(p: Position, snap: Snapshot, m: Optional[Metrics], info: MarketInfo,
                  now: float, cfg, ctx: Optional[EventContext] = None,
                  emergency: str = "") -> tuple[Optional[ExitDecision], str]:
    """Returns (decision or None, hold explanation)."""
    bid = snap.bid(p.side)
    if bid is None:
        return None, "HOLD: no bid on our side - cannot exit into an empty book"

    # Trailing stop (peak is maintained by PositionBook.mark). It activates
    # only once the peak is trail_activation_cents above the NET breakeven
    # (entry + entry fee + exit fee per contract), and it never trails below
    # breakeven - otherwise a "profit lock" can lock in a loss after fees.
    be = breakeven_bid(p, info)
    if p.peak_bid - be >= cfg.trail_activation_cents:
        p.trail_stop = max(p.trail_stop, p.peak_bid - cfg.trail_distance_cents, be)

    sm = side_metrics(m, p.side) if m is not None else None
    held = now - p.opened_ts
    spread = snap.spread()
    reason = detail = ""

    if emergency:
        reason, detail = "EMERGENCY_EXIT", emergency
    elif snap.quote.status and snap.quote.status != "active":
        reason, detail = "SETTLEMENT_EXIT", f"market status {snap.quote.status}"
    elif info.close_ts and info.close_ts - now <= cfg.settlement_buffer_s:
        reason, detail = "SETTLEMENT_EXIT", f"market closes in {info.close_ts - now:.0f}s"
    elif bid >= cfg.settlement_price_cents:
        reason, detail = "SETTLEMENT_EXIT", f"bid {bid:.1f}c is effectively settled - take it"
    elif ctx is not None and ctx.status == "final":
        reason, detail = "SETTLEMENT_EXIT", f"event finished ({ctx.provider}: {ctx.score})"
    elif p.stop and bid <= p.stop:
        reason, detail = "MAX_LOSS_EXIT", f"bid {bid:.1f}c <= stop {p.stop:.1f}c (thesis invalid)"
    elif p.target and bid >= p.target:
        reason, detail = "FIXED_PROFIT_TARGET", f"bid {bid:.1f}c >= target {p.target:.1f}c"
    elif p.trail_stop and bid <= p.trail_stop:
        reason, detail = "TRAILING_PROFIT_TARGET", (f"bid {bid:.1f}c fell to trail {p.trail_stop:.1f}c "
                                                    f"(peak {p.peak_bid:.1f}c)")
    elif sm is not None and sm["velocity_short"] <= -cfg.reversal_exit_velocity and sm["velocity_medium"] < 0:
        reason, detail = "REVERSAL_EXIT", (f"price reversing against us: short {sm['velocity_short']:+.2f}, "
                                           f"medium {sm['velocity_medium']:+.2f}c/min")
    elif (sm is not None and p.strategy in ("MOMENTUM", "DOWNTREND")
          and sm["velocity_short"] <= -cfg.momentum_exit_velocity):
        reason, detail = "MOMENTUM_EXIT", f"momentum gone: short slope {sm['velocity_short']:+.2f}c/min"
    elif cfg.liquidity_exit_enabled and (spread is None or spread >= cfg.liquidity_exit_spread_cents):
        reason, detail = "LIQUIDITY_EXIT", f"spread {spread}c >= {cfg.liquidity_exit_spread_cents}c"
    elif held >= cfg.max_hold_s:
        reason, detail = "TIME_EXIT", f"held {held:.0f}s >= {cfg.max_hold_s}s"

    if not reason:
        return None, (f"HOLD: bid {bid:.1f}c entry {p.avg_entry:.1f}c target {p.target:.1f}c "
                      f"stop {p.stop:.1f}c trail {p.trail_stop:.1f}c held {held:.0f}s")
    slip = cfg.emergency_slippage_cents if reason in URGENT else cfg.exit_slippage_cents
    limit = max(1.0, round(bid - slip, 2))
    return ExitDecision(reason, detail, bid, limit), ""
