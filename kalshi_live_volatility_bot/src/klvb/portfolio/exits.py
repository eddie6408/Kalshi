"""Dynamic exit engine. Re-evaluated for every open position on every loop.

Priority (first match wins):
  EMERGENCY_EXIT          kill switch with flatten, or daily-loss flatten policy
  SETTLEMENT_EXIT         close/expected expiry is near - never hold into settlement
  MAX_LOSS_EXIT           exit price (bid) at or below the stop
  LIQUIDITY_EXIT          spread blew out / bid depth vanished
  FIXED_PROFIT_TARGET     exit price reached the target
  TRAILING_PROFIT_TARGET  after arming, price gave back trail distance from its peak
  MOMENTUM_EXIT           momentum trades: short-term velocity turned against us
  REVERSAL_EXIT           dip/reversal trades: rebound failed, price rolling over below entry
  TIME_EXIT               maximum holding period elapsed
All prices are in side space (the price of the contract we hold).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..engine.volatility import Features
from ..models import MarketMeta, Position


@dataclass
class ExitDecision:
    reason: str
    limit: float
    detail: str


def evaluate_exit(pos: Position, f: Features | None, meta: MarketMeta | None, now: float, exits_cfg, risk_cfg,
                  flatten: str | None = None, tick: float = 1.0) -> ExitDecision | None:
    if pos.qty <= 0 or f is None:
        return None
    bid = f.side_bid(pos.side)
    if bid is None:
        return None
    slip = exits_cfg.exit_slippage_ticks * tick
    lim = max(1.0, bid - slip)
    sign = 1.0 if pos.side == "yes" else -1.0
    vel = sign * f.velocity_short
    vel_med = sign * f.velocity_medium
    gain = bid - pos.avg_entry
    # thesis-invalidation exits need BOTH the short and the medium trend against us plus a real adverse
    # move, so ordinary tick noise does not shake the position out
    adverse = gain <= -exits_cfg.thesis_fail_cents
    trend_against = vel <= exits_cfg.momentum_exit_velocity and vel_med < 0

    if flatten:
        return ExitDecision("EMERGENCY_EXIT", lim, flatten)
    end = meta.effective_end_ts() if meta else None
    if end is not None and end - now <= exits_cfg.settlement_exit_seconds:
        return ExitDecision("SETTLEMENT_EXIT", lim, f"{end - now:.0f}s to close/expiry")
    if bid <= pos.stop:
        return ExitDecision("MAX_LOSS_EXIT", lim, f"bid {bid:.1f}c <= stop {pos.stop:.1f}c")
    depth = f.side_bid_depth(pos.side)
    if (f.spread is not None and f.spread > exits_cfg.max_exit_spread_cents) or \
            (depth is not None and depth < exits_cfg.min_exit_depth_contracts):
        return ExitDecision("LIQUIDITY_EXIT", lim, f"spread {f.spread}c, bid depth {depth}")
    if bid >= pos.target:
        # at the target, a 1-tick allowance is enough; no need to give away more
        return ExitDecision("FIXED_PROFIT_TARGET", max(1.0, bid - tick), f"bid {bid:.1f}c >= target {pos.target:.1f}c")
    trail = pos.entry_features.get("signal_details", {}).get("trail_cents", exits_cfg.trailing_distance_cents)
    if pos.peak_mark is not None and pos.peak_mark - pos.avg_entry >= exits_cfg.trailing_activation_cents \
            and bid <= pos.peak_mark - trail:
        return ExitDecision("TRAILING_PROFIT_TARGET", lim,
                            f"bid {bid:.1f}c gave back {pos.peak_mark - bid:.1f}c from peak {pos.peak_mark:.1f}c")
    if pos.strategy in ("MOMENTUM", "DOWNTREND") and trend_against and (adverse or gain <= 0):
        return ExitDecision("MOMENTUM_EXIT", lim, f"momentum ended: velocity {vel:+.2f}/{vel_med:+.2f}c/min "
                                                  f"(short/medium), {gain:+.1f}c vs entry")
    if pos.strategy in ("DIP", "REVERSAL") and trend_against and adverse:
        return ExitDecision("REVERSAL_EXIT", lim, f"rebound failed: velocity {vel:+.2f}/{vel_med:+.2f}c/min, "
                                                  f"{gain:+.1f}c vs entry")
    max_hold = min(pos.max_hold_seconds, risk_cfg.max_holding_seconds)
    if now - pos.opened_ts >= max_hold:
        return ExitDecision("TIME_EXIT", lim, f"held {now - pos.opened_ts:.0f}s >= {max_hold:.0f}s")
    return None
