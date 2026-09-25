"""How many sets should we actually trade?

"An arbitrage exists" is not a decision. Fees round UP to the cent on every
leg, so the same structure can lose money at 1 set and make money at 400.
This module sweeps the quantity range and reports the honest answer.
"""
from __future__ import annotations

from dataclasses import dataclass

from .fees import FeeModel


@dataclass
class QuantityPoint:
    sets: int
    gross_cost: float
    gross_edge: float
    fees: float
    net_edge: float

    @property
    def net_per_set(self) -> float:
        return self.net_edge / self.sets if self.sets else 0.0

    @property
    def fee_drag_pct(self) -> float:
        return 100.0 * self.fees / self.gross_edge if self.gross_edge > 0 else 999.0


@dataclass
class QuantityAnalysis:
    max_executable: int
    min_profitable: int          # 0 if never profitable
    optimal: int                 # sets maximising total net edge
    curve: list[QuantityPoint]

    def at(self, sets: int) -> QuantityPoint | None:
        return next((p for p in self.curve if p.sets == sets), None)

    @property
    def best(self) -> QuantityPoint | None:
        return self.at(self.optimal)


def analyse(
    cost_fn,                    # sets -> (gross_cost_usd, gross_payout_floor_usd)
    leg_prices_fn,              # sets -> list of (price_cents, contracts) per leg
    fees: FeeModel,
    max_executable: int,
    min_net_per_set: float = 0.0,
    max_points: int = 40,
) -> QuantityAnalysis:
    """Sweep quantities and find where this structure actually pays.

    cost_fn and leg_prices_fn take a set count because walking deeper into the
    book changes the blended price - the cost is not linear in quantity.
    """
    if max_executable <= 0:
        return QuantityAnalysis(0, 0, 0, [])

    candidates = _sample(max_executable, max_points)
    curve: list[QuantityPoint] = []
    for sets in candidates:
        gross_cost, payout_floor = cost_fn(sets)
        if gross_cost <= 0:
            continue
        fee_total = sum(fees.entry_fee_usd(price, qty)
                        for price, qty in leg_prices_fn(sets))
        gross_edge = payout_floor - gross_cost
        curve.append(QuantityPoint(sets, gross_cost, gross_edge, fee_total,
                                   gross_edge - fee_total))

    profitable = [p for p in curve if p.net_per_set >= min_net_per_set and p.net_edge > 0]
    min_profitable = min((p.sets for p in profitable), default=0)
    optimal = max(profitable, key=lambda p: p.net_edge).sets if profitable else 0
    return QuantityAnalysis(max_executable, min_profitable, optimal, curve)


def _sample(maximum: int, budget: int) -> list[int]:
    """Every quantity when there are few; a log-ish spread when there are many.
    Always includes 1 and the maximum, because those are the two ends that
    decide whether fee rounding kills the trade."""
    if maximum <= budget:
        return list(range(1, maximum + 1))
    pts = {1, maximum}
    step = maximum / (budget - 2)
    for i in range(1, budget - 1):
        pts.add(max(1, min(maximum, int(round(i * step)))))
    return sorted(pts)
