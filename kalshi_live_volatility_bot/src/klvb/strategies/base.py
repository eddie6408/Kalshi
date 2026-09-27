"""Strategy framework.

A strategy looks at ONE market at ONE instant through a StrategyContext (only
data with ts <= now) and returns:
  * None            - the setup it looks for is not present at all
  * Signal(BUY)     - a complete, cost-justified entry
  * Signal(NO_TRADE)- the setup is present but a gate failed (falling knife,
                      chasing, insufficient edge, ...). These are recorded as
                      rejected signals so selectivity can be studied.

All logic is expressed in *side space*: for direction DOWN the strategy sees the
NO price (100 - YES), so a single implementation serves both sides and Kalshi's
real "buy NO" mechanism provides the downside exposure.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any

from ..engine.fees import FeeModel
from ..engine.scoring import quality_score, volatility_component
from ..engine.states import MarketState
from ..engine.volatility import Features, MarketSeries
from ..models import Direction, MarketMeta, Signal, SportsContext

REPRESENTATIVE_COUNT = 10  # fee rounding is per order; estimate with a typical order size


@dataclass
class StrategyContext:
    now: float
    meta: MarketMeta
    features: Features
    state: MarketState
    series: MarketSeries
    fees: FeeModel
    costs_cfg: Any
    eligibility_cfg: Any
    vol_cfg: Any
    min_score: float
    sports: SportsContext | None = None
    take_profit_maker: bool = True

    # ---- side-space helpers ----
    def points(self, seconds: float, side: str) -> list[tuple[float, float]]:
        return self.series.window(seconds, self.now, side)

    def velocity_short(self, side: str) -> float:
        v = self.features.velocity_short
        return v if side == "yes" else -v

    def velocity_medium(self, side: str) -> float:
        v = self.features.velocity_medium
        return v if side == "yes" else -v

    def acceleration(self, side: str) -> float:
        a = self.features.acceleration
        return a if side == "yes" else -a


class Strategy(abc.ABC):
    name: str = "BASE"

    def __init__(self, params: dict[str, Any], direction: Direction):
        self.p = params
        self.version: str = params["version"]
        self.direction = direction
        self.side = direction.side

    @abc.abstractmethod
    def evaluate(self, ctx: StrategyContext) -> Signal | None:
        ...

    # ---- shared pieces ----
    def costs(self, ctx: StrategyContext, entry: float, exit_: float) -> float:
        """Per-contract fees (entry+exit) + expected slippage, cents."""
        taker_entry = self.p.get("entry_style", "taker") == "taker"
        taker_exit = not ctx.take_profit_maker
        fees = ctx.fees.round_trip_cost(entry, exit_, REPRESENTATIVE_COUNT, taker_entry, taker_exit, ctx.meta)
        legs = int(taker_entry) + int(taker_exit)
        slip = ctx.costs_cfg.expected_slippage_ticks * ctx.meta.tick_size * legs
        return fees + slip

    def base_components(self, ctx: StrategyContext, expected_net: float, costs: float) -> dict[str, float]:
        f, e, v = ctx.features, ctx.eligibility_cfg, ctx.vol_cfg
        depth = f.side_ask_depth(self.side) or 0.0
        return {
            "volatility": volatility_component(f.realized_vol, v.regime_normal, v.regime_high, v.regime_extreme),
            "volume": min(1.0, f.volume_accel / 2.0) if f.volume_rate_baseline > 0 else 0.3,
            "liquidity": depth / (3.0 * e.min_liquidity_contracts),
            "spread": 1.0 - ((f.spread if f.spread is not None else 99) - 1.0) / max(e.max_spread_cents, 1),
            "edge": expected_net / (expected_net + costs) if expected_net > 0 else 0.0,
            "freshness": 1.0 - (f.data_age if f.data_age is not None else 999) / e.max_data_age_seconds,
        }

    def make_signal(self, ctx: StrategyContext, action: str, entry: float, target: float, stop: float,
                    gross: float, costs: float, reasons: list[str], details: dict[str, Any],
                    components: dict[str, float], reject: list[str] | None = None) -> Signal:
        score = quality_score(components)
        sig = Signal(
            ts=ctx.now, ticker=ctx.meta.ticker, strategy=self.name, strategy_version=self.version,
            direction=self.direction, action=action, entry_ref=entry, target=target, stop=stop,
            score=score, expected_gross=round(gross, 3), expected_costs=round(costs, 3),
            expected_net=round(gross - costs, 3), reasons=reasons, details=details,
            score_components={k: round(max(0.0, min(1.0, v)), 3) for k, v in components.items()},
            max_hold_seconds=float(self.p.get("max_hold_seconds", 900)),
            entry_style=self.p.get("entry_style", "taker"),
        )
        rej = list(reject or [])
        if action == "BUY" and score < ctx.min_score:
            rej.append(f"signal score {score:.0f} < minimum {ctx.min_score:.0f}")
        if rej:
            sig.action = "NO_TRADE"
            sig.reject_reasons = rej
        return sig

    def common_details(self, ctx: StrategyContext) -> dict[str, Any]:
        f = ctx.features
        d = {
            "side": self.side, "volatility_regime": ctx.state.regime, "trend": ctx.state.trend,
            "states": list(ctx.state.states), "spread": f.spread,
            "liquidity": f.side_ask_depth(self.side), "velocity": round(ctx.velocity_short(self.side), 3),
            "acceleration": round(ctx.acceleration(self.side), 3), "realized_vol": round(f.realized_vol, 3),
            "volume_accel": round(f.volume_accel, 3), "data_age": f.data_age, "sport": ctx.meta.sport,
        }
        if ctx.sports is not None:
            d["sports_context"] = {"status": ctx.sports.status, "score": ctx.sports.score,
                                   "period": ctx.sports.period, "clock": ctx.sports.clock}
        return d
