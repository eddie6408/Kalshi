"""Movement Quality Score (0-100).

This is NOT a probability of YES or NO. It rates the quality of the *current
trading opportunity*: is there enough clean, liquid, fresh movement to capture
after costs? Each component is normalized to 0..1 and weighted.
"""
from __future__ import annotations

DEFAULT_WEIGHTS = {
    "magnitude": 1.0,
    "velocity": 0.8,
    "confirmation": 1.2,
    "volatility": 0.6,
    "volume": 0.7,
    "liquidity": 0.8,
    "spread": 0.8,
    "edge": 1.5,
    "freshness": 0.6,
}


def clamp01(x: float) -> float:
    return 0.0 if x != x else max(0.0, min(1.0, x))  # NaN -> 0


def volatility_component(realized_vol: float, normal: float, high: float, extreme: float) -> float:
    """Rises through the normal/high regimes, falls again when volatility becomes extreme (risk)."""
    if realized_vol <= 0:
        return 0.0
    if realized_vol < high:
        return clamp01(realized_vol / high)
    if realized_vol < extreme:
        return 1.0
    return clamp01(1.0 - (realized_vol - extreme) / extreme)


def quality_score(components: dict[str, float], weights: dict[str, float] | None = None) -> float:
    w = weights or DEFAULT_WEIGHTS
    num = den = 0.0
    for k, wt in w.items():
        if k in components:
            num += wt * clamp01(components[k])
            den += wt
    return round(100.0 * num / den, 1) if den else 0.0


def generic_movement_score(f, vol_cfg, elig_cfg) -> float:
    """Strategy-agnostic score for the scanner/dashboard."""
    if f is None or f.price is None:
        return 0.0
    comps = {
        "magnitude": f.range_long / (3 * elig_cfg.min_movement_cents),
        "velocity": abs(f.velocity_short) / (2 * vol_cfg.trend_strong),
        "volatility": volatility_component(f.realized_vol, vol_cfg.regime_normal, vol_cfg.regime_high,
                                           vol_cfg.regime_extreme),
        "volume": f.volume_window / (5 * elig_cfg.min_volume_window) if elig_cfg.min_volume_window else 0,
        "liquidity": (min(f.bid_depth or 0, f.ask_depth or 0)) / (3 * elig_cfg.min_liquidity_contracts),
        "spread": 1 - ((f.spread if f.spread is not None else 99) - 1) / max(elig_cfg.max_spread_cents, 1),
        "freshness": 1 - (f.data_age or 999) / elig_cfg.max_data_age_seconds,
    }
    return quality_score(comps)
