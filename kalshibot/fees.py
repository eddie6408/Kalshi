"""Kalshi fee model.

Kalshi's published trading fee is a parabola that peaks at 50c:

    taker fee (dollars) = roundup_to_cent( rate * C * P * (1 - P) )

where C is the number of contracts and P is the price IN DOLLARS of the
contract you are buying (0.01 - 0.99). rate is 0.07 on standard series.
Maker (resting, post-only) orders are charged at a much smaller rate on the
series that have them.

IMPORTANT: fee schedules change. `config.yaml -> fees` holds the rates so you
can update them without touching code. Check Kalshi's fee schedule before
going live, and keep fees.safety_multiplier above 1.0 so the bot is
pessimistic about its own costs.
"""
from __future__ import annotations

import math


def _ceil_cents(dollars: float) -> float:
    """Round a dollar amount UP to the next whole cent."""
    return math.ceil(round(dollars * 100, 9)) / 100.0


def taker_fee_usd(price_cents: float, contracts: int, rate: float = 0.07,
                  safety_multiplier: float = 1.0) -> float:
    if contracts <= 0:
        return 0.0
    p = max(0.0, min(1.0, price_cents / 100.0))
    return _ceil_cents(rate * contracts * p * (1.0 - p)) * safety_multiplier


def maker_fee_usd(price_cents: float, contracts: int, rate: float = 0.0025,
                  safety_multiplier: float = 1.0) -> float:
    if contracts <= 0 or rate <= 0:
        return 0.0
    p = max(0.0, min(1.0, price_cents / 100.0))
    return _ceil_cents(rate * contracts * p) * safety_multiplier


class FeeModel:
    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.taker_rate = float(cfg.get("taker_rate", 0.07))
        self.maker_rate = float(cfg.get("maker_rate", 0.0025))
        self.assume_taker = bool(cfg.get("assume_taker", True))
        self.safety = float(cfg.get("safety_multiplier", 1.0))

    def entry_fee_usd(self, price_cents: float, contracts: int, is_maker: bool = False) -> float:
        if is_maker and not self.assume_taker:
            return maker_fee_usd(price_cents, contracts, self.maker_rate, self.safety)
        return taker_fee_usd(price_cents, contracts, self.taker_rate, self.safety)

    def round_trip_fee_usd(self, entry_cents: float, exit_cents: float, contracts: int) -> float:
        """Cost of getting in AND back out. Use this when you plan to exit
        before settlement rather than holding to $0/$1."""
        return (self.entry_fee_usd(entry_cents, contracts)
                + self.entry_fee_usd(exit_cents, contracts))

    def fee_per_contract_usd(self, price_cents: float, contracts: int = 1) -> float:
        n = max(1, contracts)
        return self.entry_fee_usd(price_cents, n) / n
