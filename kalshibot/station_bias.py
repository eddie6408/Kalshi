"""How far our forecast sits from the thermometer, per station.

An ensemble grid cell is not an airport. Open-Meteo's cell for Miami is mostly
water; the settlement station is a runway inland. That is a real, physical
offset, and on 24 September it measured like the largest known error in this
bot's forecasts:

    KXHIGHMIA  KMIA  n=71  -2.82F        KXHIGHLAX  KLAX  n=92  +0.98F
    KXHIGHAUS  KAUS  n=88  -1.98F        KXHIGHCHI  KMDW  n=94  -0.45F
    KXHIGHDEN  KDEN  n=70  -1.47F        KXHIGHNY   KNYC  n=99  +0.26F

**This module does not correct anything.** It measures. Here is why.

## The correction was built, validated, and rejected

Walk-forward, offset fitted only on days strictly before each test day:

    test day        n   raw MAE  corr MAE    change
    2026-09-21    161     1.606     1.682    +0.076
    2026-09-22    124     2.378     2.056    -0.322
    2026-09-23     55     1.325     2.549    +1.224
    ALL           553     1.890     1.962    +0.072      <- worse

One day of six improved. The 21 September audit's rule was explicit: a change
that fails walk-forward is rejected however good it looks in sample. So it is.

## Why it failed, which is the part worth keeping

Those n=70-99 counts are not 70-99 independent facts. Each station is forecast
roughly six times a day, so the real sample is **seven days**:

    station   mean offset   day-to-day sd
    KAUS         -1.55F         1.12F
    KDEN         -0.56F         2.16F
    KLAX         +0.88F         1.72F
    KMDW         -0.02F         1.81F
    KMIA         -2.90F         2.53F
    KNYC         +0.96F         1.71F

The day-to-day swing is as large as the mean at five of six stations. Miami,
the worst offender, ran -3F to -5F for five straight days and then came in
+2.09F on 23 September. A correction fitted through the 22nd was pointing the
wrong way on the 23rd, which is the whole of that +1.224 column.

Treating rows as independent put Miami's offset at twelve standard errors from
zero. Clustering by day - which is the only honest unit, because one day's
weather regime drives every forecast made for it - puts it nearer three. This
is the same clustering trap the 21 September audit documented for the
probability log, walked into a second time by whoever wrote this module. The
guard below now clusters, so the next person gets the real number.

## What this is for

Measurement, on the dashboard and in `diagnostics/settle.py`: the report that
tells you a station has gone wrong. When a station drifts 4F for a fortnight,
that is a config or data problem to go and find, not a number to subtract.

Revisit correcting only when there are 20+ distinct target dates per station,
and only if walk-forward accepts it on that data. Not before.

Sign convention: **offset = predicted - observed**. Positive means we forecast
warmer than it turned out.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: Distinct target DATES, not rows, before an offset means anything. Set from
#: the day-to-day spreads above: at sd ~2F, twenty days gives a standard error
#: near 0.45F, which can separate a 2F station offset from weather noise.
#: Seven days could not, and the walk-forward said so.
MIN_DAYS = 20

#: Standard errors, computed across DAYS, that an offset must clear.
MIN_SIGNIFICANCE = 3.0

#: Past this, it is a station or data problem to investigate, not an offset.
MAX_OFFSET_F = 5.0


@dataclass(frozen=True)
class StationBias:
    station: str
    n_rows: int
    n_days: int
    offset_f: float          # predicted - observed, mean of daily means
    stderr_f: float          # standard error ACROSS DAYS
    day_sd_f: float
    trustworthy: bool
    reason: str

    @property
    def significance(self) -> float:
        """Standard errors from zero, across days.

        A zero standard error means every day agreed exactly. That is the
        MOST convincing an offset can be, not the least - reading it as zero
        sigma would refuse the one case there is no doubt about. It only
        arises with synthetic data, but getting it backwards in a guard is
        how a guard ends up rejecting the wrong things.
        """
        if self.stderr_f == 0:
            return float("inf") if self.offset_f else 0.0
        if self.stderr_f == float("inf"):
            return 0.0
        return abs(self.offset_f) / self.stderr_f

    def describe(self) -> str:
        sign = "+" if self.offset_f >= 0 else ""
        return (f"{self.station}: {sign}{self.offset_f:.2f}F over {self.n_days} "
                f"days ({self.n_rows} forecasts), day-to-day sd "
                f"{self.day_sd_f:.2f}F, {self.significance:.1f} sigma"
                + ("" if self.trustworthy else f" - {self.reason}"))


def fit_offsets(conn: sqlite3.Connection,
                as_of: str | None = None) -> dict[str, StationBias]:
    """Measure each station's offset, clustered by target date.

    The daily mean is the unit of observation. One day's weather regime drives
    every forecast made for that day, so six forecasts for one day are one
    fact, not six - and the standard error has to be computed across days or
    it will be roughly sqrt(6) too small.

    `as_of` is a target_date; only days strictly before it are used. Pass it
    whenever the answer will touch a forecast, so a day can never inform a
    prediction of itself.
    """
    sql = ("SELECT station, target_date, predicted_mean_f, actual_f "
           "FROM forecasts WHERE actual_f IS NOT NULL "
           "AND station IS NOT NULL AND station != ''")
    params: tuple = ()
    if as_of:
        sql += " AND target_date < ?"
        params = (as_of,)

    by_day: dict[str, dict[str, list[float]]] = {}
    try:
        for r in conn.execute(sql, params):
            try:
                err = float(r["predicted_mean_f"]) - float(r["actual_f"])
            except (TypeError, ValueError):
                continue
            by_day.setdefault(r["station"], {}).setdefault(
                r["target_date"], []).append(err)
    except sqlite3.Error as exc:
        log.error("BIAS  could not read forecast history: %s", exc)
        return {}

    out: dict[str, StationBias] = {}
    for station, days in by_day.items():
        daily = [sum(v) / len(v) for v in days.values()]
        n_rows = sum(len(v) for v in days.values())
        n_days = len(daily)
        mean = sum(daily) / n_days
        if n_days > 1:
            var = sum((x - mean) ** 2 for x in daily) / (n_days - 1)
            day_sd = var ** 0.5
            stderr = (var / n_days) ** 0.5
        else:
            day_sd = stderr = float("inf")

        trustworthy, reason = True, ""
        if n_days < MIN_DAYS:
            trustworthy, reason = False, (
                f"only {n_days} days, need {MIN_DAYS} - rows are not days")
        elif abs(mean) > MAX_OFFSET_F:
            trustworthy, reason = False, (
                f"{mean:+.1f}F is past the {MAX_OFFSET_F:.1f}F cap - this is a "
                f"station or data problem to find, not an offset to subtract")

        candidate = StationBias(
            station=station, n_rows=n_rows, n_days=n_days, offset_f=mean,
            stderr_f=stderr, day_sd_f=day_sd, trustworthy=True, reason="")
        if trustworthy and candidate.significance < MIN_SIGNIFICANCE:
            trustworthy, reason = False, (
                f"{candidate.significance:.1f} sigma across days, need "
                f"{MIN_SIGNIFICANCE:.1f}")

        out[station] = StationBias(
            station=station, n_rows=n_rows, n_days=n_days, offset_f=mean,
            stderr_f=stderr, day_sd_f=day_sd, trustworthy=trustworthy,
            reason=reason)
    return out
