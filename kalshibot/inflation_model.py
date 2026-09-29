"""The inflation nowcast model: pure functions, no network.

What a Kalshi inflation contract asks. "Will CPI rise more than 0.3% in
December?" resolves on the figure the statistical agency PUBLISHES, to one
decimal. So YES needs the published 0.4 or more, which needs the unrounded
change to be at least 0.35:

    P(YES) = P(X >= strike + 0.05),   X ~ Normal(mu, sigma)

`mu` - the internal weighted nowcast:
    mu = w * cleveland + (1 - w) * trend - bias
where `cleveland` is the Cleveland Fed's latest nowcast for that month,
`trend` is the mean of the previous six released months (month over month)
or the latest released year-over-year rate, and `w` and `bias` are fitted.

The error distribution - how wrong that has actually been, measured on the
Cleveland Fed's own archive (every month since 2013, with every day's
nowcast and the value later released) by days before the release. It is
used as MEASURED (empirical residual quantiles), not as a normal curve: a
normal fitted to the same errors put 4% on outcomes that happened 14% of the
time, because inflation surprises are fat-tailed. The blend weight is
fitted per distance from the release too - close to it the Cleveland
nowcast dominates, far from it the trend earns weight. `fit()` measures all
of this; `scripts/fit_inflation_model.py` writes data/inflation_model.json.

    P(YES) = P(actual >= t) = P(residual <= mu - t),  t = strike + 0.05

For a month the Cleveland Fed is not nowcasting yet, the trend alone prices
it, with the error the trend has had that many months ahead. The strategy's
lead-time gate keeps those far months out of trading; they are still priced
and scored, so the record builds.

Regime watch: when the latest nowcast has moved more than `regime_threshold`
over its last five readings, something changed (an energy shock, a revised
input) and confidence is cut until it settles.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date
from statistics import mean

#: Kalshi series -> (measure, frequency)
SERIES = {
    "KXCPI": ("CPI", "mom"),
    "KXCPICORE": ("CORE_CPI", "mom"),
    "KXCPIYOY": ("CPI", "yoy"),
    "KXCPICOREYOY": ("CORE_CPI", "yoy"),
    "KXPCECORE": ("CORE_PCE", "mom"),
    "KXPCEHEAD": ("PCE", "mom"),
}
#: days-before-release buckets the error is measured in (upper bounds)
DAY_BUCKETS = (3, 7, 14, 21, 31, 60)
STEP = 0.1                        # published to one decimal
MIN_SIGMA = 0.03
MODEL_VERSION = "inflation_nowcast/blend@1"


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bucket_of(days: float) -> int:
    for b in DAY_BUCKETS:
        if days <= b:
            return b
    return DAY_BUCKETS[-1]


def prob_above(strike: float, mu: float, sigma: float, step: float = STEP) -> float:
    """P(the published figure, rounded to `step`, is above `strike`)."""
    threshold = strike + step / 2.0
    if sigma <= 0:
        return 1.0 if mu >= threshold else 0.0
    return 1.0 - _phi((threshold - mu) / sigma)


_THRESHOLD_TAIL = re.compile(r"-T(-?\d+(?:\.\d+)?)$")


def split_threshold(ticker: str) -> tuple[str, float | None]:
    """"KXCPI-26DEC-T-0.1" -> ("KXCPI-26DEC", -0.1). A negative strike has
    its own minus sign, so splitting on the last "-" gets both parts wrong."""
    m = _THRESHOLD_TAIL.search(ticker or "")
    if not m:
        return (ticker or "").rsplit("-", 1)[0], None
    return ticker[:m.start()], float(m.group(1))


def month_add(key: str, n: int) -> str:
    y, m = (int(x) for x in key.split("-"))
    m0 = y * 12 + (m - 1) + n
    return f"{m0 // 12:04d}-{m0 % 12 + 1:02d}"


def months_between(a: str, b: str) -> int:
    ya, ma = (int(x) for x in a.split("-"))
    yb, mb = (int(x) for x in b.split("-"))
    return (yb * 12 + mb) - (ya * 12 + ma)


# ------------------------------------------------------------------ params
DEFAULT_SIGMA = {3: 0.08, 7: 0.09, 14: 0.10, 21: 0.11, 31: 0.12, 60: 0.14}
DEFAULT_TREND_SIGMA = {"mom": {1: 0.18, 2: 0.20, 3: 0.22, 4: 0.25},
                       "yoy": {1: 0.25, 2: 0.35, 3: 0.45, 4: 0.55}}


@dataclass
class Params:
    bias: float = 0.0
    blend_w: float = 0.85
    sigma: dict = field(default_factory=lambda: dict(DEFAULT_SIGMA))
    trend_sigma: dict = field(default_factory=dict)
    n: int = 0
    fitted: bool = False
    #: per days-bucket blend weight (overrides blend_w)
    blend_w_by_days: dict = field(default_factory=dict)
    #: per days-bucket residual quantiles (forecast - actual), 0..100
    quantiles: dict = field(default_factory=dict)

    def w_for(self, days: float) -> float:
        b = bucket_of(days)
        return float(self.blend_w_by_days.get(b, self.blend_w_by_days.get(str(b), self.blend_w)))

    def q_for(self, days: float) -> list[float] | None:
        b = bucket_of(days)
        q = self.quantiles.get(b) or self.quantiles.get(str(b))
        return q if q and len(q) >= 11 else None

    def sigma_for(self, days: float) -> float:
        return max(MIN_SIGMA, float(self.sigma.get(bucket_of(days),
                                                   self.sigma.get(str(bucket_of(days)), 0.12))))

    def trend_sigma_for(self, freq: str, months_ahead: int) -> float:
        table = self.trend_sigma or DEFAULT_TREND_SIGMA[freq]
        k = max(1, min(4, months_ahead))
        return max(MIN_SIGMA, float(table.get(k, table.get(str(k), 0.3))))

    @classmethod
    def from_dict(cls, d: dict | None, freq: str) -> "Params":
        d = d or {}
        return cls(bias=float(d.get("bias", 0.0)), blend_w=float(d.get("blend_w", 0.85)),
                   sigma={int(k): float(v) for k, v in (d.get("sigma") or DEFAULT_SIGMA).items()},
                   trend_sigma={int(k): float(v) for k, v in
                                (d.get("trend_sigma") or DEFAULT_TREND_SIGMA[freq]).items()},
                   n=int(d.get("n", 0)), fitted=bool(d.get("fitted", False)),
                   blend_w_by_days={int(k): float(v) for k, v in
                                    (d.get("blend_w_by_days") or {}).items()},
                   quantiles={int(k): [float(x) for x in v] for k, v in
                              (d.get("quantiles") or {}).items()})


def empirical_cdf(q: list[float], x: float) -> float:
    """P(residual <= x) from 101 quantiles, linear between them, with a
    little mass kept beyond the observed extremes (never 0 or 1)."""
    n = len(q) - 1
    if x <= q[0]:
        span = max(1e-6, q[1] - q[0])
        return max(0.002, 0.5 / n * math.exp((x - q[0]) / (span * 4)))
    if x >= q[-1]:
        span = max(1e-6, q[-1] - q[-2])
        return min(0.998, 1 - 0.5 / n * math.exp(-(x - q[-1]) / (span * 4)))
    for i in range(n):
        if q[i] <= x <= q[i + 1]:
            f = 0.0 if q[i + 1] == q[i] else (x - q[i]) / (q[i + 1] - q[i])
            return (i + f) / n
    return 0.5


def quantiles_of(xs: list[float], k: int = 100) -> list[float]:
    s = sorted(xs)
    out = []
    for i in range(k + 1):
        pos = (len(s) - 1) * i / k
        lo = int(math.floor(pos))
        hi = min(len(s) - 1, lo + 1)
        out.append(s[lo] + (s[hi] - s[lo]) * (pos - lo))
    return out


def prob_yes(strike: float, mu: float, sigma: float, quantiles: list[float] | None,
             step: float = STEP) -> float:
    """P(published > strike). With measured residual quantiles, the measured
    distribution; without, a normal with `sigma`."""
    if quantiles:
        return empirical_cdf(quantiles, mu - (strike + step / 2.0))
    return prob_above(strike, mu, sigma, step)


# ------------------------------------------------------------------ trend
def trend_value(archive: dict, measure: str, freq: str, before: str) -> float | None:
    """Mean of the six released months before `before` (m/m), or the latest
    released rate before it (y/y)."""
    vals = []
    for k in sorted(archive):
        if k >= before:
            break
        h = archive[k].get(measure)
        if h is not None and h.actual is not None:
            vals.append(h.actual)
    if not vals:
        return None
    return mean(vals[-6:]) if freq == "mom" else vals[-1]


def latest_released(archive: dict, measure: str) -> str | None:
    done = [k for k, v in archive.items() if v.get(measure) and v[measure].actual is not None]
    return max(done) if done else None


# ------------------------------------------------------------------ forecast
@dataclass
class Forecast:
    measure: str
    freq: str
    target: str
    mu: float
    sigma: float
    source: str                       # blend | trend | released
    quantiles: list | None = None     # residual quantiles used, if measured
    cleveland: float | None = None
    cleveland_date: date | None = None
    trend: float | None = None
    days_to_release: float | None = None
    months_ahead: int = 0
    regime_shift: bool = False
    regime_delta: float | None = None
    actual: float | None = None


def forecast(archive: dict, measure: str, freq: str, target: str, today: date,
             params: Params, days_to_release: float | None,
             regime_threshold: float = 0.10) -> Forecast | None:
    h = (archive.get(target) or {}).get(measure)
    trend = trend_value(archive, measure, freq, target)
    if h is not None and h.actual is not None and (h.released is None or h.released <= today):
        return Forecast(measure, freq, target, h.actual, 0.01, "released",
                        trend=trend, actual=h.actual, days_to_release=days_to_release)
    points = [(d, v) for d, v in (h.nowcasts if h else []) if d <= today]
    if points:
        d_last, cf = points[-1]
        days = days_to_release if days_to_release is not None else 21.0
        w = params.w_for(days) if trend is not None else 1.0
        mu = w * cf + (1 - w) * (trend if trend is not None else cf) - params.bias
        delta = (cf - points[-6][1]) if len(points) >= 6 else None
        return Forecast(measure, freq, target, mu, params.sigma_for(days), "blend",
                        quantiles=params.q_for(days),
                        cleveland=cf, cleveland_date=d_last, trend=trend,
                        days_to_release=days_to_release,
                        regime_shift=delta is not None and abs(delta) > regime_threshold,
                        regime_delta=delta)
    if trend is None:
        return None
    last = latest_released(archive, measure) or target
    ahead = max(1, months_between(last, target))
    return Forecast(measure, freq, target, trend, params.trend_sigma_for(freq, ahead),
                    "trend", trend=trend, days_to_release=days_to_release, months_ahead=ahead)


# ------------------------------------------------------------------ fitting
def _sd(xs: list[float], centre: float = 0.0) -> float:
    return math.sqrt(sum((x - centre) ** 2 for x in xs) / len(xs)) if xs else float("nan")


def fit(archive: dict, measure: str, freq: str, since: str = "2016-01",
        min_n: int = 30, until: str = "9999-99") -> tuple[dict, dict]:
    """Measure the nowcast's error on the archive. Returns (params, report)."""
    rows = []                          # (days, cf, trend, actual)
    trend_rows: dict[int, list[float]] = {}
    for key in sorted(archive):
        if key < since or key > until:
            continue
        h = archive[key].get(measure)
        if h is None or h.actual is None or h.released is None:
            continue
        tr = trend_value(archive, measure, freq, key)
        for d, v in h.nowcasts:
            days = (h.released - d).days
            if days <= 0:
                continue
            rows.append((days, v, tr, h.actual))
        # the trend alone, k months ahead
        for k in (1, 2, 3, 4):
            t_k = trend_value(archive, measure, freq, month_add(key, -(k - 1)))
            if t_k is not None:
                trend_rows.setdefault(k, []).append(t_k - h.actual)
    usable = [r for r in rows if r[2] is not None]
    if len(usable) < min_n:
        return {"fitted": False, "n": len(usable)}, {"n": len(usable)}

    def best_w(sel):
        # the blend weight minimising squared error (closed form), in [0.5, 1]
        a = [r[1] - r[2] for r in sel]
        b = [r[3] - r[2] for r in sel]
        denom = sum(x * x for x in a)
        return max(0.5, min(1.0, sum(x * y for x, y in zip(a, b)) / denom)) if denom else 1.0

    def lo_of(b_):
        return ([0] + list(DAY_BUCKETS))[DAY_BUCKETS.index(b_)]

    w = best_w(usable)
    w_by = {}
    for b_ in DAY_BUCKETS:
        sel = [r for r in usable if lo_of(b_) < r[0] <= b_]
        w_by[b_] = best_w(sel) if len(sel) >= 50 else w
    errs = [(r[0], w_by[bucket_of(r[0])] * r[1] + (1 - w_by[bucket_of(r[0])]) * r[2] - r[3],
             r[1] - r[3], r[2] - r[3]) for r in usable]
    bias = mean(e[1] for e in errs)
    sigma, report_b, quant = {}, {}, {}
    for b_ in DAY_BUCKETS:
        sel = [e for e in errs if lo_of(b_) < e[0] <= b_]
        if len(sel) >= 20:
            sigma[b_] = max(MIN_SIGMA, _sd([e[1] for e in sel], bias))
        if len(sel) >= 150:
            quant[b_] = [round(x, 5) for x in quantiles_of([e[1] - bias for e in sel])]
        report_b[b_] = {"n": len(sel),
                        "rmse_blend": _sd([e[1] for e in sel]) if sel else None,
                        "rmse_cleveland": _sd([e[2] for e in sel]) if sel else None,
                        "rmse_trend": _sd([e[3] for e in sel]) if sel else None}
    # fill gaps and keep sigma non-decreasing with distance from the release
    last = None
    for b_ in DAY_BUCKETS:
        if b_ not in sigma:
            sigma[b_] = last if last is not None else DEFAULT_SIGMA[b_]
        if last is not None and sigma[b_] < last:
            sigma[b_] = last
        last = sigma[b_]
    trend_sigma = {k: max(MIN_SIGMA, _sd(v)) for k, v in trend_rows.items() if len(v) >= 12}
    params = {"bias": bias, "blend_w": w, "blend_w_by_days": w_by, "quantiles": quant,
              "sigma": sigma, "trend_sigma": trend_sigma,
              "n": len(usable), "months": len({k for k in archive if k >= since}),
              "since": since, "fitted": True}
    return params, {"n": len(usable), "blend_w": w, "bias": bias, "by_days": report_b,
                    "trend_by_months": {k: {"n": len(v), "rmse": _sd(v)} for k, v in trend_rows.items()}}


