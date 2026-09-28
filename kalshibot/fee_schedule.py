"""Package 8: the fee schedule Kalshi publishes, per series, instead of assumed.

Kalshi publishes each series' `fee_type` and `fee_multiplier`, and scheduled
changes at /series/fee_changes. Until now fees were two numbers in
config.yaml. Checked 2026-09-28: every series the bot has traded is
`quadratic` x1 on demo - exactly the configured taker formula - and 120 of 122
demo fills were charged exactly that (the other two were resting orders on
weather, charged $0: `quadratic` has no maker fee).

So this changes no decision today. What it adds:

  * every decision records WHICH schedule priced it (fee_schedule_version);
  * an entry is refused - never loosened - on a series whose schedule is
    unknown, unreadable, or HIGHER than the model assumes (a multiplier
    above the configured one, or a fee type the model does not know).
    Research brief section 13: fee uncertainty is a rejection reason. Exits
    are never gated;
  * a scheduled fee change is alerted once, before it takes effect;
  * the correct MAKER fee per series for the maker shadow (C2 M0).

Formulas (Kalshi fee schedule, per fill, rounded up to the cent):
    taker  0.07   x mult x C x P x (1 - P)
    maker  0.0175 x mult x C x P x (1 - P)   only for quadratic_with_maker_fees
Read from the DEMO exchange (it is where the bot's fees are charged); cached
for `refresh_hours`. A failed read keeps the last good schedule; with none,
the series counts as uncertain.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

TAKER_RATE = 0.07
MAKER_RATE = 0.0175
#: fee types this model can price, and whether they charge resting orders
KNOWN_TYPES = {"quadratic": False, "quadratic_with_maker_fees": True}


def _ceil_cents(dollars: float) -> float:
    return math.ceil(round(dollars * 100, 9)) / 100.0


def series_of(ticker: str) -> str:
    return (ticker or "").split("-")[0].upper()


@dataclass
class SeriesFees:
    series: str
    fee_type: str | None
    multiplier: float | None
    fetched_at: float
    pending: list = field(default_factory=list)
    error: str = ""

    @property
    def known(self) -> bool:
        return self.fee_type in KNOWN_TYPES and self.multiplier is not None

    @property
    def version(self) -> str:
        if not self.known:
            return f"kalshi:{self.series}:unknown"
        return f"kalshi:{self.series}:{self.fee_type}:x{self.multiplier:g}"

    def taker_fee_usd(self, price_cents: float, contracts: int) -> float:
        p = max(0.0, min(1.0, price_cents / 100.0))
        return _ceil_cents(TAKER_RATE * (self.multiplier or 1.0) * contracts * p * (1 - p))

    def maker_fee_usd(self, price_cents: float, contracts: int) -> float:
        if not KNOWN_TYPES.get(self.fee_type or "", True):
            return 0.0
        p = max(0.0, min(1.0, price_cents / 100.0))
        return _ceil_cents(MAKER_RATE * (self.multiplier or 1.0) * contracts * p * (1 - p))


class FeeSchedule:
    def __init__(self, client, *, assumed_multiplier: float = 1.0,
                 refresh_hours: float = 6.0, enforce: bool = True,
                 clock=time.time, alert=None):
        """`client` has get_series(ticker) -> dict and get_fee_changes(ticker)
        -> list. `assumed_multiplier` is what config.yaml's taker_rate
        implies (taker_rate / 0.07)."""
        self.client = client
        self.assumed = assumed_multiplier
        self.refresh = refresh_hours * 3600
        self.enforce = enforce
        self.clock = clock
        self.alert = alert
        self._lock = threading.Lock()
        self._cache: dict[str, SeriesFees] = {}
        self._alerted: set[str] = set()

    # ------------------------------------------------------------------
    def get(self, series: str) -> SeriesFees:
        series = series.upper()
        now = self.clock()
        with self._lock:
            cur = self._cache.get(series)
        if cur is not None and now - cur.fetched_at < self.refresh:
            return cur
        fresh = self._fetch(series, now)
        with self._lock:
            if fresh.known or cur is None:
                self._cache[series] = fresh
            else:
                # A failed read keeps the last good schedule, and tries again
                # next time rather than hammering the endpoint.
                cur.fetched_at = now - self.refresh + 600
                cur.error = fresh.error
                fresh = cur
        return fresh

    def _fetch(self, series: str, now: float) -> SeriesFees:
        try:
            raw = self.client.get_series(series) or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("FEES  could not read the fee schedule for %s: %s", series, exc)
            return SeriesFees(series, None, None, now, error=str(exc)[:200])
        try:
            mult = float(raw.get("fee_multiplier")) if raw.get("fee_multiplier") is not None else None
        except (TypeError, ValueError):
            mult = None
        sf = SeriesFees(series, (raw.get("fee_type") or None), mult, now)
        try:
            changes = self.client.get_fee_changes(series) or []
        except Exception as exc:  # noqa: BLE001
            log.debug("FEES  no fee_changes for %s: %s", series, exc)
            changes = []
        sf.pending = [c for c in changes
                      if str(c.get("series_ticker") or series).upper() == series]
        if not sf.known:
            log.warning("FEES  %s: fee type %r x%r is not one this model prices",
                        series, sf.fee_type, sf.multiplier)
        for c in sf.pending:
            key = f"{series}|{c.get('scheduled_ts') or c.get('effective_ts') or c}"
            if key not in self._alerted:
                self._alerted.add(key)
                msg = f"{series}: scheduled fee change {c}"
                log.warning("FEES  %s", msg)
                if self.alert is not None:
                    try:
                        self.alert(series, msg)
                    except Exception:  # noqa: BLE001
                        pass
        return sf

    # ------------------------------------------------------------------
    def uncertain(self, series: str) -> str | None:
        """Why this series' fees cannot be trusted for an entry, or None."""
        sf = self.get(series)
        if not sf.known:
            return (f"{series} fee schedule unknown ({sf.fee_type or 'unreadable'}"
                    f"{': ' + sf.error if sf.error else ''})")
        if sf.multiplier > self.assumed + 1e-9:
            return (f"{series} fee multiplier x{sf.multiplier:g} is above the "
                    f"x{self.assumed:g} the model prices with")
        return None

    def check_tickers(self, tickers) -> str | None:
        """The first reason any of these markets' fees are uncertain, if
        enforcing; None otherwise. Every series is still read and cached."""
        for s in sorted({series_of(t) for t in tickers if t}):
            why = self.uncertain(s)
            if why and self.enforce:
                return why
            if why:
                log.info("FEES  (not enforced) %s", why)
        return None

    def version_for(self, tickers) -> str:
        return ";".join(self.get(s).version for s in sorted({series_of(t) for t in tickers if t}))

    def maker_fee_usd(self, ticker: str, price_cents: float, contracts: int) -> float:
        return self.get(series_of(ticker)).maker_fee_usd(price_cents, contracts)

    def summary(self) -> str:
        with self._lock:
            rows = sorted(self._cache.values(), key=lambda s: s.series)
        return ", ".join(f"{s.series} {s.fee_type or '?'} x{s.multiplier if s.multiplier is not None else '?'}"
                         for s in rows) or "nothing read yet"
