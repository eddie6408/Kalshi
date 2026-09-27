"""Volatility engine: rolling per-market price history and the metrics the
classifier, scanner and strategies read (section 17).

Everything is computed from observations at or before the evaluation time,
so replay cannot leak future information into a metric.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from lvt.models import YES, Snapshot


@dataclass(slots=True)
class Obs:
    ts: float
    bid: Optional[float]      # YES bid
    ask: Optional[float]      # YES ask
    mid: Optional[float]
    bid_size: float
    ask_size: float
    volume: float
    last: Optional[float]


def linreg(ts: list[float], ys: list[float]) -> tuple[float, float]:
    """Slope in cents per MINUTE and r^2 of y against time."""
    n = len(ts)
    if n < 3:
        return 0.0, 0.0
    t0 = ts[0]
    xs = [(t - t0) / 60.0 for t in ts]
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 1e-12:
        return 0.0, 0.0
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    syy = sum((y - my) ** 2 for y in ys)
    slope = sxy / sxx
    r2 = 0.0 if syy <= 1e-12 else (sxy * sxy) / (sxx * syy)
    return slope, r2


def resample(ts: list[float], ys: list[float], step: float, end: float, start: float) -> list[float]:
    """Last value at or before each grid point (no interpolation forward)."""
    out: list[float] = []
    j = 0
    t = start
    last = None
    while t <= end + 1e-9:
        while j < len(ts) and ts[j] <= t:
            last = ys[j]
            j += 1
        if last is not None:
            out.append(last)
        t += step
    return out


def rms(xs: list[float]) -> float:
    """Realised volatility: root-mean-square of changes, NOT demeaned, so a
    steady trend counts as movement (a demeaned stdev would call it 0)."""
    if not xs:
        return 0.0
    return math.sqrt(sum(x * x for x in xs) / len(xs))


@dataclass
class Metrics:
    ticker: str
    ts: float
    n_obs: int
    bid: Optional[float]
    ask: Optional[float]
    mid: Optional[float]
    last: Optional[float]
    spread: Optional[float]
    spread_pct: Optional[float]
    recent_high: Optional[float]
    recent_low: Optional[float]
    range_cents: float
    change_short: float
    change_medium: float
    change_long: float
    pct_change_long: float
    velocity_short: float          # cents / min
    velocity_medium: float
    r2_medium: float
    acceleration: float            # recent-half slope minus first-half slope
    realized_vol: float            # RMS of 1-min mid changes (cents), medium window
    realized_vol_long: float
    volume_long: float             # contracts traded in the long window
    volume_medium: float
    volume_acceleration: float     # rate(recent half) / rate(first half) of medium window
    liquidity_usd: float
    top_bid_size: float
    top_ask_size: float
    depth_yes: float               # contracts on the YES-bid ladder (fresh depth only)
    depth_no: float
    imbalance: float               # (yes bids - no bids)/(sum); + = buying pressure on YES
    update_freq: float             # mid changes per minute (medium window)
    since_move_s: float            # time since the last meaningful mid move
    data_age_s: float
    history_s: float
    extras: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__ if k != "extras"}
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items()}


class MarketSeries:
    def __init__(self, ticker: str, history_s: float):
        self.ticker = ticker
        self.history_s = history_s
        self.obs: deque[Obs] = deque()
        self.last_snapshot: Optional[Snapshot] = None
        self.first_ts: float = 0.0

    def add(self, snap: Snapshot) -> None:
        q = snap.quote
        mid = (q.yes_bid + q.yes_ask) / 2.0 if (q.yes_bid is not None and q.yes_ask is not None) else None
        if self.obs and snap.ts <= self.obs[-1].ts:
            return                                  # out of order / duplicate
        self.obs.append(Obs(snap.ts, q.yes_bid, q.yes_ask, mid, q.yes_bid_size, q.yes_ask_size,
                            q.volume, q.last))
        if not self.first_ts:
            self.first_ts = snap.ts
        self.last_snapshot = snap
        cutoff = snap.ts - self.history_s
        while self.obs and self.obs[0].ts < cutoff:
            self.obs.popleft()

    # -- side-relative views used by the pattern detectors -------------------
    def view(self, side: str, since: float) -> tuple[list[float], list[float], list[float], list[float]]:
        """(ts, mid, bid, ask) for `side`, observations with ts >= since and a
        two-sided quote."""
        ts, mids, bids, asks = [], [], [], []
        for o in self.obs:
            if o.ts < since or o.mid is None:
                continue
            ts.append(o.ts)
            if side == YES:
                mids.append(o.mid); bids.append(o.bid); asks.append(o.ask)
            else:
                mids.append(100.0 - o.mid); bids.append(100.0 - o.ask); asks.append(100.0 - o.bid)
        return ts, mids, bids, asks

    def volume_between(self, t0: float, t1: float) -> float:
        v0 = v1 = None
        for o in self.obs:
            if o.ts <= t0:
                v0 = o.volume
            if o.ts <= t1:
                v1 = o.volume
        if v1 is None:
            return 0.0
        if v0 is None:
            v0 = self.obs[0].volume if self.obs else v1
        return max(v1 - v0, 0.0)


def compute_metrics(series: MarketSeries, now: float, cfg) -> Optional[Metrics]:
    """cfg is the [volatility] config section."""
    if not series.obs or series.last_snapshot is None:
        return None
    snap = series.last_snapshot
    last = series.obs[-1]
    sw, mw, lw = cfg.short_window_s, cfg.medium_window_s, cfg.long_window_s
    ts_l, mid_l, _, _ = series.view(YES, now - lw)
    ts_m = [t for t in ts_l if t >= now - mw]
    mid_m = mid_l[len(ts_l) - len(ts_m):]
    ts_s = [t for t in ts_l if t >= now - sw]
    mid_s = mid_l[len(ts_l) - len(ts_s):]

    def change(ts, ys):
        return (ys[-1] - ys[0]) if len(ys) >= 2 else 0.0

    vel_s, _ = linreg(ts_s, mid_s)
    vel_m, r2_m = linreg(ts_m, mid_m)
    accel = 0.0
    if len(ts_m) >= 6:
        half = now - mw / 2.0
        a_ts = [t for t in ts_m if t < half]
        b_ts = [t for t in ts_m if t >= half]
        if len(a_ts) >= 3 and len(b_ts) >= 3:
            s1, _ = linreg(a_ts, mid_m[:len(a_ts)])
            s2, _ = linreg(b_ts, mid_m[len(a_ts):])
            accel = s2 - s1

    def rvol(ts, ys, window):
        if len(ts) < 2:
            return 0.0
        grid = resample(ts, ys, 60.0, now, max(now - window, ts[0]))
        return rms([b - a for a, b in zip(grid, grid[1:])])

    vol_m = rvol(ts_m, mid_m, mw)
    vol_l = rvol(ts_l, mid_l, lw)

    changes = sum(1 for a, b in zip(mid_m, mid_m[1:]) if abs(b - a) > 1e-9)
    span_m = (ts_m[-1] - ts_m[0]) / 60.0 if len(ts_m) >= 2 else 0.0
    update_freq = changes / span_m if span_m > 0 else 0.0

    since_move = now - series.first_ts
    if mid_l:
        ref = mid_l[-1]
        for t, y in zip(reversed(ts_l), reversed(mid_l)):
            if abs(y - ref) >= cfg.meaningful_move_cents:
                since_move = now - t
                break

    vol_long = series.volume_between(now - lw, now)
    vol_med = series.volume_between(now - mw, now)
    v_first = series.volume_between(now - mw, now - mw / 2.0)
    v_second = series.volume_between(now - mw / 2.0, now)
    vol_acc = (v_second / v_first) if v_first > 0 else (2.0 if v_second > 0 else 1.0)

    depth_yes = depth_no = 0.0
    if snap.depth is not None and snap.ts - snap.depth.ts <= 10:
        depth_yes = sum(s for _, s in snap.depth.yes)
        depth_no = sum(s for _, s in snap.depth.no)
    tot = depth_yes + depth_no
    if tot <= 0:
        depth_yes_top, depth_no_top = last.bid_size, last.ask_size
        tot_top = depth_yes_top + depth_no_top
        imbalance = (depth_yes_top - depth_no_top) / tot_top if tot_top > 0 else 0.0
    else:
        imbalance = (depth_yes - depth_no) / tot

    spread = snap.spread()
    mid = last.mid
    hi = max(mid_l) if mid_l else None
    lo = min(mid_l) if mid_l else None
    first_l = mid_l[0] if mid_l else None
    return Metrics(
        ticker=series.ticker, ts=now, n_obs=len(series.obs),
        bid=last.bid, ask=last.ask, mid=mid, last=last.last,
        spread=spread, spread_pct=(spread / mid) if (spread is not None and mid) else None,
        recent_high=hi, recent_low=lo, range_cents=(hi - lo) if hi is not None else 0.0,
        change_short=change(ts_s, mid_s), change_medium=change(ts_m, mid_m), change_long=change(ts_l, mid_l),
        pct_change_long=((mid_l[-1] - first_l) / first_l) if (mid_l and first_l) else 0.0,
        velocity_short=vel_s, velocity_medium=vel_m, r2_medium=r2_m, acceleration=accel,
        realized_vol=vol_m, realized_vol_long=vol_l,
        volume_long=vol_long, volume_medium=vol_med, volume_acceleration=vol_acc,
        liquidity_usd=snap.liquidity_usd(), top_bid_size=last.bid_size, top_ask_size=last.ask_size,
        depth_yes=depth_yes, depth_no=depth_no, imbalance=imbalance,
        update_freq=update_freq, since_move_s=since_move,
        data_age_s=max(now - snap.ts, 0.0), history_s=now - series.first_ts,
    )


def side_metrics(m: Metrics, side: str) -> dict:
    """The few metrics strategies need, expressed for `side` (NO mirrors YES)."""
    if side == YES:
        return {"velocity_short": m.velocity_short, "velocity_medium": m.velocity_medium,
                "acceleration": m.acceleration, "change_long": m.change_long,
                "imbalance": m.imbalance, "recent_high": m.recent_high, "recent_low": m.recent_low}
    return {"velocity_short": -m.velocity_short, "velocity_medium": -m.velocity_medium,
            "acceleration": -m.acceleration, "change_long": -m.change_long,
            "imbalance": -m.imbalance,
            "recent_high": None if m.recent_low is None else 100.0 - m.recent_low,
            "recent_low": None if m.recent_high is None else 100.0 - m.recent_high}


def regime(vol: float, cfg) -> str:
    if vol >= cfg.vol_extreme:
        return "EXTREME_VOLATILITY"
    if vol >= cfg.vol_high:
        return "HIGH_VOLATILITY"
    if vol >= cfg.vol_low:
        return "NORMAL_VOLATILITY"
    return "LOW_VOLATILITY"
