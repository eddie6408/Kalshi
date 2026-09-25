"""Calibration as permission, not as a report.

    "A strategy should have to earn the right to trade."  - Eddie, 2026-09-17

Tracking a Brier score and never acting on it is a dashboard. This is the
part that acts on it.

    UNPROVEN    not enough settled forecasts to judge. Trades.
    CALIBRATED  beating climatology over a real sample. Trades.
    DEGRADED    measurably worse than climatology. New entries suspended.

The chosen posture is lenient: a strategy trades at full size from the start
and is suspended only once the data says it is bad. That is defensible here
because the gate is a BACKSTOP over guards that already work - the coherence
check, the 5-95c price band, the relative-disagreement cap, the per-trade
dollar cap, and a position worker that exits a dead thesis. It would not have
been defensible before those existed.

Two consequences of being reactive rather than preventive:

  * Suspension must be SLOW to fire. With no reduced-size phase in front of
    it, a suspension triggered by noise looks like a broken bot. Nothing can
    suspend before `min_sample` settled forecasts.

  * The benchmark is climatology, not an absolute number. A Brier score of
    0.18 is poor for a coin flip and good for a six-way temperature bucket.
    What matters is whether we beat the naive forecast, not whether we hit a
    round number.

One rule is absolute:

    A DEGRADED STRATEGY MAY NOT ENTER. IT MAY ALWAYS EXIT.

A strategy that has lost the right to open new positions has not lost the
right to get out of the ones it holds - it needs that more, not less.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

log = logging.getLogger(__name__)


class CalibrationState(str, Enum):
    UNPROVEN = "UNPROVEN"
    CALIBRATED = "CALIBRATED"
    DEGRADED = "DEGRADED"


@dataclass
class CalibrationConfig:
    enabled: bool = True
    #: nothing may be suspended on fewer settled forecasts than this
    min_sample: int = 30
    #: ...and nothing may be CALIBRATED on fewer distinct contracts than this.
    #:
    #: Rows are not evidence. The same contract is re-priced about six times a
    #: day, so 452 settled rows were 74 independent things, and a bootstrap
    #: over contracts put the aggregate skill interval at [-0.018, +0.198] -
    #: across zero. Judging on rows had this gate reporting CALIBRATED over
    #: 1,605 forecasts while the dashboard, counting contracts, said there was
    #: not yet enough to judge. Both were reading the same database.
    min_contracts: int = 100
    #: Ignore forecasts made before this timestamp (ISO, or "" for all).
    #:
    #: A configuration change makes a different forecaster. On 21 September
    #: the lead gate cut trading from fourteen days out to six hours, and
    #: pooling both sides of that line describes neither bot. Permission to
    #: trade should rest on the bot that is running now.
    since: str = ""
    #: a strategy is CALIBRATED once it beats climatology by this margin
    #: over at least `min_sample` forecasts
    calibrated_margin: float = 0.01
    #: and DEGRADED once it is worse than climatology by this much
    degraded_margin: float = 0.03
    #: how often to re-read the journal (seconds); scores move slowly
    refresh_seconds: float = 300.0
    #: when there is no settled data at all, what to assume
    unproven_may_trade: bool = True

    @classmethod
    def from_dict(cls, d: dict | None) -> "CalibrationConfig":
        d = d or {}
        return cls(
            enabled=bool(d.get("enabled", True)),
            min_sample=int(d.get("min_sample", 30)),
            min_contracts=int(d.get("min_contracts", 100)),
            since=str(d.get("since", "") or ""),
            calibrated_margin=float(d.get("calibrated_margin", 0.01)),
            degraded_margin=float(d.get("degraded_margin", 0.03)),
            refresh_seconds=float(d.get("refresh_seconds", 300.0)),
            unproven_may_trade=bool(d.get("unproven_may_trade", True)),
        )


def climatology_brier(base_rate: float | None) -> float:
    """The score a forecaster gets for always predicting the base rate.

    If an outcome happens 30% of the time, "30%" every single time scores
    0.3 * 0.7 = 0.21. Beating that is the minimum bar for claiming to know
    anything: it is what you would achieve by reading a climate table and
    never looking at a weather model.
    """
    if base_rate is None:
        return 0.25          # the coin-flip benchmark
    p = min(1.0, max(0.0, base_rate))
    return p * (1.0 - p)


@dataclass
class Verdict:
    state: CalibrationState
    n: int = 0
    brier: float | None = None
    benchmark: float | None = None
    base_rate: float | None = None
    mean_forecast: float | None = None
    detail: str = ""

    @property
    def may_enter(self) -> bool:
        return self.state is not CalibrationState.DEGRADED

    @property
    def edge_over_climatology(self) -> float | None:
        if self.brier is None or self.benchmark is None:
            return None
        return self.benchmark - self.brier      # positive is better than naive

    def describe(self) -> str:
        if self.n == 0:
            return f"{self.state.value}: no settled forecasts yet"
        if self.brier is None or self.benchmark is None:
            # Rows counted but not scored. Say so plainly rather than crashing
            # the scan loop on a log line - this is the summary of a safety
            # check, and it must never be the thing that breaks.
            return (f"{self.state.value}: {self.n} forecasts, no score yet"
                    + (f" ({self.detail})" if self.detail else ""))
        return (f"{self.state.value}: Brier {self.brier:.4f} vs climatology "
                f"{self.benchmark:.4f} over {self.n} forecasts "
                f"({self.edge_over_climatology:+.4f})")


class CalibrationGate:
    """Reads settled forecasts and decides whether a strategy may still enter."""

    def __init__(self, journal, cfg: CalibrationConfig | None = None):
        self.journal = journal
        self.cfg = cfg or CalibrationConfig()
        self._cache: dict[str, Verdict] = {}
        self._checked_at: dict[str, datetime] = {}

    # ------------------------------------------------------------------
    def verdict(self, estimator: str, force: bool = False) -> Verdict:
        if not self.cfg.enabled:
            return Verdict(CalibrationState.UNPROVEN, detail="gate disabled")

        last = self._checked_at.get(estimator)
        if (not force and last is not None
                and (datetime.now(timezone.utc) - last).total_seconds()
                < self.cfg.refresh_seconds
                and estimator in self._cache):
            return self._cache[estimator]

        try:
            stats = self.journal.calibration(
                estimator=estimator, since=self.cfg.since or None)
        except Exception as exc:  # noqa: BLE001
            # A calibration read that fails must not stop trading. Failing
            # closed here would let a database hiccup halt the bot.
            log.error("CALIBRATION  could not read scores for %s: %s",
                      estimator, exc)
            return Verdict(CalibrationState.UNPROVEN,
                           detail=f"calibration unavailable: {exc}")

        v = self._judge(stats)
        self._cache[estimator] = v
        self._checked_at[estimator] = datetime.now(timezone.utc)
        log.info("CALIBRATION  %s -> %s", estimator, v.describe())
        return v

    def _judge(self, stats: dict) -> Verdict:
        n = int(stats.get("n") or 0)
        # None means this journal cannot count contracts, which is not the
        # same as counting zero. Abstain and fall back to the row rule rather
        # than refusing to certify anything at all.
        raw_contracts = stats.get("contracts")
        contracts = None if raw_contracts is None else int(raw_contracts)
        brier = stats.get("brier")
        base = stats.get("base_rate")
        bench = climatology_brier(base)

        if n < self.cfg.min_sample or brier is None:
            return Verdict(
                CalibrationState.UNPROVEN, n=n, brier=brier, benchmark=bench,
                base_rate=base, mean_forecast=stats.get("mean_forecast"),
                detail=(f"{n} settled forecasts, need {self.cfg.min_sample} "
                        f"before this can be judged either way"))

        edge = bench - brier

        # DEGRADED is still decided on rows, deliberately. Refusing to open
        # new positions on thin evidence costs an opportunity; granting
        # permission on thin evidence costs money. The two errors are not
        # symmetric, so the bar is not either.
        if (contracts is not None and edge > -self.cfg.degraded_margin
                and contracts < self.cfg.min_contracts):
            return Verdict(
                CalibrationState.UNPROVEN, n=n, brier=brier, benchmark=bench,
                base_rate=base, mean_forecast=stats.get("mean_forecast"),
                detail=(f"{n} forecasts but only {contracts} distinct "
                        f"contracts, and contracts are what carry the "
                        f"information - need {self.cfg.min_contracts}"))
        if edge <= -self.cfg.degraded_margin:
            return Verdict(
                CalibrationState.DEGRADED, n=n, brier=brier, benchmark=bench,
                base_rate=base, mean_forecast=stats.get("mean_forecast"),
                detail=(f"Brier {brier:.4f} is {-edge:.4f} WORSE than simply "
                        f"predicting the {base:.0%} base rate"))
        if edge >= self.cfg.calibrated_margin:
            return Verdict(
                CalibrationState.CALIBRATED, n=n, brier=brier, benchmark=bench,
                base_rate=base, mean_forecast=stats.get("mean_forecast"),
                detail=f"beating climatology by {edge:.4f} over {n} forecasts")
        return Verdict(
            CalibrationState.UNPROVEN, n=n, brier=brier, benchmark=bench,
            base_rate=base, mean_forecast=stats.get("mean_forecast"),
            detail=(f"Brier {brier:.4f} against a {bench:.4f} benchmark - "
                    f"no clear verdict either way"))

    # ------------------------------------------------------------------
    def may_enter(self, estimator: str) -> tuple[bool, str]:
        """The question the scan loop asks before opening a position."""
        v = self.verdict(estimator)
        if v.state is CalibrationState.DEGRADED:
            return False, v.describe()
        if v.state is CalibrationState.UNPROVEN and not self.cfg.unproven_may_trade:
            return False, v.describe()
        return True, v.describe()

    @staticmethod
    def may_exit(estimator: str) -> tuple[bool, str]:
        """Always. A strategy that may not enter still has to be able to leave.

        This exists as a method so the rule is written down somewhere a future
        change would have to deliberately edit, rather than being an absence.
        """
        return True, "exits are never gated on calibration"
