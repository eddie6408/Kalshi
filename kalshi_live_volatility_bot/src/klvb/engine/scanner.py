"""Market scanner: discovery pre-filter, candidate ranking and trade eligibility.

Two stages:
  1. Discovery (every ~90s, cheap): all open markets -> pre-filter on 24h volume,
     spread, price band, time to close, category -> ranked candidate list; the
     top N become the watchlist that is polled every ~2s.
  2. Eligibility (every loop, per watched market): the spec-16 rule
       VOLATILITY >= MIN_VOLATILITY and LIQUIDITY >= MIN_LIQUIDITY and
       VOLUME >= MIN_VOLUME and SPREAD <= MAX_SPREAD and
       DATA_FRESHNESS <= MAX_DATA_AGE and PRICE_MOVEMENT >= MIN_MOVEMENT
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..models import MarketMeta, MarketSnapshot
from .volatility import Features


@dataclass
class Candidate:
    meta: MarketMeta
    snap: MarketSnapshot
    rank_score: float
    live_context: bool = False
    reasons: list[str] = field(default_factory=list)


def prefilter(meta: MarketMeta, snap: MarketSnapshot, now: float, scfg, ecfg) -> list[str]:
    """Returns the list of reasons the market is excluded (empty list = passes)."""
    why: list[str] = []
    if scfg.exclude_multivariate and meta.is_multivariate:
        why.append("multivariate/combo market")
    if meta.category and meta.category not in scfg.categories:
        why.append(f"category {meta.category} not scanned")
    if snap.status and snap.status not in ("active", "open"):
        why.append(f"status {snap.status}")
    end = meta.effective_end_ts()
    if end is not None:
        left = end - now
        if left < scfg.min_time_to_close_seconds:
            why.append(f"only {left:.0f}s until close/expiry")
        if left > scfg.max_time_to_close_hours * 3600:
            why.append(f"{left / 3600:.0f}h until close/expiry (> {scfg.max_time_to_close_hours}h)")
    mid = snap.mid
    if mid is None:
        why.append("no quotes")
    elif not (scfg.min_price_cents <= mid <= scfg.max_price_cents):
        why.append(f"price {mid:.0f}c outside {scfg.min_price_cents}-{scfg.max_price_cents}c band")
    if (snap.volume_24h or 0) < scfg.min_volume_24h:
        why.append(f"24h volume {snap.volume_24h or 0:.0f} < {scfg.min_volume_24h}")
    if (snap.open_interest or 0) < scfg.min_open_interest:
        why.append(f"open interest {snap.open_interest or 0:.0f} < {scfg.min_open_interest}")
    sp = snap.spread
    if sp is None or sp > 2 * ecfg.max_spread_cents:
        why.append(f"spread {sp if sp is not None else 'n/a'} too wide at discovery")
    return why


def rank(meta: MarketMeta, snap: MarketSnapshot, scfg, live_event: bool) -> float:
    """Heuristic ranking of discovery candidates: activity, tightness, headroom, preference."""
    vol = math.log10(1 + (snap.volume_24h or 0))
    tight = 1.0 / max(snap.spread or 1.0, 1.0)
    mid = snap.mid or 50.0
    centrality = 1.0 - abs(mid - 50.0) / 50.0     # more room to move both ways, highest fee though
    pref = 1.0 if meta.category in scfg.prefer_categories else 0.0
    return round(vol * 2.0 + tight * 2.0 + centrality + pref * 1.5 + (3.0 if live_event else 0.0), 4)


def build_candidates(markets: list[tuple[MarketMeta, MarketSnapshot]], now: float, scfg, ecfg,
                     live_events: set[str]) -> tuple[list[Candidate], dict[str, int]]:
    out: list[Candidate] = []
    rejected: dict[str, int] = {}
    for meta, snap in markets:
        why = prefilter(meta, snap, now, scfg, ecfg)
        if why:
            key = why[0].split(" ")[0]
            rejected[key] = rejected.get(key, 0) + 1
            continue
        live = meta.event_ticker in live_events
        out.append(Candidate(meta, snap, rank(meta, snap, scfg, live), live))
    out.sort(key=lambda c: c.rank_score, reverse=True)
    return out, rejected


def select_watchlist(candidates: list[Candidate], pinned: set[str], max_n: int) -> list[str]:
    """Pinned tickers (open positions/orders) always stay watched."""
    chosen = list(dict.fromkeys(sorted(pinned)))
    for c in candidates:
        if len(chosen) >= max(max_n, len(pinned)):
            break
        if c.meta.ticker not in chosen:
            chosen.append(c.meta.ticker)
    return chosen


def eligibility(f: Features | None, ecfg, vol_cfg) -> tuple[bool, list[str]]:
    if f is None:
        return False, ["no data"]
    why: list[str] = []
    if f.realized_vol < ecfg.min_volatility:
        why.append(f"volatility {f.realized_vol:.2f} < {ecfg.min_volatility}")
    liq = min(x if x is not None else 0 for x in (f.bid_depth, f.ask_depth))
    if liq < ecfg.min_liquidity_contracts:
        why.append(f"liquidity {liq:.0f} < {ecfg.min_liquidity_contracts}")
    if f.volume_window < ecfg.min_volume_window:
        why.append(f"window volume {f.volume_window:.0f} < {ecfg.min_volume_window}")
    if f.spread is None or f.spread > ecfg.max_spread_cents:
        why.append(f"spread {f.spread if f.spread is not None else 'n/a'} > {ecfg.max_spread_cents}")
    if f.data_age is None or f.data_age > ecfg.max_data_age_seconds:
        why.append(f"data age {f.data_age if f.data_age is not None else 'n/a'} > {ecfg.max_data_age_seconds}s")
    if f.range_long < ecfg.min_movement_cents:
        why.append(f"movement {f.range_long:.1f}c < {ecfg.min_movement_cents}c")
    return (not why), why
