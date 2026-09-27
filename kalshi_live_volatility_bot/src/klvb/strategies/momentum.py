"""Buy-the-increase (MOMENTUM, direction UP) and its mirror DOWNTREND (direction DOWN).

Pattern (side space): PRICE INCREASE -> PERSISTENCE -> VELOCITY -> ACCELERATION ->
VOLUME CONFIRMATION -> ENTRY. One tick is never enough: the move must persist
across `confirm_samples` observations with a majority of changes in its direction.

Anti-chase: if the move is already larger than max_extension_cents, or there is
not enough headroom to 99c for the target, it is NO_TRADE.

DOWNTREND runs the same logic on the NO price: YES falling with persistence is
traded by buying NO ("selling into a decline").
"""
from __future__ import annotations

from ..models import Direction, Signal
from .base import Strategy, StrategyContext


class MomentumStrategy(Strategy):
    name = "MOMENTUM"

    def __init__(self, params, direction: Direction = Direction.UP):
        super().__init__(params, direction)
        if direction is Direction.DOWN:
            self.name = "DOWNTREND"

    def evaluate(self, ctx: StrategyContext) -> Signal | None:
        p = self.p
        pts = ctx.points(p["lookback_seconds"], self.side)
        if len(pts) < max(ctx.vol_cfg.min_samples, p["confirm_samples"] + 1):
            return None
        lo_idx = min(range(len(pts)), key=lambda i: (pts[i][1], -i))
        start_ts, start = pts[lo_idx]
        cur = pts[-1][1]
        move = cur - start
        if move < p["min_move_cents"]:
            return None
        leg = pts[lo_idx:]
        changes = [b[1] - a[1] for a, b in zip(leg, leg[1:]) if abs(b[1] - a[1]) > 1e-9]
        persistence = (sum(1 for c in changes if c > 0) / len(changes)) if changes else 0.0
        tail = pts[-(p["confirm_samples"] + 1):]
        confirmed = all(b[1] >= a[1] - 1e-9 for a, b in zip(tail, tail[1:])) and tail[-1][1] > tail[0][1]

        f = ctx.features
        ask, bid = f.side_ask(self.side), f.side_bid(self.side)
        if ask is None or bid is None:
            return None
        vel = ctx.velocity_short(self.side)
        acc = ctx.acceleration(self.side)
        vol_ratio = f.volume_accel if f.volume_rate_baseline > 0 else 0.0

        target = ask + p["target_cents"]
        stop = bid - p["stop_cents"]
        gross = target - ask
        costs = self.costs(ctx, ask, target)
        net = gross - costs
        details = self.common_details(ctx) | {
            "move_cents": round(move, 2), "move_start": round(start, 2),
            "move_seconds": round(ctx.now - start_ts, 1), "persistence": round(persistence, 3),
            "confirmed": confirmed, "volume_ratio": round(vol_ratio, 3), "headroom": round(99 - ask, 2),
        }
        reject: list[str] = []
        if persistence < p["min_persistence"]:
            reject.append(f"choppy: persistence {persistence:.0%} < {p['min_persistence']:.0%}")
        if not confirmed:
            reject.append(f"not confirmed over the last {p['confirm_samples']} observations")
        if vel < p["min_velocity"]:
            reject.append(f"velocity {vel:+.2f}c/min < {p['min_velocity']}")
        if acc < -0.5 * p["min_velocity"]:
            reject.append(f"decelerating ({acc:+.2f}c/min)")
        if vol_ratio < p["min_volume_ratio"]:
            reject.append(f"no volume confirmation: volume ratio {vol_ratio:.2f} < {p['min_volume_ratio']}")
        if move > p["max_extension_cents"]:
            reject.append(f"exhausted: move already {move:.1f}c > {p['max_extension_cents']}c (don't chase)")
        if 99 - ask < p["min_headroom_cents"]:
            reject.append(f"no headroom: ask {ask:.0f}c leaves {99 - ask:.0f}c to the ceiling")
        if net < ctx.costs_cfg.min_net_edge_cents:
            reject.append(f"insufficient edge: expected net {net:.1f}c < {ctx.costs_cfg.min_net_edge_cents}c")
        if ctx.state.has("EXHAUSTION"):
            reject.append("market state EXHAUSTION")
        if ctx.sports is not None and ctx.sports.status.lower() in ("final", "closed", "complete", "ended"):
            reject.append("event is final")

        comps = self.base_components(ctx, net, costs) | {
            "magnitude": move / (2.0 * p["min_move_cents"]),
            "velocity": vel / (2.0 * p["min_velocity"]),
            "confirmation": persistence * (1.0 if confirmed else 0.5),
        }
        # an extended move is worth less even inside the limit
        comps["magnitude"] *= max(0.0, 1.0 - max(0.0, move - p["min_move_cents"]) / max(p["max_extension_cents"], 1))
        comps["magnitude"] = max(comps["magnitude"], 0.2)
        reasons = [
            f"{'NO' if self.side == 'no' else 'YES'} price up {move:.1f}c from {start:.1f}c in "
            f"{details['move_seconds']:.0f}s with {persistence:.0%} persistence",
            f"velocity {vel:+.2f}c/min, acceleration {acc:+.2f}, volume ratio {vol_ratio:.2f}",
            f"target {target:.1f}c, stop {stop:.1f}c, trail {p['trail_cents']}c",
            f"expected net {net:.1f}c after {costs:.1f}c fees+slippage",
        ]
        sig = self.make_signal(ctx, "BUY", ask, target, stop, gross, costs, reasons, details, comps, reject)
        sig.details["trail_cents"] = p["trail_cents"]
        return sig
