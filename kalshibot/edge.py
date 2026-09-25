"""Expected value and sizing math.

Everything here is deterministic. The estimator supplies a probability; this
module decides whether that probability is worth money, and how much.

Conventions
-----------
* Prices are integer CENTS (1..99). A contract pays $1.00 if it resolves true.
* `q` is OUR probability that the YES side resolves true.
* Buying YES at price p costs $p and returns $1 if YES happens.
* Buying NO  at price n costs $n and returns $1 if YES does NOT happen.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .fees import FeeModel


@dataclass
class EdgeResult:
    side: str                  # "yes" | "no"
    price_cents: int           # what we would pay
    win_probability: float     # our probability that THIS side pays out
    gross_edge: float          # win_probability - price_in_dollars
    fee_per_contract: float
    edge_after_fees: float
    ev_per_contract_usd: float
    tradable: bool
    reason: str = ""


def _one_side(side: str, price_cents: int, win_prob: float, fees: FeeModel) -> EdgeResult:
    p = price_cents / 100.0
    gross = win_prob - p
    fee_pc = fees.fee_per_contract_usd(price_cents, contracts=1)
    after = gross - fee_pc
    return EdgeResult(
        side=side,
        price_cents=price_cents,
        win_probability=win_prob,
        gross_edge=gross,
        fee_per_contract=fee_pc,
        edge_after_fees=after,
        ev_per_contract_usd=after,   # payoff is $1, so EV per contract == edge
        tradable=False,
    )


def evaluate(q_yes: float, yes_ask_cents: int, no_ask_cents: int, fees: FeeModel) -> EdgeResult:
    """Return the better of buying YES or buying NO, priced at the ask.

    We always price ourselves at the ask (i.e. we assume we cross the spread
    and pay up). That is the pessimistic, honest assumption; if a resting
    order fills better, that is upside.
    """
    candidates: list[EdgeResult] = []
    if 0 < yes_ask_cents < 100:
        candidates.append(_one_side("yes", yes_ask_cents, q_yes, fees))
    if 0 < no_ask_cents < 100:
        candidates.append(_one_side("no", no_ask_cents, 1.0 - q_yes, fees))
    if not candidates:
        return EdgeResult("none", 0, 0.0, 0.0, 0.0, -1.0, -1.0, False, "no two-sided market")
    return max(candidates, key=lambda c: c.edge_after_fees)


def kelly_fraction(win_prob: float, price_cents: int) -> float:
    """Full-Kelly fraction of bankroll for a $1-payout binary contract.

        f* = (q - p) / (1 - p)

    Returns 0 when the bet is not favourable.
    """
    p = price_cents / 100.0
    if not 0 < p < 1:
        return 0.0
    f = (win_prob - p) / (1.0 - p)
    return max(0.0, min(1.0, f))


def contracts_from_kelly(
    win_prob: float,
    price_cents: int,
    bankroll_usd: float,
    kelly_multiplier: float,
    confidence: float = 1.0,
) -> int:
    """Convert a Kelly fraction into a whole number of contracts.

    We scale by `confidence` as well, so a shaky estimate gets a smaller bet
    even when the nominal edge looks large.
    """
    f = kelly_fraction(win_prob, price_cents) * kelly_multiplier * max(0.0, min(1.0, confidence))
    if f <= 0:
        return 0
    dollars_at_risk = bankroll_usd * f
    cost_per_contract = price_cents / 100.0
    # +1e-9 so that a value like 49.999999999 (floating point for exactly $50)
    # does not silently cost us a contract.
    return int(math.floor(dollars_at_risk / cost_per_contract + 1e-9))


def ev_usd(win_prob: float, price_cents: int, contracts: int, fees: FeeModel) -> float:
    """Total expected dollars for a position held to settlement."""
    if contracts <= 0:
        return 0.0
    p = price_cents / 100.0
    gross = contracts * (win_prob - p)
    return gross - fees.entry_fee_usd(price_cents, contracts)
