"""Remember which series actually have order books.

A live scan of the demo exchange found ~7% of sampled markets quoted. Spending
the order book budget uniformly across 4,000 events therefore wastes almost
all of it. This keeps a running per-series hit rate on disk so each scan
spends its budget where books were actually found last time.

It is a cache, not a filter: a series with no history is still tried, and a
series that goes quiet decays out rather than being banned.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class SeriesStat:
    checked: int = 0
    with_depth: int = 0
    last_seen: float = field(default_factory=time.time)

    @property
    def hit_rate(self) -> float:
        return (self.with_depth / self.checked) if self.checked else 0.0

    def to_dict(self) -> dict:
        return {"checked": self.checked, "with_depth": self.with_depth,
                "last_seen": self.last_seen}


class LiquidityProfile:
    #: how much history to keep before halving it, so old data fades
    DECAY_AFTER = 400
    #: forget a series entirely if it has not been seen for this long
    STALE_AFTER_SECONDS = 7 * 24 * 3600

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.stats: dict[str, SeriesStat] = {}
        self._load()

    # ---------------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
            now = time.time()
            for series, d in (raw.get("series") or {}).items():
                last = float(d.get("last_seen", now))
                if now - last > self.STALE_AFTER_SECONDS:
                    continue           # too old to trust; let it be re-learned
                self.stats[series] = SeriesStat(
                    int(d.get("checked", 0)), int(d.get("with_depth", 0)), last)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read liquidity profile: %s", exc)

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(
                {"updated": time.time(),
                 "series": {k: v.to_dict() for k, v in self.stats.items()}},
                indent=2))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not write liquidity profile: %s", exc)

    # ---------------------------------------------------------------
    def reset(self) -> None:
        self.stats.clear()
        self.save()

    def prune_to(self, live_series: set[str]) -> int:
        """Drop anything the exchange is not currently running.

        Keeps test fixtures and delisted series from sitting at the top of
        the priority list forever.
        """
        gone = [s for s in self.stats if s not in live_series]
        for s in gone:
            del self.stats[s]
        return len(gone)

    def record(self, series: str, checked: int, with_depth: int) -> None:
        if not series or checked <= 0:
            return
        st = self.stats.setdefault(series, SeriesStat())
        st.checked += checked
        st.with_depth += with_depth
        st.last_seen = time.time()
        if st.checked > self.DECAY_AFTER:
            st.checked //= 2
            st.with_depth //= 2

    def score(self, series: str) -> float:
        """Higher is a better place to spend an order book fetch.

        An unseen series scores 0.5 - optimistic enough to get tried, but
        behind anything with a proven book.
        """
        st = self.stats.get(series)
        if st is None or st.checked == 0:
            return 0.5
        return st.hit_rate

    def known_liquid(self, min_rate: float = 0.2) -> list[str]:
        """Any series where we have actually seen an order book.

        This used to require two samples. With a budget of a few hundred books
        spread over hundreds of series, almost everything gets sampled exactly
        once - so that rule silently discarded 47 of 55 confirmed-liquid
        series. One observed book is real evidence; two is a luxury.
        """
        return sorted((s for s, st in self.stats.items()
                       if st.with_depth >= 1 and st.hit_rate >= min_rate),
                      key=lambda s: (-self.stats[s].hit_rate,
                                     -self.stats[s].with_depth))

    def known_dead(self, min_checked: int = 3) -> list[str]:
        """Series we have sampled several times and never once found a book."""
        return [s for s, st in self.stats.items()
                if st.checked >= min_checked and st.with_depth == 0]

    def summary(self, top: int = 8) -> str:
        liquid = self.known_liquid()
        if not liquid:
            return "no liquidity history yet"
        bits = [f"{s} {self.stats[s].hit_rate:.0%}" for s in liquid[:top]]
        return f"{len(liquid)} series with books: " + ", ".join(bits)
