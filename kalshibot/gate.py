"""The single profitability gate.

Nothing reaches the exchange without passing through here. One place, one set
of thresholds, every strategy - arbitrage today, forecast strategies later.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from .opportunity import ArbitrageOpportunity, ExecutionRisk

log = logging.getLogger(__name__)


class Reject(str, Enum):
    OK = "OK"
    ZERO_QUANTITY = "ZERO_QUANTITY"
    FEE_DRAG = "FEE_DRAG"
    BELOW_MIN_EDGE = "BELOW_MIN_EDGE"
    BELOW_MIN_EDGE_PCT = "BELOW_MIN_EDGE_PCT"
    BELOW_MIN_PROFIT = "BELOW_MIN_PROFIT"
    TOO_MANY_LEGS = "TOO_MANY_LEGS"
    EXECUTION_RISK = "EXECUTION_RISK"
    STALE = "STALE"
    NEGATIVE_FLOOR = "NEGATIVE_FLOOR"
    NOT_GUARANTEED = "NOT_GUARANTEED"
    IMPLAUSIBLE_RETURN = "IMPLAUSIBLE_RETURN"
    FORECASTS_DISABLED = "FORECASTS_DISABLED"
    BELOW_MIN_PROBABILITY_EDGE = "BELOW_MIN_PROBABILITY_EDGE"
    BELOW_MIN_CONFIDENCE = "BELOW_MIN_CONFIDENCE"
    NEGATIVE_EV = "NEGATIVE_EV"


@dataclass
class GateDecision:
    accepted: bool
    reason: Reject
    detail: str = ""
    checks: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        verdict = "TRADE" if self.accepted else "REJECT"
        return f"{verdict} [{self.reason.value}] {self.detail}"


class ProfitabilityGate:
    def __init__(self, cfg: dict | None = None):
        c = cfg or {}
        self.min_net_edge_usd = float(c.get("min_net_edge_usd", 0.02))
        self.min_net_edge_cents_per_contract = float(
            c.get("min_net_edge_cents_per_contract", 0.5))
        self.min_net_edge_percent = float(c.get("min_net_edge_percent", 0.25))
        self.min_profit_per_trade_usd = float(c.get("min_profit_per_trade_usd", 0.02))
        self.max_fee_drag_percent = float(c.get("max_fee_drag_percent", 80.0))
        self.max_legs = int(c.get("max_legs", 14))
        self.max_execution_risk = str(c.get("max_execution_risk", "medium")).lower()
        self.max_opportunity_age_seconds = float(
            c.get("max_opportunity_age_seconds", 20))
        self.require_guaranteed = bool(c.get("require_guaranteed", True))
        self.max_guaranteed_return_percent = float(
            c.get("max_guaranteed_return_percent", 100.0))
        # --- forecast-derived edges ------------------------------------
        # A forecast trade has no payout floor, so it cannot be judged by the
        # structural rules. These are its rules. allow_forecast defaults to
        # False so turning forecasts on is always a deliberate act.
        self.allow_forecast = bool(c.get("allow_forecast", False))
        self.min_probability_edge = float(c.get("min_probability_edge", 0.05))
        self.min_confidence = float(c.get("min_confidence", 0.50))
        self.min_ev_usd = float(c.get("min_ev_usd", 0.05))

    _RISK_ORDER = {ExecutionRisk.NONE: 0, ExecutionRisk.LOW: 1,
                   ExecutionRisk.MEDIUM: 2, ExecutionRisk.HIGH: 3}

    def _forecast_checks(self, op: ArbitrageOpportunity,
                         checks: list[str]) -> GateDecision:
        """Rules for an edge that comes from an opinion rather than from the
        contracts. Nothing here may ever call the trade guaranteed."""
        if not self.allow_forecast or self.require_guaranteed:
            return GateDecision(
                False, Reject.FORECASTS_DISABLED,
                "forecast strategies are off (profitability.allow_forecast is "
                "false, or require_guaranteed is still true)", checks)

        if op.confidence < self.min_confidence:
            return GateDecision(
                False, Reject.BELOW_MIN_CONFIDENCE,
                f"confidence {op.confidence:.2f} < {self.min_confidence:.2f}",
                checks)
        checks.append(f"confidence {op.confidence:.2f}")

        edge_pts = op.probability_edge if op.legs and op.legs[0].side == "yes" \
            else -op.probability_edge
        if abs(edge_pts) < self.min_probability_edge:
            return GateDecision(
                False, Reject.BELOW_MIN_PROBABILITY_EDGE,
                f"{abs(edge_pts) * 100:.1f} probability points < "
                f"{self.min_probability_edge * 100:.1f}", checks)
        checks.append(f"{abs(edge_pts) * 100:.1f} probability points of edge")

        if op.ev_usd <= 0 or op.net_edge <= 0:
            return GateDecision(
                False, Reject.NEGATIVE_EV,
                f"expected value ${op.ev_usd:+.2f} is not positive", checks)
        if op.ev_usd < self.min_ev_usd:
            return GateDecision(
                False, Reject.NEGATIVE_EV,
                f"expected value ${op.ev_usd:+.2f} < ${self.min_ev_usd:.2f}",
                checks)
        checks.append(f"expected value ${op.ev_usd:+.2f}")

        if op.n_legs > self.max_legs:
            return GateDecision(False, Reject.TOO_MANY_LEGS,
                                f"{op.n_legs} legs > max {self.max_legs}", checks)

        if op.is_stale(self.max_opportunity_age_seconds):
            return GateDecision(
                False, Reject.STALE,
                f"priced {op.age_seconds:.1f}s ago, limit "
                f"{self.max_opportunity_age_seconds:.0f}s", checks)
        checks.append(f"age {op.age_seconds:.1f}s fresh")

        allowed = self._RISK_ORDER.get(ExecutionRisk(self.max_execution_risk), 2)
        if self._RISK_ORDER[op.execution_risk] > allowed:
            return GateDecision(
                False, Reject.EXECUTION_RISK,
                f"execution risk {op.execution_risk.value} exceeds "
                f"{self.max_execution_risk}", checks)
        checks.append(f"execution risk {op.execution_risk.value} acceptable")

        if op.fee_drag_pct > self.max_fee_drag_percent:
            return GateDecision(
                False, Reject.FEE_DRAG,
                f"fees are {op.fee_drag_pct:.0f}% of gross edge "
                f"(${op.estimated_fees:.2f} of ${op.gross_edge:.2f})", checks)
        checks.append(f"fee drag {op.fee_drag_pct:.0f}% ok")

        return GateDecision(
            True, Reject.OK,
            f"EV ${op.ev_usd:+.2f} on ${op.gross_cost:.2f} at "
            f"{op.confidence:.0%} confidence (NOT guaranteed)", checks)

    def evaluate(self, op: ArbitrageOpportunity) -> GateDecision:
        checks: list[str] = []

        if op.quantity <= 0:
            return GateDecision(False, Reject.ZERO_QUANTITY,
                                "optimal quantity is zero", checks)
        checks.append(f"quantity {op.quantity} > 0")

        # ---- the fork ----------------------------------------------------
        # A structural trade is judged by the payout its contracts guarantee.
        # A forecast trade has no such floor and must be judged on expected
        # value instead. Sending a forecast through the structural checks
        # would reject every one of them; sending a structural trade through
        # the forecast checks would drop its most important protection.
        if not op.guaranteed:
            return self._forecast_checks(op, checks)

        if op.guaranteed_min_payout <= op.gross_cost:
            return GateDecision(
                False, Reject.NEGATIVE_FLOOR,
                f"payout floor ${op.guaranteed_min_payout:.2f} does not exceed "
                f"cost ${op.gross_cost:.2f}", checks)
        checks.append(f"floor ${op.guaranteed_min_payout:.2f} > cost ${op.gross_cost:.2f}")

        # A structure that claims a risk-free return this large is far more
        # likely to be a modelling error than free money. Refuse it and say so,
        # rather than sending an order built on an assumption nobody checked.
        if (op.guaranteed and self.max_guaranteed_return_percent > 0
                and op.expected_return_pct > self.max_guaranteed_return_percent):
            return GateDecision(
                False, Reject.IMPLAUSIBLE_RETURN,
                f"claims a guaranteed {op.expected_return_pct:.1f}% return, above "
                f"the {self.max_guaranteed_return_percent:.0f}% sanity limit - "
                f"treat the payout floor as unproven, not as an opportunity",
                checks)
        checks.append(f"return {op.expected_return_pct:.2f}% within sanity limit")

        if op.n_legs > self.max_legs:
            return GateDecision(False, Reject.TOO_MANY_LEGS,
                                f"{op.n_legs} legs > max {self.max_legs}", checks)
        checks.append(f"{op.n_legs} legs <= {self.max_legs}")

        if op.is_stale(self.max_opportunity_age_seconds):
            return GateDecision(
                False, Reject.STALE,
                f"priced {op.age_seconds:.1f}s ago, limit "
                f"{self.max_opportunity_age_seconds:.0f}s", checks)
        checks.append(f"age {op.age_seconds:.1f}s fresh")

        allowed = self._RISK_ORDER.get(
            ExecutionRisk(self.max_execution_risk), 2)
        if self._RISK_ORDER[op.execution_risk] > allowed:
            return GateDecision(
                False, Reject.EXECUTION_RISK,
                f"execution risk {op.execution_risk.value} exceeds "
                f"{self.max_execution_risk}", checks)
        checks.append(f"execution risk {op.execution_risk.value} acceptable")

        if op.fee_drag_pct > self.max_fee_drag_percent:
            return GateDecision(
                False, Reject.FEE_DRAG,
                f"fees are {op.fee_drag_pct:.0f}% of gross edge "
                f"(${op.estimated_fees:.2f} of ${op.gross_edge:.2f})", checks)
        checks.append(f"fee drag {op.fee_drag_pct:.0f}% ok")

        if op.net_edge < self.min_net_edge_usd:
            return GateDecision(
                False, Reject.BELOW_MIN_EDGE,
                f"net edge ${op.net_edge:+.4f} < ${self.min_net_edge_usd:.2f}", checks)
        checks.append(f"net edge ${op.net_edge:+.4f}")

        if op.net_edge_per_contract * 100 < self.min_net_edge_cents_per_contract:
            return GateDecision(
                False, Reject.BELOW_MIN_EDGE,
                f"{op.net_edge_per_contract * 100:.3f}c/contract < "
                f"{self.min_net_edge_cents_per_contract:.2f}c", checks)
        checks.append(f"{op.net_edge_per_contract * 100:.3f}c per contract")

        if op.expected_return_pct < self.min_net_edge_percent:
            return GateDecision(
                False, Reject.BELOW_MIN_EDGE_PCT,
                f"return {op.expected_return_pct:.3f}% < "
                f"{self.min_net_edge_percent:.2f}%", checks)
        checks.append(f"return {op.expected_return_pct:.2f}%")

        if op.net_edge < self.min_profit_per_trade_usd:
            return GateDecision(
                False, Reject.BELOW_MIN_PROFIT,
                f"${op.net_edge:.4f} is not worth the execution risk", checks)
        checks.append(f"profit ${op.net_edge:.2f} worth doing")

        return GateDecision(True, Reject.OK,
                            f"net ${op.net_edge:+.2f} on ${op.gross_cost:.2f} "
                            f"({op.expected_return_pct:+.2f}%)", checks)
