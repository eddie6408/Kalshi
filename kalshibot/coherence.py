"""Is a set of probabilities consistent with itself?

A forecast bot fails in two very different ways, and they look identical in a
log:

    our model disagrees with the market   <- this is an edge
    our model disagrees with arithmetic   <- this is a bug

The second one is far more common, and it is the more dangerous of the two
because a broken distribution produces ENORMOUS apparent edges. A real example
from the Kalshi demo board:

    P(high >= 82)      44.6%
    P(high in 81..82)   7.4%

The second contains the first, so it cannot be smaller. One of those numbers
is wrong, and trading on either is trading on a bug. The market looked
"mispriced" by 30 percentage points and the trade showed a +212% return.

These checks are cheap, they run over one event's markets at a time, and they
only ever say "do not trade this" - never "trade this".
"""
from __future__ import annotations

from dataclasses import dataclass

#: Buckets are allowed to overshoot 100% by this much before it is called a
#: problem. Rounding and overlapping half-degree edges are real.
BUCKET_TOLERANCE = 0.02
_EPS = 1e-9


@dataclass
class Claim:
    """One market's parsed strike and the probability we assigned it."""
    ticker: str
    lower: float | None      # inclusive lower bound, None = unbounded below
    upper: float | None      # inclusive upper bound, None = unbounded above
    probability: float

    @property
    def is_threshold(self) -> bool:
        return self.lower is not None and self.upper is None

    @property
    def is_bucket(self) -> bool:
        return self.lower is not None and self.upper is not None


def check(claims: list[Claim]) -> list[str]:
    """Return a list of problems. Empty means the distribution is consistent."""
    problems: list[str] = []
    if len(claims) < 2:
        return problems

    # 1. Thresholds must be monotonic: a higher bar cannot be more likely.
    thresholds = sorted((c for c in claims if c.is_threshold),
                        key=lambda c: c.lower)
    for a, b in zip(thresholds, thresholds[1:]):
        if b.probability > a.probability + _EPS:
            problems.append(
                f"P(>={b.lower:g}) = {b.probability * 100:.1f}% exceeds "
                f"P(>={a.lower:g}) = {a.probability * 100:.1f}% "
                f"({b.ticker} vs {a.ticker}); a higher bar cannot be more likely")

    # 2. Mutually exclusive buckets cannot sum above 100%.
    buckets = [c for c in claims if c.is_bucket]
    if buckets:
        total = sum(c.probability for c in buckets)
        if total > 1.0 + BUCKET_TOLERANCE:
            problems.append(
                f"{len(buckets)} mutually exclusive buckets sum to "
                f"{total * 100:.1f}%")

    # 3. A threshold whose whole range sits inside a bucket cannot be more
    #    likely than that bucket.
    for b in buckets:
        for t in thresholds:
            if b.lower <= t.lower <= b.upper and t.probability > b.probability + _EPS:
                problems.append(
                    f"P({t.ticker} >= {t.lower:g}) = {t.probability * 100:.1f}% "
                    f"exceeds the bucket {b.lower:g}-{b.upper:g} that contains "
                    f"it ({b.ticker}, {b.probability * 100:.1f}%)")

    # 4. Nothing may be certain. A probability pinned at 0 or 1 means the
    #    model has stopped modelling.
    for c in claims:
        if c.probability <= 0.0 or c.probability >= 1.0:
            problems.append(
                f"{c.ticker} is {c.probability * 100:.0f}% - a forecast is "
                f"never certain")
    return problems
