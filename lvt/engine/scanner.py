"""Market scanner (sections 15, 16).

Three tiers, so thousands of markets can be screened on a small request
budget:

  UNIVERSE  every open, non-combo market with 24h volume above a floor,
            from an hourly paced sweep of GET /markets (~120 pages).
  PROBE     the top `probe_size` universe markets by 24h volume x category
            weight, batch-quoted every `probe_interval_s` (50 per request).
            Measures what is moving NOW (recent volume, price change).
  HOT       the `watch_size` most active probe markets (plus any market
            with a position or working order), batch-quoted every
            `quote_interval_s`, with rotating order-book depth. Only hot
            markets are traded.

Eligibility is evaluated on hot markets from their live metrics.
"""
from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass

from lvt.engine.volatility import Metrics
from lvt.models import MarketInfo, Quote, classify_sport

log = logging.getLogger("lvt.scanner")


@dataclass
class Eligibility:
    ok: bool
    reasons: dict[str, str]           # check -> explanation (failures only when not ok)


def eligibility(m: Metrics, info: MarketInfo, now: float, ecfg, min_minutes_to_close: float) -> Eligibility:
    fails: dict[str, str] = {}
    passes: dict[str, str] = {}

    def chk(name, ok, detail):
        (passes if ok else fails)[name] = detail

    chk("volatility", m.realized_vol >= ecfg.min_volatility,
        f"realised vol {m.realized_vol:.2f} (min {ecfg.min_volatility})")
    chk("liquidity", m.liquidity_usd >= ecfg.min_liquidity_usd,
        f"${m.liquidity_usd:.0f} at touch (min ${ecfg.min_liquidity_usd})")
    chk("volume", m.volume_long >= ecfg.min_volume, f"{m.volume_long:.0f} contracts in window (min {ecfg.min_volume})")
    chk("spread", m.spread is not None and m.spread <= ecfg.max_spread_cents,
        f"spread {m.spread}c (max {ecfg.max_spread_cents}c)")
    chk("data_freshness", m.data_age_s <= ecfg.max_data_age_s, f"data {m.data_age_s:.1f}s old (max {ecfg.max_data_age_s}s)")
    chk("price_movement", m.range_cents >= ecfg.min_movement_cents,
        f"range {m.range_cents:.1f}c (min {ecfg.min_movement_cents}c)")
    chk("price_band", m.mid is not None and ecfg.min_price <= m.mid <= ecfg.max_price, f"mid {m.mid}")
    ttc = (info.close_ts - now) / 60.0 if info.close_ts else 1e9
    chk("time_remaining", ttc >= min_minutes_to_close, f"{ttc:.0f} min to close")
    chk("status", (info.status or "active") in ("active", "open", "initialized", ""), f"status {info.status}")
    return Eligibility(not fails, fails if fails else passes)


class Scanner:
    def __init__(self, cfg):
        self.cfg = cfg
        self.d = cfg.discovery
        self.markets: dict[str, MarketInfo] = {}
        self.catalog: dict[str, dict] = {}
        self.probe: list[str] = []
        self.probe_hist: dict[str, deque] = {}        # ticker -> (ts, mid, volume)
        self.hot: dict[str, float] = {}               # ticker -> entered ts
        self.activity: dict[str, float] = {}
        self.last_sweep = 0.0
        self.last_catalog = 0.0
        self.last_probe = 0.0

    # -- universe --------------------------------------------------------------
    def weight(self, category: str) -> float:
        w = self.d.category_weight
        return float(w.get(category, w.get("default", 0.5)))

    def enrich(self, mi: MarketInfo) -> MarketInfo:
        s = self.catalog.get(mi.series_ticker) or {}
        mi.category = s.get("category", mi.category) or mi.category
        mi.fee_type = s.get("fee_type", mi.fee_type)
        mi.fee_multiplier = s.get("fee_multiplier", mi.fee_multiplier)
        mi.sport = classify_sport(mi.series_ticker, mi.category)
        return mi

    def accept(self, mi: MarketInfo, now: float) -> bool:
        if mi.volume_24h < self.d.universe_min_volume_24h:
            return False
        if self.d.include_categories and mi.category not in self.d.include_categories:
            return False
        if any(mi.series_ticker.startswith(p) for p in self.d.exclude_series_prefixes):
            return False
        if mi.close_ts and mi.close_ts - now < self.d.min_minutes_to_close * 60:
            return False
        return True

    def load_universe(self, found: list[MarketInfo], now: float, keep: set[str]) -> list[MarketInfo]:
        fresh: dict[str, MarketInfo] = {}
        for mi in found:
            self.enrich(mi)
            if self.accept(mi, now):
                fresh[mi.ticker] = mi
        # Update IN PLACE: the engine and the venue hold this dict.
        for t in list(self.markets):
            if t not in fresh and t not in keep:        # never drop what we hold
                del self.markets[t]
        self.markets.update(fresh)
        fresh = dict(self.markets)
        ranked = sorted(fresh.values(), key=lambda m: -(math.log1p(m.volume_24h) * self.weight(m.category)))
        self.probe = [m.ticker for m in ranked[: self.d.probe_size]]
        self.last_sweep = now
        log.info("universe %d markets, probing %d", len(fresh), len(self.probe))
        return list(fresh.values())

    # -- probe tier -------------------------------------------------------------
    def probe_due(self, now: float) -> list[str]:
        if now - self.last_probe < self.d.probe_interval_s:
            return []
        self.last_probe = now
        return [t for t in self.probe if t not in self.hot]

    def note_probe(self, quotes: dict[str, Quote]) -> None:
        for t, q in quotes.items():
            if q.yes_bid is None or q.yes_ask is None:
                mid = None
            else:
                mid = (q.yes_bid + q.yes_ask) / 2.0
            h = self.probe_hist.setdefault(t, deque(maxlen=40))
            h.append((q.ts, mid, q.volume, q.yes_ask - q.yes_bid if mid is not None else None))
            mi = self.markets.get(t)
            if mi is not None and q.status:
                mi.status = q.status

    def score_activity(self, t: str, now: float) -> float:
        h = self.probe_hist.get(t)
        if not h or len(h) < 2:
            return 0.0
        recent = [x for x in h if now - x[0] <= 600]
        if len(recent) < 2:
            return 0.0
        dv = max(recent[-1][2] - recent[0][2], 0.0)
        mids = [x[1] for x in recent if x[1] is not None]
        move = (max(mids) - min(mids)) if mids else 0.0
        spread = recent[-1][3]
        if spread is None or spread > 3 * self.cfg.eligibility.max_spread_cents:
            return 0.0
        mi = self.markets.get(t)
        w = self.weight(mi.category) if mi else 0.5
        return w * (math.log1p(dv) + 2.0 * move)

    # -- hot tier -----------------------------------------------------------------
    def select_hot(self, now: float, pinned: set[str]) -> list[str]:
        acts = {t: self.score_activity(t, now) for t in self.probe_hist}
        self.activity = acts
        keep = {t for t, since in self.hot.items() if now - since < self.d.watch_min_dwell_s}
        ranked = [t for t, a in sorted(acts.items(), key=lambda x: -x[1]) if a > 0]
        new: dict[str, float] = {}
        for t in list(pinned) + sorted(keep, key=lambda x: self.hot[x]):
            new[t] = self.hot.get(t, now)
        for t in ranked:
            if len(new) >= self.d.watch_size:
                break
            if t not in new:
                new[t] = self.hot.get(t, now)
        self.hot = new
        return list(new)
