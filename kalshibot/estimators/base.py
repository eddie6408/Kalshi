"""The estimator interface.

An estimator's only job is to answer one question for a given market:

    "What is the true probability that this contract resolves YES,
     and how much do I trust that number?"

It must NOT know about money, sizing, fees, or order placement. That
separation is what lets you swap a weather model for a Claude-powered
estimator in rendition 2 without touching the risk engine.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..models import Market


@dataclass
class Estimate:
    probability_yes: float          # 0..1
    confidence: float               # 0..1 - how much we trust the number
    source: str                     # human-readable name of the method
    evidence: dict[str, Any] = field(default_factory=dict)
    skip_reason: str | None = None  # set when we decline to price the market

    @classmethod
    def skip(cls, reason: str, source: str = "") -> "Estimate":
        return cls(0.5, 0.0, source, {}, skip_reason=reason)

    @property
    def usable(self) -> bool:
        return self.skip_reason is None and 0.0 <= self.probability_yes <= 1.0


class Estimator(Protocol):
    name: str

    def candidate_series(self) -> list[str]:
        """Kalshi series tickers this estimator knows how to price."""
        ...

    def estimate(self, market: Market) -> Estimate:
        ...
