"""Kalshi trading-fee model.

General Trading Fee (taker):  ceil_to_cent( M * 0.07   * C * P * (1 - P) )
Maker fee (only for series with fee_type 'quadratic_with_maker_fees'):
                              ceil_to_cent( M * 0.0175 * C * P * (1 - P) )
where P is the price in dollars, C the contract count and M the series
fee_multiplier. Fees are charged per fill/order and rounded UP to the cent,
which makes small orders relatively more expensive. 'flat' fee series and fee
waivers are also supported. Source: Kalshi fee schedule + /series fee_type docs.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..models import MarketMeta


def _ceil_cents(dollars: float) -> float:
    # round to 1e-9 first so float noise (1.7500000001) doesn't add a cent
    return math.ceil(round(dollars * 100, 6)) * 1.0


@dataclass
class FeeModel:
    taker_rate: float = 0.07
    maker_rate: float = 0.0175
    default_multiplier: float = 1.0
    default_fee_type: str = "quadratic_with_maker_fees"
    flat_fee_cents: float = 1.0  # used only for fee_type == 'flat' when no better info

    @classmethod
    def from_config(cls, fees_cfg) -> "FeeModel":
        return cls(taker_rate=fees_cfg.taker_rate, maker_rate=fees_cfg.maker_rate,
                   default_multiplier=fees_cfg.default_fee_multiplier,
                   default_fee_type=fees_cfg.default_fee_type)

    def fee(self, price_cents: float, count: int, is_taker: bool, meta: MarketMeta | None = None,
            at_ts: float | None = None) -> float:
        """Total fee in cents for one fill of `count` contracts at `price_cents` (either side)."""
        if count <= 0:
            return 0.0
        fee_type = (meta.fee_type if meta and meta.fee_type else self.default_fee_type)
        mult = (meta.fee_multiplier if meta and meta.fee_multiplier is not None else self.default_multiplier)
        if meta and meta.fee_waiver_until and at_ts is not None and at_ts < meta.fee_waiver_until:
            return 0.0
        p = min(max(price_cents / 100.0, 0.0), 1.0)
        if fee_type == "flat":
            return self.flat_fee_cents * count * mult
        if is_taker:
            rate = self.taker_rate
        else:
            if fee_type != "quadratic_with_maker_fees":
                return 0.0
            rate = self.maker_rate
        return _ceil_cents(mult * rate * count * p * (1 - p))

    def per_contract(self, price_cents: float, count: int, is_taker: bool, meta: MarketMeta | None = None) -> float:
        return self.fee(price_cents, max(count, 1), is_taker, meta) / max(count, 1)

    def round_trip_cost(self, entry: float, exit_: float, count: int, entry_taker: bool, exit_taker: bool,
                        meta: MarketMeta | None = None) -> float:
        """Fees in cents per contract for entering at `entry` and exiting at `exit_`."""
        c = max(count, 1)
        return (self.fee(entry, c, entry_taker, meta) + self.fee(exit_, c, exit_taker, meta)) / c
