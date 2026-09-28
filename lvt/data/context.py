"""Live sports context providers.

Primary: Kalshi's own public live data (GET /milestones + GET
/live_data/{type}/milestone/{id}). It is keyed by Kalshi event tickers, so no
fuzzy matching is needed, and it covers tennis point by point, baseball
pitch by pitch, football, basketball, soccer, hockey and more.

Fallback: MLB StatsAPI (statsapi.mlb.com, the league's own free feed) for
baseball. ESPN and cdn.nba.com both return HTTP 403 from the droplet, so
they are not used.

Context is recorded next to prices so movements can be explained later; the
strategy never trades on an event alone (section 29).
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from lvt.data.http import PublicHttp
from lvt.data.kalshi_public import parse_ts
from lvt.data.provider import EventContext, SportsEventDataProvider

log = logging.getLogger("lvt.context")

LIVE_WORDS = ("inprogress", "in_progress", "live", "set", "quarter", "half", "inning",
              "period", "in progress", "halftime", "break")
FINAL_WORDS = ("closed", "complete", "completed", "finished", "ended", "final", "retired", "walkover")
SCHEDULED_WORDS = ("scheduled", "not_started", "pre", "sch", "time-tbd", "delayed", "postponed")


def normalize_status(*values) -> str:
    for v in values:
        s = str(v or "").strip().lower()
        if not s:
            continue
        if any(w in s for w in SCHEDULED_WORDS):
            return "scheduled"
        if any(w in s for w in FINAL_WORDS):
            return "final"
        if any(w in s for w in LIVE_WORDS):
            return "live"
    return "unknown"


def _sport_of(mtype: str) -> str:
    t = mtype.lower()
    for key, sport in (("tennis", "TENNIS"), ("basketball", "BASKETBALL"), ("football", "FOOTBALL"),
                       ("baseball", "BASEBALL"), ("soccer", "SOCCER"), ("hockey", "HOCKEY"),
                       ("cricket", "CRICKET")):
        if key in t:
            return sport
    return "OTHER"


def normalize_live_data(event_ticker: str, mtype: str, details: dict, fetched: float) -> EventContext:
    sport = _sport_of(mtype)
    d = details or {}
    ctx = EventContext(event_ticker=event_ticker, provider="kalshi_live_data", sport=sport,
                       status=normalize_status(d.get("match_status"), d.get("widget_status"), d.get("status")),
                       fetched_ts=fetched, event_ts=parse_ts(d.get("last_updated_ts")))
    if sport == "TENNIS":
        s1 = [r.get("score") for r in d.get("competitor1_round_scores") or []]
        s2 = [r.get("score") for r in d.get("competitor2_round_scores") or []]
        ctx.score = " ".join(f"{a}-{b}" for a, b in zip(s1, s2))
        ctx.period = str(d.get("match_status") or "")
        ctx.detail = {
            "sets": f"{d.get('competitor1_overall_score', 0)}-{d.get('competitor2_overall_score', 0)}",
            "game_points": f"{d.get('competitor1_current_round_score', '')}-{d.get('competitor2_current_round_score', '')}",
            "server": d.get("server") or d.get("serving") or "",
            "advantage": d.get("advantage") or "",
            # the complete provider payload (points, games, sets, serve stats):
            # the fair-value research needs fields we have not normalised yet
            "raw": d,
        }
    elif sport == "BASEBALL":
        ctx.score = f"{d.get('away_points', '')}-{d.get('home_points', '')}"
        half = "top" if d.get("inning_half") == 1 else "bottom" if d.get("inning_half") == 2 else ""
        ctx.period = f"{half} {d.get('inning', '')}".strip()
        ctx.detail = {"outs": d.get("outs"), "balls": d.get("balls"), "strikes": d.get("strikes"),
                      "bases": d.get("bases"), "last_play": (d.get("last_play") or "")[:120]}
    else:
        ctx.score = f"{d.get('away_points', d.get('away_score', ''))}-{d.get('home_points', d.get('home_score', ''))}"
        ctx.period = str(d.get("period") or d.get("quarter") or d.get("half") or "")
        ctx.clock = str(d.get("clock") or d.get("time_remaining") or d.get("game_clock") or "")
        keep = ("possession", "down", "distance", "yard_line", "field_position", "last_play")
        ctx.detail = {k: d[k] for k in keep if k in d}
    return ctx


class KalshiLiveDataContext(SportsEventDataProvider):
    name = "kalshi_live_data"

    def __init__(self, http: PublicHttp, lookback_days: int = 2):
        self.http = http
        self.lookback_days = lookback_days
        self.index: dict[str, tuple[str, str]] = {}      # event_ticker -> (milestone id, type)
        self.indexed_ts = 0.0

    def refresh_index(self) -> None:
        start = (datetime.now(timezone.utc) - timedelta(days=self.lookback_days)).strftime("%Y-%m-%dT00:00:00Z")
        idx: dict[str, tuple[str, str]] = {}
        groups: dict[str, tuple] = {}
        meta: dict[str, dict] = {}
        cursor = None
        for _ in range(20):
            params = {"limit": 500, "category": "Sports", "minimum_start_date": start}
            if cursor:
                params["cursor"] = cursor
            d = self.http.get("/milestones", params=params)
            for m in d.get("milestones") or []:
                group = tuple(dict.fromkeys((m.get("primary_event_tickers") or []) + (m.get("related_event_tickers") or [])))
                for ev in group:
                    idx.setdefault(ev, (m["id"], m.get("type") or ""))
                    groups.setdefault(ev, group)
                    meta.setdefault(ev, {"milestone_id": m["id"], "type": m.get("type") or "",
                                         "title": m.get("title") or "", "details": m.get("details") or {},
                                         "group": list(group), "start_date": m.get("start_date") or ""})
            cursor = d.get("cursor")
            if not cursor:
                break
        self.index = idx
        self.groups = groups          # event -> every event ticker of the same match/game
        self.meta = meta
        self.indexed_ts = time.time()
        log.info("live-data index: %d event tickers", len(idx))

    def covers(self, event_ticker: str) -> bool:
        return event_ticker in self.index

    groups: dict = {}
    meta: dict = {}

    def context(self, event_ticker: str) -> Optional[EventContext]:
        hit = self.index.get(event_ticker)
        if not hit:
            return None
        mid, mtype = hit
        d = self.http.get(f"/live_data/{mtype}/milestone/{mid}")
        ld = d.get("live_data") or {}
        if not ld:
            return None
        return normalize_live_data(event_ticker, mtype, ld.get("details") or {}, time.time())


_MLB_EVENT = re.compile(r"^KXMLB[A-Z]*-\d{2}[A-Z]{3}\d{2}(?:\d{4})?([A-Z]+)$")


class MlbStatsContext(SportsEventDataProvider):
    """Official MLB StatsAPI. Maps KXMLBGAME-26SEP271510AZSD -> the AZ @ SD
    game by splitting the team code suffix against today's schedule."""
    name = "mlb_statsapi"
    URL = "https://statsapi.mlb.com/api/v1/schedule"

    def __init__(self, http: PublicHttp):
        self.http = http
        self.games: dict[str, dict] = {}       # "AZSD" / "SDAZ" -> game
        self.abbrevs: set[str] = set()

    def refresh_index(self) -> None:
        day = datetime.now(timezone.utc)
        games: dict[str, dict] = {}
        for delta in (-1, 0):
            date = (day + timedelta(days=delta)).strftime("%Y-%m-%d")
            d = self.http.get(self.URL, params={"sportId": 1, "date": date, "hydrate": "linescore,team"},
                              absolute=True)
            for dt in d.get("dates") or []:
                for g in dt.get("games") or []:
                    away = (g.get("teams", {}).get("away", {}).get("team", {}) or {}).get("abbreviation", "")
                    home = (g.get("teams", {}).get("home", {}).get("team", {}) or {}).get("abbreviation", "")
                    if away and home:
                        games[away + home] = g
                        self.abbrevs.update((away, home))
        self.games = games

    def _find(self, event_ticker: str) -> Optional[dict]:
        m = _MLB_EVENT.match(event_ticker or "")
        return self.games.get(m.group(1)) if m else None

    def covers(self, event_ticker: str) -> bool:
        return event_ticker.startswith("KXMLB") and self._find(event_ticker) is not None

    def context(self, event_ticker: str) -> Optional[EventContext]:
        g = self._find(event_ticker)
        if not g:
            return None
        d = self.http.get(f"https://statsapi.mlb.com/api/v1.1/game/{g['gamePk']}/feed/live",
                          absolute=True)
        gd = d.get("gameData", {})
        ls = d.get("liveData", {}).get("linescore", {})
        status = gd.get("status", {}).get("abstractGameState", "")
        away = gd.get("teams", {}).get("away", {}).get("abbreviation", "")
        home = gd.get("teams", {}).get("home", {}).get("abbreviation", "")
        runs = ls.get("teams", {})
        return EventContext(
            event_ticker=event_ticker, provider=self.name, sport="BASEBALL",
            status=normalize_status(status), fetched_ts=time.time(),
            competitors=[away, home],
            score=f"{runs.get('away', {}).get('runs', '')}-{runs.get('home', {}).get('runs', '')}",
            period=f"{ls.get('inningHalf', '')} {ls.get('currentInning', '')}".strip(),
            detail={"outs": ls.get("outs"),
                    "runners": [b for b in ("first", "second", "third") if b in (ls.get("offense") or {})]},
        )
