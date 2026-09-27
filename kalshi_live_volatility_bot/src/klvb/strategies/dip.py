"""Buy-the-dip (DIP, direction UP) and its mirror REVERSAL (direction DOWN).

Pattern (in side space):  LOCAL HIGH -> MEANINGFUL DECLINE -> DECLINE SLOWS ->
STABILIZATION (no new low for N seconds) -> REVERSAL CONFIRMATION -> BUY.

REVERSAL runs the same logic on the NO price: a YES spike (= NO dip) that stalls
and starts rolling over is bought via NO, i.e. "selling into an increase" using
Kalshi's real downside mechanism.

Distinguishing a DIP from a CONTINUED COLLAPSE:
  * short-window velocity must not be falling faster than max_collapse_velocity
  * no new low within stabilization_seconds
  * price must have rebounded >= min_rebound_cents off the trough
  * short-window velocity must have turned non-negative
A setup that fails these gates is logged as NO_TRADE ("falling knife").
"""
from __future__ import annotations

from ..models import Direction, Signal
from .base import Strategy, StrategyContext


class DipStrategy(Strategy):
    name = "DIP"

    def __init__(self, params, direction: Direction = Direction.UP):
        super().__init__(params, direction)
        if direction is Direction.DOWN:
            self.name = "REVERSAL"

    def evaluate(self, ctx: StrategyContext) -> Signal | None:
        p = self.p
        pts = ctx.points(p["lookback_seconds"], self.side)
        if len(pts) < ctx.vol_cfg.min_samples:
            return None
        # local high, then the trough after it
        hi_idx = max(range(len(pts)), key=lambda i: (pts[i][1], i))
        high_ts, high = pts[hi_idx]
        after = pts[hi_idx:]
        if len(after) < 3:
            return None
        low = min(x[1] for x in after)
        last_low_ts = max(t for t, x in after if x <= low + 1e-9)
        drop = high - low
        if drop < p["min_drop_cents"] or (high > 0 and drop / high < p["min_drop_pct"]):
            return None

        f = ctx.features
        cur = pts[-1][1]
        ask = f.side_ask(self.side)
        bid = f.side_bid(self.side)
        if ask is None or bid is None:
            return None
        rebound = cur - low
        vel = ctx.velocity_short(self.side)
        since_low = ctx.now - last_low_ts
        # velocity of the decline leg (high -> low) to show that the decline slowed
        leg = [x for x in after if x[0] <= last_low_ts]
        leg_minutes = max((last_low_ts - high_ts) / 60.0, 1e-6)
        decline_velocity = -drop / leg_minutes

        details = self.common_details(ctx) | {
            "recent_high": round(high, 2), "recent_low": round(low, 2), "decline_cents": round(drop, 2),
            "decline_pct": round(100 * drop / high, 2) if high else 0.0, "rebound_cents": round(rebound, 2),
            "seconds_since_low": round(since_low, 1), "decline_velocity": round(decline_velocity, 2),
            "leg_points": len(leg),
        }
        target = min(high, low + p["target_retrace_fraction"] * drop)
        stop = low - p["stop_buffer_cents"]
        gross = target - ask
        costs = self.costs(ctx, ask, target)
        net = gross - costs

        reject: list[str] = []
        if vel < p["max_collapse_velocity"]:
            reject.append(f"falling knife: still dropping {vel:+.2f}c/min")
        if since_low < p["stabilization_seconds"]:
            reject.append(f"no stabilization: new low {since_low:.0f}s ago (< {p['stabilization_seconds']}s)")
        if rebound < p["min_rebound_cents"]:
            reject.append(f"no reversal confirmation: rebound {rebound:.1f}c < {p['min_rebound_cents']}c")
        if vel < 0:
            reject.append(f"short-window velocity still negative ({vel:+.2f}c/min)")
        if rebound > p["max_rebound_fraction"] * drop:
            reject.append(f"late: already recovered {100 * rebound / drop:.0f}% of the drop (don't chase)")
        if net < ctx.costs_cfg.min_net_edge_cents:
            reject.append(f"insufficient edge: expected net {net:.1f}c < {ctx.costs_cfg.min_net_edge_cents}c "
                          f"(gross {gross:.1f}c, costs {costs:.1f}c)")
        if ask <= stop:
            reject.append("ask already below stop")
        if ctx.sports is not None and ctx.sports.status.lower() in ("final", "closed", "complete", "ended"):
            reject.append("event is final")

        comps = self.base_components(ctx, net, costs) | {
            "magnitude": drop / (2.0 * p["min_drop_cents"]),
            "velocity": max(0.0, vel) / 3.0,
            "confirmation": min(1.0, rebound / (2.0 * p["min_rebound_cents"]))
                            * min(1.0, since_low / (2.0 * p["stabilization_seconds"])),
        }
        reasons = [
            f"{'NO' if self.side == 'no' else 'YES'} price fell {drop:.1f}c ({details['decline_pct']:.1f}%) "
            f"from local high {high:.1f}c to {low:.1f}c",
            f"no new low for {since_low:.0f}s (decline slowed from {decline_velocity:.1f}c/min to {vel:+.2f}c/min)",
            f"rebounded {rebound:.1f}c off the low",
            f"target {target:.1f}c (retrace {p['target_retrace_fraction']:.0%}), stop {stop:.1f}c",
            f"expected net {net:.1f}c after {costs:.1f}c fees+slippage",
        ]
        return self.make_signal(ctx, "BUY", ask, target, stop, gross, costs, reasons, details, comps, reject)
