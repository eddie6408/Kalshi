"""Market-state classifier (section 18). Every label comes with the reason
it was applied."""
from __future__ import annotations

from dataclasses import dataclass, field

from lvt.engine.volatility import Metrics, regime

QUALITY_BLOCKERS = ("STALE_DATA", "WIDE_SPREAD", "LOW_LIQUIDITY")
PATTERNS = ("REVERSAL", "BREAKOUT", "EXHAUSTION", "MOMENTUM")


@dataclass
class MarketState:
    primary: str
    labels: list[str]
    explanation: dict[str, str] = field(default_factory=dict)
    regime: str = ""
    trend: str = ""

    @property
    def tradeable(self) -> bool:
        return "NO_TRADE" not in self.labels


def classify(m: Metrics, vcfg, ecfg) -> MarketState:
    labels: list[str] = []
    why: dict[str, str] = {}

    if m.data_age_s > ecfg.max_data_age_s:
        labels.append("STALE_DATA")
        why["STALE_DATA"] = f"last quote {m.data_age_s:.1f}s old > {ecfg.max_data_age_s}s"
    if m.spread is None:
        labels.append("WIDE_SPREAD")
        why["WIDE_SPREAD"] = "one side of the book is empty"
    elif m.spread > ecfg.max_spread_cents:
        labels.append("WIDE_SPREAD")
        why["WIDE_SPREAD"] = f"spread {m.spread:.1f}c > {ecfg.max_spread_cents}c"
    if m.liquidity_usd < ecfg.min_liquidity_usd:
        labels.append("LOW_LIQUIDITY")
        why["LOW_LIQUIDITY"] = f"${m.liquidity_usd:.0f} at the touch < ${ecfg.min_liquidity_usd:.0f}"

    reg = regime(m.realized_vol, vcfg)
    labels.append(reg)
    why[reg] = f"realised vol {m.realized_vol:.2f}c/min (low<{vcfg.vol_low}, high>={vcfg.vol_high}, extreme>={vcfg.vol_extreme})"

    v, r2 = m.velocity_medium, m.r2_medium
    if abs(v) >= vcfg.trend_strong_velocity and r2 >= vcfg.trend_min_r2:
        trend = "STRONG_UPTREND" if v > 0 else "STRONG_DOWNTREND"
    elif abs(v) >= vcfg.trend_weak_velocity:
        trend = "WEAK_UPTREND" if v > 0 else "WEAK_DOWNTREND"
    else:
        trend = "RANGE"
    labels.append(trend)
    why[trend] = f"medium slope {v:+.2f}c/min, r2 {r2:.2f}"

    big_move = abs(m.change_long) >= 2.0 * ecfg.min_movement_cents
    vs = m.velocity_short
    if big_move and vs * m.change_long < 0 and abs(vs) >= vcfg.trend_weak_velocity:
        labels.append("REVERSAL")
        why["REVERSAL"] = (f"long-window move {m.change_long:+.1f}c but short slope {vs:+.2f}c/min "
                           "points the other way")
    if (m.mid is not None and m.recent_high is not None and m.range_cents >= 2 * ecfg.min_movement_cents
            and abs(vs) >= vcfg.trend_strong_velocity and vs * m.acceleration > 0
            and ((vs > 0 and m.mid >= m.recent_high - 0.5) or (vs < 0 and m.mid <= m.recent_low + 0.5))):
        labels.append("BREAKOUT")
        why["BREAKOUT"] = (f"price at the {'high' if vs > 0 else 'low'} of a {m.range_cents:.1f}c range "
                           f"with accelerating slope {vs:+.2f}c/min")
    if (big_move and vs * m.change_long >= 0 and abs(m.velocity_medium) > 0
            and abs(vs) < 0.3 * abs(m.velocity_medium)):
        labels.append("EXHAUSTION")
        why["EXHAUSTION"] = (f"move of {m.change_long:+.1f}c is stalling: short slope {vs:+.2f} vs "
                             f"medium {m.velocity_medium:+.2f}c/min")
    if trend.startswith("STRONG") and m.acceleration * v >= 0 and m.volume_acceleration >= 1.0:
        labels.append("MOMENTUM")
        why["MOMENTUM"] = (f"{trend.lower()} with non-negative acceleration and volume rate x"
                           f"{m.volume_acceleration:.1f}")

    blockers = [l for l in labels if l in QUALITY_BLOCKERS]
    if blockers or reg == "LOW_VOLATILITY":
        labels.append("NO_TRADE")
        why["NO_TRADE"] = "blocked by " + ", ".join(blockers or [reg])

    if blockers:
        primary = blockers[0]
    else:
        pats = [l for l in labels if l in PATTERNS]
        primary = pats[0] if pats else ("NO_TRADE" if reg == "LOW_VOLATILITY" else trend)
    return MarketState(primary=primary, labels=labels, explanation=why, regime=reg, trend=trend)
