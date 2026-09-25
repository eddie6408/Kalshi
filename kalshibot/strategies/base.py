"""Strategy interface.

A strategy looks at normalized markets and returns ArbitrageOpportunity
objects. It does not know the account balance, does not size against risk
limits, and never places an order. The gate and the execution manager own
those decisions.

Future forecast strategies (Fed, crypto, econ, weather) implement the same
shape and emit the same object, with guaranteed=False.
"""
from __future__ import annotations

from typing import Protocol

from ..depth import DepthBook
from ..models import Market
from ..opportunity import ArbitrageOpportunity


class Strategy(Protocol):
    name: str
    #: True only when the payoff is locked by contract structure.
    model_free: bool

    def scan_event(self, event_id: str, markets: list[Market],
                   books: dict[str, DepthBook],
                   is_exclusive: bool = False) -> list[ArbitrageOpportunity]:
        ...
