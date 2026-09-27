"""Movement quality score, 0-100 (section 32).

This is NOT a probability of YES or NO. It grades how good the current
trading opportunity looks on the evidence the bot can measure. Components
are each 0..1 and are stored with the signal so the score can be audited
and, later, re-weighted against realised outcomes.
"""
from __future__ import annotations

WEIGHTS = {
    "magnitude": 0.14,
    "velocity": 0.10,
    "confirmation": 0.14,
    "persistence": 0.08,
    "volatility": 0.08,
    "volume": 0.10,
    "liquidity": 0.08,
    "spread": 0.08,
    "edge": 0.12,
    "freshness": 0.04,
    "imbalance": 0.04,
}

REGIME_FIT = {"LOW_VOLATILITY": 0.2, "NORMAL_VOLATILITY": 0.9, "HIGH_VOLATILITY": 1.0,
              "EXTREME_VOLATILITY": 0.5}


def clamp01(x: float) -> float:
    return 0.0 if x != x else max(0.0, min(1.0, x))


def score(components: dict[str, float]) -> float:
    total = sum(WEIGHTS.values())
    s = sum(WEIGHTS[k] * clamp01(components.get(k, 0.0)) for k in WEIGHTS)
    return round(100.0 * s / total, 1)


def common_components(m, regime: str, ecfg, rcfg, side_imbalance: float,
                      expected_net: float | None, min_edge: float) -> dict[str, float]:
    return {
        "volatility": REGIME_FIT.get(regime, 0.5),
        "volume": clamp01(m.volume_long / max(5.0 * ecfg.min_volume, 1.0)),
        "liquidity": clamp01(m.liquidity_usd / max(5.0 * ecfg.min_liquidity_usd, 1.0)),
        "spread": clamp01(1.0 - (m.spread or 99.0) / max(rcfg.max_spread_cents + 1.0, 1.0)),
        "edge": clamp01((expected_net or 0.0) / max(3.0 * min_edge, 0.5)),
        "freshness": clamp01(1.0 - m.data_age_s / max(ecfg.max_data_age_s, 0.1)),
        "imbalance": clamp01((1.0 + side_imbalance) / 2.0),
    }
