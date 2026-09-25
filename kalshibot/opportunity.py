"""The normalized opportunity object.

Every strategy - arbitrage today, Fed/crypto/econ/weather later - emits one
of these. The execution layer never needs to know where it came from.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .depth import Walk


class ExecutionRisk(str, Enum):
    """How badly a partial fill would hurt."""
    NONE = "none"           # single leg, nothing to break
    LOW = "low"             # 2 legs, both deep
    MEDIUM = "medium"       # 2 legs, one thin, or 3-4 legs
    HIGH = "high"           # many legs, or thin books


@dataclass
class Leg:
    ticker: str
    side: str                 # "yes" | "no"
    action: str               # "buy" | "sell"
    quantity: int
    limit_price_cents: int    # the worst price we will accept
    vwap_cents: float = 0.0   # blended price from walking the book
    available: int = 0        # total contracts at or better than our limit
    title: str = ""
    walk: Walk | None = None

    @property
    def cost_usd(self) -> float:
        return self.quantity * self.vwap_cents / 100.0

    def __str__(self) -> str:
        return (f"{self.action.upper()} {self.quantity} {self.side.upper()} "
                f"{self.ticker} @ {self.vwap_cents:.2f}c (limit {self.limit_price_cents}c)")


@dataclass
class ArbitrageOpportunity:
    strategy_type: str                     # same_market | ladder | exclusive_basket
    event_id: str
    market_tickers: list[str]
    legs: list[Leg]
    quantity: int                          # number of complete sets

    gross_cost: float = 0.0                # USD, from walking real books
    guaranteed_min_payout: float = 0.0     # USD, worst case across all outcomes
    max_payout: float = 0.0                # USD, best case
    gross_edge: float = 0.0                # guaranteed_min_payout - gross_cost
    estimated_fees: float = 0.0
    net_edge: float = 0.0                  # gross_edge - fees - safety buffer
    net_edge_per_contract: float = 0.0
    expected_return_pct: float = 0.0
    execution_risk: ExecutionRisk = ExecutionRisk.MEDIUM

    guaranteed: bool = True                # False for forecast-derived edges

    # --- forecast-derived edges only (guaranteed=False) ----------------
    # A structural trade needs no opinion about the world. A forecast trade
    # is entirely an opinion, so the opinion has to travel with it: what we
    # thought, how much we trusted it, and what the market thought instead.
    probability_yes: float = 0.0        # OUR probability the YES side resolves
    implied_probability: float = 0.0    # what the price we paid implies
    confidence: float = 0.0             # 0..1, how much we trust our estimate
    estimator: str = ""                 # which estimator produced it
    ev_usd: float = 0.0                 # expected dollars, net of fees

    @property
    def probability_edge(self) -> float:
        """Our probability minus the market's, in probability points, on the
        side we are actually buying. Positive means we think we are right."""
        return self.probability_yes - self.implied_probability
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    rationale: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    # Quantity analysis
    max_executable_quantity: int = 0
    min_profitable_quantity: int = 0
    optimal_quantity: int = 0
    fee_drag_pct: float = 0.0

    @property
    def n_legs(self) -> int:
        return len(self.legs)

    @property
    def age_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.timestamp).total_seconds()

    def is_stale(self, max_age_seconds: float) -> bool:
        return self.age_seconds > max_age_seconds

    def fingerprint(self) -> str:
        """Identity of the structure, ignoring price. Used to spot repeats."""
        parts = sorted(f"{l.ticker}:{l.side}:{l.action}" for l in self.legs)
        return f"{self.strategy_type}|{'|'.join(parts)}"

    def summary(self) -> str:
        return (f"{self.strategy_type} {self.n_legs}L x{self.quantity} "
                f"cost ${self.gross_cost:.2f} floor ${self.guaranteed_min_payout:.2f} "
                f"fees ${self.estimated_fees:.2f} net ${self.net_edge:+.2f}")

    def explain(self) -> str:
        lines = [
            f"  strategy            : {self.strategy_type}",
            f"  event               : {self.event_id}",
            f"  legs                : {self.n_legs}",
        ]
        for leg in self.legs:
            lines.append(f"      {leg}")
        lines += [
            f"  sets                : {self.quantity} "
            f"(max executable {self.max_executable_quantity}, "
            f"min profitable {self.min_profitable_quantity})",
            f"  executable cost     : ${self.gross_cost:.2f}",
        ]
        if self.guaranteed:
            lines.append(
                f"  guaranteed payout   : ${self.guaranteed_min_payout:.2f} "
                f"(best case ${self.max_payout:.2f})")
        else:
            # A forecast trade has no floor, so printing "$0.00 guaranteed"
            # would be technically true and completely useless. Show the
            # opinion instead - that IS the trade.
            side = self.legs[0].side.upper() if self.legs else "?"
            ours_side = (self.probability_yes if side == "YES"
                         else 1.0 - self.probability_yes)
            mkt_side = (self.implied_probability if side == "YES"
                        else 1.0 - self.implied_probability)
            lines += [
                f"  NO GUARANTEED PAYOUT: this is a forecast. "
                f"Worst case is -${self.gross_cost:.2f} "
                f"(best case +${self.max_payout - self.gross_cost:.2f})",
                f"  our probability     : {self.probability_yes * 100:.1f}% YES "
                f"= {ours_side * 100:.1f}% on the {side} we are buying "
                f"(estimator: {self.estimator}, confidence "
                f"{self.confidence:.0%})",
                f"  market implies      : {self.implied_probability * 100:.1f}% "
                f"YES = {mkt_side * 100:.1f}% on the {side} we are buying",
                f"  discrepancy         : "
                f"{abs(self.probability_edge) * 100:.1f} probability points",
                f"  expected value      : ${self.ev_usd:+.2f}",
            ]
        lines += [
            f"  gross edge          : ${self.gross_edge:+.2f}",
            f"  fees                : ${self.estimated_fees:.2f} "
            f"({self.fee_drag_pct:.1f}% of gross edge)",
            f"  net edge            : ${self.net_edge:+.2f} "
            f"(${self.net_edge_per_contract:+.4f}/contract)",
            f"  return on cost      : {self.expected_return_pct:+.2f}%",
            f"  execution risk      : {self.execution_risk.value}",
            f"  guaranteed          : {self.guaranteed}",
            f"  rationale           : {self.rationale}",
        ]
        return "\n".join(lines)
