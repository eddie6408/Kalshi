"""Test helpers: synthetic markets, price paths and an engine wired to a
throwaway database and the paper venue."""
from __future__ import annotations

import math
import tempfile
from pathlib import Path

from lvt.alerts import Alerter
from lvt.clock import ReplayClock
from lvt.config import ROOT, load_config
from lvt.db import open_state_db
from lvt.engine.trader import Engine
from lvt.execution.paper import PaperVenue
from lvt.models import Depth, MarketInfo, Quote, Snapshot

T0 = 1_800_000_000.0          # fixed epoch for deterministic tests


def cfg(overrides: dict | None = None, env: dict | None = None):
    base = {"execution": {"touch_miss_probability": 0.0, "latency_jitter_ms": 0.0},
            "signals": {"min_history_s": 0}}
    if overrides:
        base = _merge(base, overrides)
    return load_config(ROOT, overrides=base, env=dict(env or {}))


def _merge(a, b):
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def market(ticker="KXATPMATCH-26SEP27AAABBB-AAA", sport="TENNIS", close_in=86400.0 * 7) -> MarketInfo:
    return MarketInfo(ticker=ticker, event_ticker=ticker.rsplit("-", 1)[0], series_ticker=ticker.split("-")[0],
                      category="Sports", sport=sport, title="A vs B", close_ts=T0 + close_in,
                      fee_type="quadratic", fee_multiplier=1.0, volume_24h=100000)


def quote(ticker, ts, bid, ask, bid_size=500.0, ask_size=500.0, volume=0.0, status="active", result="") -> Quote:
    return Quote(ticker, ts, bid, ask, bid_size, ask_size, None, volume, 0.0, status, result)


def snap(ticker, ts, bid, ask, depth: Depth | None = None, **kw) -> Snapshot:
    return Snapshot(ticker, ts, quote(ticker, ts, bid, ask, **kw), depth)


def path(points: list[tuple[float, float]], step=2.0) -> list[tuple[float, float]]:
    """Linear interpolation of (seconds, mid) knots every `step` seconds."""
    out = []
    for (t0, y0), (t1, y1) in zip(points, points[1:]):
        t = t0
        while t < t1 - 1e-9:
            out.append((t, y0 + (y1 - y0) * (t - t0) / (t1 - t0)))
            t += step
    out.append(points[-1])
    return out


def mid_to_book(mid: float) -> tuple[float, float]:
    """1c-wide book around a mid, on whole cents."""
    bid = math.floor(mid - 0.5 + 1e-9)
    return float(bid), float(bid + 1)


class Harness:
    def __init__(self, overrides=None, env="PAPER", tickers=None, db_path=None, shadow=False):
        self.tmp = tempfile.mkdtemp()
        self.cfg = cfg(overrides)
        self.db_path = db_path or Path(self.tmp) / "state.db"
        self.db = open_state_db(self.db_path)
        self.markets = {m.ticker: m for m in (tickers or [market()])}
        self.clock = ReplayClock(T0)
        self.venue = PaperVenue(self.cfg, env, self.markets, shadow=shadow)
        self.venue.restore(self.db)
        self.alerter = Alerter(self.db, "test", 0)
        self.engine = Engine(self.cfg, env, "test-run", self.db, self.venue, self.markets, self.alerter, self.clock)
        self.volume = 0.0
        self.last: dict[str, Snapshot] = {}

    def step(self, t: float, books: dict[str, tuple[float, float]], vol_per_step=20.0, depth=None, **kw):
        now = T0 + t
        self.clock.advance_to(now)
        self.volume += vol_per_step
        snaps = {}
        for tk, (b, a) in books.items():
            snaps[tk] = snap(tk, now, b, a, depth.get(tk) if depth else None, volume=self.volume, **kw)
        self.last.update(snaps)
        self.engine.on_snapshots(snaps, now)
        return snaps

    def run_path(self, ticker, pts, **kw):
        for t, mid in pts:
            self.step(t, {ticker: mid_to_book(mid)}, **kw)

    def signals(self, **where):
        sql = "SELECT * FROM signals"
        if where:
            sql += " WHERE " + " AND ".join(f"{k}=?" for k in where)
        return self.db.query(sql + " ORDER BY ts", tuple(where.values()))
