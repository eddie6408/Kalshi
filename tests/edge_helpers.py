"""Synthetic live tennis match driving the EDGE engine through the real
Engine: score snapshots every 4 s, quotes every 2 s."""
from __future__ import annotations

from lvt.data.context import normalize_live_data
from lvt.data.events import EventDetector
from lvt.edge.engine import EdgeEngine
from lvt.models import MarketInfo
from tests.helpers import Harness, T0

EVENT = "KXATPMATCH-26SEP28AAABBB"
TA, TB = EVENT + "-AAA", EVENT + "-BBB"
META = {"milestone_id": "m1", "type": "tennis_tournament_singles", "title": "Aaa vs Bbb",
        "details": {"best_of": "3", "tour": "ATP", "gender": "men", "first_competitor_id": "c1",
                    "second_competitor_id": "c2"}, "group": [EVENT]}
PTS = {0: 0, 1: 15, 2: 30, 3: 40}


def market(ticker, name):
    return MarketInfo(ticker=ticker, event_ticker=EVENT, series_ticker="KXATPMATCH", category="Sports",
                      sport="TENNIS", title=f"{name} wins", sub_title=name, close_ts=T0 + 7 * 86400,
                      fee_type="quadratic", fee_multiplier=1.0, volume_24h=100000)


def raw(sets=(0, 0), games=(2, 2), points=(1, 1), server="c1", status="live"):
    return {"status": status, "match_status": "1st_set", "competitor1_id": "c1", "competitor2_id": "c2",
            "server": server, "competitor1_overall_score": sets[0], "competitor2_overall_score": sets[1],
            "competitor1_round_scores": [{"outcome": "ongoing", "score": games[0]}],
            "competitor2_round_scores": [{"outcome": "ongoing", "score": games[1]}],
            "competitor1_current_round_score": PTS.get(points[0], 40),
            "competitor2_current_round_score": PTS.get(points[1], 40), "advantage": "",
            "competitor1_statistics": {"points_won": 0}, "competitor2_statistics": {"points_won": 0}}


class EdgeHarness:
    def __init__(self, overrides=None):
        base = {"strategies": {k: {"enabled": False} for k in ("DIP", "REVERSAL", "MOMENTUM", "DOWNTREND")},
                "edge": {"anchor_min_samples": 5}}
        if overrides:
            for k, v in overrides.items():
                base[k] = {**base.get(k, {}), **v} if isinstance(v, dict) else v
        self.h = Harness(base, env="PAPER_EDGE",
                         tickers=[market(TA, "Alpha Aaa"), market(TB, "Beta Bbb")])
        e = self.h.engine
        self.edge = EdgeEngine(self.h.cfg, self.h.db, "PAPER_EDGE", "test", e.series, self.h.markets.get,
                               lambda ev: META)
        e.edge, e.strategies = self.edge, []
        self.detector = EventDetector()
        self.state = raw()
        self.t = 0.0
        self.last_ctx = -99.0

    def score(self, **kw):
        self.state = raw(**{"sets": (0, 0), "games": (2, 2), "points": (1, 1), "server": "c1", **kw})

    def tick(self, mid_a: float, dt: float = 2.0):
        now = T0 + self.t
        if self.t - self.last_ctx >= 4.0:
            ctx = normalize_live_data(EVENT, "tennis_tournament_singles", self.state, now)
            evs = self.detector.update(ctx)
            self.edge.on_context(ctx, evs, now)
            self.h.engine.contexts[EVENT] = ctx
            self.last_ctx = self.t
        b = float(int(mid_a - 0.5))
        self.h.step(self.t, {TA: (b, b + 1), TB: (99 - b - 1, 99 - b)})
        self.t += dt

    def run(self, mids, dt: float = 2.0):
        for m in mids:
            self.tick(m, dt)

    def signals(self, **where):
        return self.h.signals(**where)