def calibration(archive: dict, measure: str, freq: str, params: Params,
                since: str = "2016-01", strikes_around: int = 3,
                until: str = "9999-99") -> dict:
    """How honest are the probabilities? Brier and a reliability table for the
    ladder of strikes around each actual, priced from every archived day."""
    buckets: dict[int, list[tuple[float, int]]] = {}
    briers = []
    for key in sorted(archive):
        if key < since or key > until:
            continue
        h = archive[key].get(measure)
        if h is None or h.actual is None or h.released is None:
            continue
        published = round(h.actual + 1e-9, 1)
        tr = trend_value(archive, measure, freq, key)
        for d, v in h.nowcasts[::3]:
            days = (h.released - d).days
            if days <= 0:
                continue
            w = params.w_for(days)
            mu = w * v + (1 - w) * (tr if tr is not None else v) - params.bias
            sig = params.sigma_for(days)
            q = params.q_for(days)
            for i in range(-strikes_around, strikes_around + 1):
                strike = round(published + i * STEP - STEP, 1)
                p = prob_yes(strike, mu, sig, q)
                y = 1 if published > strike + 1e-9 else 0
                briers.append((p - y) ** 2)
                buckets.setdefault(min(9, int(p * 10)), []).append((p, y))
    return {"brier": mean(briers) if briers else None, "n": len(briers),
            "reliability": {k: {"n": len(v), "said": mean(p for p, _ in v),
                                "happened": mean(y for _, y in v)}
                            for k, v in sorted(buckets.items())}}
