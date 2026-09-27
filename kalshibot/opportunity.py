"""The normalized opportunity object.

Every strategy - arbitrage today, Fed/crypto/econ/weather later - emits one
of these. The execution layer never needs to know where it came from.

Package 1 (research brief section 4) made it the standard record of a
potential trade: who found it, what it was expected to be worth and how that
number is built (ev_breakdown), what is MEASURED versus MODELLED versus not
modelled at all, which code/config/model/fee versions produced it, and what
was decided at which stage. `Opportunity` is the canonical name;
`ArbitrageOpportunity` remains as an alias so nothing that imports it breaks.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from . import versioning
from .depth import Walk


class OpportunityClass(str, Enum):
    """Research brief section 5. Only the first and fifth exist today."""
    STRUCTURAL_ARB = "STRUCTURAL_ARB"
    CROSS_CONTRACT = "CROSS_CONTRACT"
    CROSS_MARKET = "CROSS_MARKET"
    INFO_LATENCY = "INFO_LATENCY"
    WEATHER_FORECAST = "WEATHER_FORECAST"
    FORECAST = "FORECAST"                  # a forecast estimator other than weather
    MARKET_MAKING = "MARKET_MAKING"
    STAT_MISPRICING = "STAT_MISPRICING"
    UNCLASSIFIED = "UNCLASSIFIED"


class DecisionStage(str, Enum):
    """Where a decision was made - so model error, data problems, execution
    problems and risk constraints can be told apart (brief section 18)."""
    MODEL = "MODEL"                 # the estimator declined or the edge was too small
    DATA = "DATA"                   # data-quality gates
    MARKET_SKILL = "MARKET_SKILL"   # the model has not shown it beats the market
    POLICY = "POLICY"               # deliberate rules: lead-time window, price band
    CALIBRATION = "CALIBRATION"
    LIQUIDITY = "LIQUIDITY"         # depth / spread
    GATE = "GATE"                   # the profitability gate
    RISK = "RISK"                   # account limits and circuit breakers
    EXECUTION = "EXECUTION"


class Basis(str, Enum):
    """How a number in the EV breakdown was obtained."""
    MEASURED = "MEASURED"           # read from the book or the contract terms
    MODELLED = "MODELLED"           # computed by a model we maintain
    NOT_MODELLED = "NOT_MODELLED"   # unknown - never silently treated as zero


@dataclass(frozen=True)
class EVComponent:
    name: str
    usd: float | None               # None exactly when basis is NOT_MODELLED
    basis: Basis
    note: str = ""


@dataclass(frozen=True)
class EVBreakdown:
    """NET EV = payout EV - cost at the touch - slippage - fees
                - adverse selection - execution loss - risk adjustment.

    `net_ev` sums only what is known. `complete` is False while any cost is
    NOT_MODELLED, and `unmodelled` names them, so a later package can see
    exactly which assumptions are still open instead of inheriting a zero.
    Informational (not part of the sum): spread_cost, the part of the cost
    at the touch that is paid away by crossing from the mid.
    """
    components: tuple[EVComponent, ...]
    net_ev: float
    spread_cost: EVComponent

    @property
    def complete(self) -> bool:
        return not self.unmodelled

    @property
    def unmodelled(self) -> list[str]:
        return [c.name for c in self.components if c.basis is Basis.NOT_MODELLED]

    def get(self, name: str) -> EVComponent | None:
        return next((c for c in self.components if c.name == name), None)

    def to_dict(self) -> dict:
        d = {c.name: {"usd": None if c.usd is None else round(c.usd, 6),
                      "basis": c.basis.value, **({"note": c.note} if c.note else {})}
             for c in (*self.components, self.spread_cost)}
        d["net_ev"] = round(self.net_ev, 6)
        d["complete"] = self.complete
        d["unmodelled"] = self.unmodelled
        return d


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
    #: the touch on this leg's side when it was priced (Package 1), so the
    #: cost can be split into mid, spread and slippage. None = not recorded.
    best_ask_cents: int | None = None
    best_bid_cents: int | None = None

    @property
    def cost_usd(self) -> float:
        return self.quantity * self.vwap_cents / 100.0

    def __str__(self) -> str:
        return (f"{self.action.upper()} {self.quantity} {self.side.upper()} "
                f"{self.ticker} @ {self.vwap_cents:.2f}c (limit {self.limit_price_cents}c)")


@dataclass
class Opportunity:
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

    # --- Package 1: identity, classification, versions, decision ---------
    opportunity_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    opportunity_class: OpportunityClass = OpportunityClass.UNCLASSIFIED
    direction: str = ""
    #: probability of the side we would buy - ours, and the price's (at size)
    fair_probability: float | None = None
    market_probability: float | None = None
    #: which book the price came from: "demo" | "production"
    market_price_source: str = "demo"
    settlement_time: datetime | None = None
    #: {"depth_at_best": min over legs, "available": min over legs}
    liquidity: dict[str, Any] = field(default_factory=dict)
    #: "OK" or "DEGRADED: <why>"
    data_quality: str = "UNKNOWN"
    #: the WORST (highest-numbered) source tier behind the price or estimate
    source_quality_tier: int | None = None
    #: fill probability and adverse selection are not modelled yet (brief
    #: sections 10-12); None means unknown, not zero
    est_fill_probability: float | None = None
    adverse_selection_estimate: float | None = None
    risk_score: float | None = None
    correlation_exposure: float | None = None
    model_version: str = ""
    fee_schedule_version: str = ""
    code_version: str = field(default_factory=versioning.code_version)
    config_version: str = field(default_factory=versioning.config_version)
    execution_mode: str = ""
    decision: str = "DETECTED"             # DETECTED | ACCEPTED | REJECTED
    decision_stage: str = ""
    rejection_reason: str = ""

    # --- derived -----------------------------------------------------------
    @property
    def detected_at(self) -> datetime:
        return self.timestamp

    @property
    def guaranteed_value(self) -> float:
        return self.guaranteed_min_payout if self.guaranteed else 0.0

    @property
    def capital_required(self) -> float:
        """Cash committed at entry: what the legs cost plus entry fees."""
        return self.gross_cost + self.estimated_fees

    @property
    def max_loss(self) -> float:
        """Worst case at settlement, as a positive number of dollars."""
        floor = self.guaranteed_min_payout if self.guaranteed else 0.0
        return max(0.0, self.capital_required - floor)

    @property
    def max_profit(self) -> float:
        return self.max_payout - self.capital_required

    @property
    def holding_period_hours(self) -> float | None:
        """Upper bound: time from detection to settlement (every strategy can
        hold to settlement; exits before it only shorten this)."""
        if self.settlement_time is None:
            return None
        return max(0.0, (self.settlement_time - self.timestamp).total_seconds() / 3600.0)

    def ev_breakdown(self) -> EVBreakdown:
        """Where the expected value comes from, component by component."""
        C, M, U = Basis.MEASURED, Basis.MODELLED, Basis.NOT_MODELLED
        # Payout: guaranteed by the contracts, or our probability x $1 a set.
        if self.guaranteed:
            payout = EVComponent("payout_ev", self.guaranteed_min_payout, C,
                                 "worst-case payout set by the contract terms")
        elif self.fair_probability is not None:
            payout = EVComponent("payout_ev", self.fair_probability * self.quantity, M,
                                 f"{self.model_version or 'model'} probability x $1")
        else:
            payout = EVComponent("payout_ev", None, U, "no probability recorded")

        # Cost: at the touch, plus what walking deeper added (slippage).
        touches = [l for l in self.legs if l.best_ask_cents is not None]
        if self.legs and len(touches) == len(self.legs):
            at_touch = sum(l.quantity * l.best_ask_cents for l in self.legs) / 100.0
            slip = EVComponent("slippage", self.gross_cost - at_touch, C,
                               "VWAP of the walked depth minus the best ask")
            touch = EVComponent("cost_at_touch", at_touch, C, "best ask x size")
        else:
            slip = EVComponent("slippage", None, U, "touch not recorded")
            touch = EVComponent("cost_at_touch", self.gross_cost, C,
                                "walked cost (touch not recorded)")
        fees = EVComponent("fees", self.estimated_fees, M,
                           self.fee_schedule_version or "fee model")
        adverse = (EVComponent("adverse_selection", self.adverse_selection_estimate, M)
                   if self.adverse_selection_estimate is not None else
                   EVComponent("adverse_selection", None, U, "no fill model yet"))
        execution = EVComponent("execution_loss", None, U,
                                "partial fills / leg risk not modelled yet")
        buffer = self.gross_edge - self.estimated_fees - self.net_edge
        risk = (EVComponent("risk_adjustment", buffer, M, "strategy safety buffer")
                if abs(buffer) > 1e-9 else
                EVComponent("risk_adjustment", None, U, "none applied"))

        parts = (payout, touch, slip, fees, adverse, execution, risk)
        net = payout.usd or 0.0
        for c in parts[1:]:
            net -= c.usd or 0.0

        mids = [l for l in self.legs
                if l.best_ask_cents is not None and l.best_bid_cents is not None]
        if self.legs and len(mids) == len(self.legs):
            spread = EVComponent(
                "spread_cost",
                sum(l.quantity * (l.best_ask_cents - l.best_bid_cents) / 2.0
                    for l in self.legs) / 100.0, C,
                "half the bid-ask spread x size, paid by crossing from the mid")
        else:
            spread = EVComponent("spread_cost", None, U, "no bid on at least one leg")
        return EVBreakdown(parts, net, spread)

    def to_record(self) -> dict[str, Any]:
        """Flat and JSON-safe, for the opportunity journal (Package 2)."""
        def iso(t):
            return t.isoformat() if t is not None else None
        return {
            "opportunity_id": self.opportunity_id,
            "detected_at": iso(self.timestamp),
            "opportunity_class": self.opportunity_class.value,
            "strategy": self.strategy_type,
            "event_id": self.event_id,
            "markets": list(self.market_tickers),
            "direction": self.direction,
            "legs": [{"ticker": l.ticker, "side": l.side, "action": l.action,
                      "quantity": l.quantity, "limit_price_cents": l.limit_price_cents,
                      "vwap_cents": round(l.vwap_cents, 4),
                      "best_ask_cents": l.best_ask_cents,
                      "best_bid_cents": l.best_bid_cents,
                      "available": l.available} for l in self.legs],
            "quantity": self.quantity,
            "guaranteed": self.guaranteed,
            "fair_probability": self.fair_probability,
            "market_probability": self.market_probability,
            "market_price_source": self.market_price_source,
            "gross_cost": round(self.gross_cost, 6),
            "fees": round(self.estimated_fees, 6),
            "gross_edge": round(self.gross_edge, 6),
            "net_edge": round(self.net_edge, 6),
            "guaranteed_value": round(self.guaranteed_value, 6),
            "capital_required": round(self.capital_required, 6),
            "max_loss": round(self.max_loss, 6),
            "max_profit": round(self.max_profit, 6),
            "settlement_time": iso(self.settlement_time),
            "holding_period_hours": self.holding_period_hours,
            "liquidity": dict(self.liquidity),
            "confidence": self.confidence,
            "data_quality": self.data_quality,
            "source_quality_tier": self.source_quality_tier,
            "est_fill_probability": self.est_fill_probability,
            "adverse_selection_estimate": self.adverse_selection_estimate,
            "execution_risk": self.execution_risk.value,
            "risk_score": self.risk_score,
            "correlation_exposure": self.correlation_exposure,
            "ev": self.ev_breakdown().to_dict(),
            "model_version": self.model_version,
            "fee_schedule_version": self.fee_schedule_version,
            "code_version": self.code_version,
            "config_version": self.config_version,
            "execution_mode": self.execution_mode,
            "decision": self.decision,
            "decision_stage": self.decision_stage,
            "rejection_reason": self.rejection_reason,
        }

    @classmethod
    def stub(cls, *, strategy_type: str, event_id: str, ticker: str,
             stage: "DecisionStage | str", reason: str, detail: str = "",
             opportunity_class: OpportunityClass = OpportunityClass.UNCLASSIFIED,
             probability_yes: float | None = None,
             touch: dict | None = None, settlement_time: datetime | None = None,
             model_version: str = "", fee_schedule_version: str = "",
             evidence: dict | None = None) -> "Opportunity":
        """A market looked at and turned down before a full trade was priced.

        Package 2 records these too - they are most of what the bot decides.
        No legs and no size: what is recorded is what was KNOWN at the moment
        (our probability of YES, the touch on each side) so the outcome can be
        judged later against that, and nothing more.
        """
        stage_value = stage.value if isinstance(stage, DecisionStage) else str(stage)
        return cls(
            strategy_type=strategy_type, event_id=event_id, market_tickers=[ticker],
            legs=[], quantity=0, guaranteed=False,
            probability_yes=probability_yes if probability_yes is not None else 0.0,
            opportunity_class=opportunity_class,
            fair_probability=probability_yes, settlement_time=settlement_time,
            model_version=model_version, fee_schedule_version=fee_schedule_version,
            evidence={**(evidence or {}), "touch": dict(touch or {}),
                      "probability_yes": probability_yes, "stub": True},
            rationale=detail[:400],
            decision="REJECTED", decision_stage=stage_value, rejection_reason=reason)

    @property
    def is_stub(self) -> bool:
        return bool(self.evidence.get("stub"))

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


#: The name every existing module imports. Same class.
ArbitrageOpportunity = Opportunity

#: ExecutionRisk as an ordinal risk score (0 = none .. 3 = high).
RISK_SCORE = {ExecutionRisk.NONE: 0.0, ExecutionRisk.LOW: 1.0,
              ExecutionRisk.MEDIUM: 2.0, ExecutionRisk.HIGH: 3.0}
