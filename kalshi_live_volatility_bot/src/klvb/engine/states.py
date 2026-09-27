"""Market-state classifier. Every state carries a human-readable explanation."""
from __future__ import annotations

from dataclasses import dataclass, field

from .volatility import Features

BLOCKING = {"STALE_DATA", "WIDE_SPREAD", "LOW_LIQUIDITY", "WARMUP", "CLOSING_SOON", "NOT_ACTIVE"}


@dataclass
class MarketState:
    ticker: str
    ts: float
    states: list[str] = field(default_factory=list)
    explanations: dict[str, str] = field(default_factory=dict)
    regime: str = "UNKNOWN"
    trend: str = "RANGE"

    def add(self, state: str, why: str) -> None:
        if state not in self.states:
            self.states.append(state)
        self.explanations[state] = why

    @property
    def tradable(self) -> bool:
        return not (BLOCKING & set(self.states))

    def has(self, s: str) -> bool:
        return s in self.states


def classify(f: Features, vol_cfg, elig_cfg, min_samples: int, seconds_to_end: float | None = None,
             min_seconds_to_end: float = 0.0, status: str = "active") -> MarketState:
    st = MarketState(ticker=f.ticker, ts=f.ts)
    if status and status not in ("active", "open", ""):
        st.add("NOT_ACTIVE", f"market status is {status!r}")
    if f.data_age is None or f.data_age > elig_cfg.max_data_age_seconds:
        st.add("STALE_DATA", f"last update {f.data_age if f.data_age is not None else 'never'}s ago "
                             f"> {elig_cfg.max_data_age_seconds}s")
    if f.samples < min_samples:
        st.add("WARMUP", f"only {f.samples} observations (< {min_samples}); metrics not yet reliable")
    if seconds_to_end is not None and seconds_to_end < min_seconds_to_end:
        st.add("CLOSING_SOON", f"{seconds_to_end:.0f}s to close/expected expiry (< {min_seconds_to_end:.0f}s)")
    if f.spread is None:
        st.add("WIDE_SPREAD", "one side of the book is empty")
    elif f.spread > elig_cfg.max_spread_cents:
        st.add("WIDE_SPREAD", f"spread {f.spread:.1f}c > {elig_cfg.max_spread_cents}c")
    depth = min(x for x in (f.bid_depth, f.ask_depth) if x is not None) if (
        f.bid_depth is not None or f.ask_depth is not None) else None
    if depth is None or depth < elig_cfg.min_liquidity_contracts:
        st.add("LOW_LIQUIDITY", f"thinnest side depth {depth if depth is not None else 'unknown'} "
                                f"< {elig_cfg.min_liquidity_contracts} contracts")

    rv = f.realized_vol
    if rv >= vol_cfg.regime_extreme:
        st.regime = "EXTREME_VOLATILITY"
    elif rv >= vol_cfg.regime_high:
        st.regime = "HIGH_VOLATILITY"
    elif rv >= vol_cfg.regime_normal:
        st.regime = "NORMAL_VOLATILITY"
    else:
        st.regime = "LOW_VOLATILITY"
    st.add(st.regime, f"realized vol {rv:.2f}c/sqrt(min) (normal>={vol_cfg.regime_normal}, "
                      f"high>={vol_cfg.regime_high}, extreme>={vol_cfg.regime_extreme})")

    v = f.velocity_medium
    if v >= vol_cfg.trend_strong:
        st.trend = "STRONG_UPTREND"
    elif v >= vol_cfg.trend_weak:
        st.trend = "WEAK_UPTREND"
    elif v <= -vol_cfg.trend_strong:
        st.trend = "STRONG_DOWNTREND"
    elif v <= -vol_cfg.trend_weak:
        st.trend = "WEAK_DOWNTREND"
    else:
        st.trend = "RANGE"
    st.add(st.trend, f"medium-window velocity {v:+.2f}c/min (weak {vol_cfg.trend_weak}, strong {vol_cfg.trend_strong})")

    vs, vw = f.velocity_short, vol_cfg.trend_weak
    if abs(vs) >= vw and vs * v > 0 and f.acceleration * vs >= 0:
        st.add("MOMENTUM", f"short velocity {vs:+.2f}c/min agrees with medium trend and is not decelerating "
                           f"(accel {f.acceleration:+.2f})")
    if abs(v) >= vw and (vs * v < 0 or abs(vs) < 0.25 * abs(v)) and f.acceleration * v < 0:
        st.add("EXHAUSTION", f"medium trend {v:+.2f}c/min but short velocity {vs:+.2f}c/min is fading/opposing")
    if f.price is not None and f.recent_high is not None and f.recent_low is not None and f.range_long >= elig_cfg.min_movement_cents:
        if f.price >= f.recent_high - 0.5 and vs >= vw:
            st.add("BREAKOUT", f"price {f.price:.1f}c at the {f.recent_high:.1f}c window high with "
                               f"{vs:+.2f}c/min velocity")
        elif f.price <= f.recent_low + 0.5 and vs <= -vw:
            st.add("BREAKOUT", f"price {f.price:.1f}c at the {f.recent_low:.1f}c window low with "
                               f"{vs:+.2f}c/min velocity (downside)")
        rebound = f.price - f.recent_low
        pullback = f.recent_high - f.price
        if rebound >= 2 and vs > 0 and v < 0:
            st.add("REVERSAL", f"rebounded {rebound:.1f}c off the {f.recent_low:.1f}c low while the "
                               f"medium trend is still down (turning up)")
        elif pullback >= 2 and vs < 0 and v > 0:
            st.add("REVERSAL", f"pulled back {pullback:.1f}c from the {f.recent_high:.1f}c high while the "
                               f"medium trend is still up (turning down)")
    if not st.tradable:
        blockers = [s for s in st.states if s in BLOCKING]
        st.add("NO_TRADE", "blocked by " + ", ".join(blockers))
    return st
