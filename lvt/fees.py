"""Kalshi trading fees.

Kalshi publishes a fee schedule per series (`fee_type`, `fee_multiplier` on
GET /series). The general taker formula is

    fee = round_up_to_cent( 0.07 * multiplier * C * P * (1 - P) )

with P the contract price in dollars and C the contract count. Series with
`quadratic_with_maker_fees` also charge makers 0.0175 * ...; plain
`quadratic` series charge makers nothing. This bot only ever TAKES liquidity
(marketable IOC orders), so the taker rate applies to every entry and exit.
Settlement is free.

Unknown fee types fall back to the standard taker rate - overestimating
fees is the safe direction for a paper simulator.
"""
from __future__ import annotations

import math

TAKER_RATE = 0.07
MAKER_RATE = 0.0175


def fee_usd(count: float, price_cents: float, fee_type: str = "quadratic",
            multiplier: float = 1.0, maker: bool = False) -> float:
    if count <= 0:
        return 0.0
    p = min(max(price_cents / 100.0, 0.0), 1.0)
    ft = (fee_type or "quadratic").lower()
    if maker:
        rate = MAKER_RATE if ft == "quadratic_with_maker_fees" else 0.0
    else:
        rate = TAKER_RATE
    mult = multiplier if multiplier and multiplier > 0 else 1.0
    raw = rate * mult * count * p * (1.0 - p)
    return math.ceil(raw * 100.0 - 1e-9) / 100.0


def fee_per_contract_cents(price_cents: float, fee_type: str = "quadratic",
                           multiplier: float = 1.0, count: int = 1, maker: bool = False) -> float:
    """Average fee per contract in cents for an order of `count` contracts.
    Rounding up to the cent is per order, so small orders pay more per
    contract; pass the planned count for an honest estimate."""
    count = max(int(count), 1)
    return fee_usd(count, price_cents, fee_type, multiplier, maker=maker) * 100.0 / count
