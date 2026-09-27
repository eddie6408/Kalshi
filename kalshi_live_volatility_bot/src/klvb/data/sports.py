"""Live sports-event context from Kalshi's own public milestone / live-data feeds.

Kalshi is the settlement authority for its markets, so its milestone feed is the
most authoritative free source for "which game is this market about and is it
live". The payload of `live_data.details` differs per sport; we normalize the
common fields and keep the rest verbatim in `extra`.

Context is informational only. Strategies never turn a score change into an
outcome prediction; the market's own price reaction stays the trading signal.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from ..models import SportsContext
from .base import SportsEventDataProvider
from .parsing import classify_sport, iso_ts

log = logging.getLogger("klvb.data.sports")


def _first(d: dict, *keys: str, default: Any = "") -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def normalize_live_details(details: dict[str, Any]) -> dict[str, Any]:
    """Map the sport-specific live payload to the normalized shape used in the DB/dashboard."""
    score = {}
    for side in ("home", "away"):
        v = _first(details, f"{side}_score", f"{side}_points", f"{side}Score", default=None)
        if v is not None:
            score[side] = v
    for k in ("sets", "games", "points", "set_scores", "game_score"):  # tennis
        if k in details:
            score[k] = details[k]
    return {
        "status": str(_first(details, "status", "game_status", "state")),
        "period": str(_first(details, "period", "quarter", "inning", "set", "half")),
        "clock": str(_first(details, "clock", "time_remaining", "game_clock")),
        "possession": _first(details, "possession", "server", "serving", default=None),
        "competitors": [c for c in (_first(details, "home_team", "home", "player1", default=None),
                                    _first(details, "away_team", "away", "player2", default=None)) if c],
        "score": score,
        "extra": {k: v for k, v in details.items() if k not in score},
    }


class KalshiMilestoneSportsProvider(SportsEventDataProvider):
    name = "kalshi_milestones"

    def __init__(self, client, lookback_hours: float = 8.0):
        self.client = client
        self.lookback_hours = lookback_hours
        self.last_ok_ts: float | None = None
        self.last_error: str | None = None

    def status(self) -> dict[str, Any]:
        return {"name": self.name, "last_ok_ts": self.last_ok_ts, "last_error": self.last_error}

    async def contexts(self) -> dict[str, SportsContext]:
        start = (datetime.now(timezone.utc) - timedelta(hours=self.lookback_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            data = await self.client.get("/milestones", {"category": "Sports", "minimum_start_date": start,
                                                         "limit": 500})
        except Exception as e:  # noqa: BLE001 - context is optional
            self.last_error = str(e)
            return {}
        milestones = data.get("milestones") or []
        now_ = time.time()
        # only games that have started and are not long over
        live = [m for m in milestones
                if (iso_ts(m.get("start_date")) or now_ + 1) <= now_
                and (not m.get("end_date") or (iso_ts(m.get("end_date")) or 0) >= now_ - 1800)]
        by_id = {m["id"]: m for m in live if m.get("id")}
        details: dict[str, dict] = {}
        ids = list(by_id)
        for i in range(0, len(ids), 50):
            try:
                resp = await self.client.get("/live_data/batch", {"milestone_ids": ",".join(ids[i:i + 50])})
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
                continue
            for ld in resp.get("live_datas") or resp.get("live_data") or []:
                details[ld.get("milestone_id", "")] = ld.get("details") or {}
        out: dict[str, SportsContext] = {}
        for mid, m in by_id.items():
            norm = normalize_live_details(details.get(mid, {}))
            sport = classify_sport("", m.get("type", ""), [], m.get("title", ""), "sports")
            for ev in (m.get("primary_event_tickers") or []) + (m.get("related_event_tickers") or []):
                out[ev] = SportsContext(
                    event_ticker=ev, sport=sport, ts=now_, status=norm["status"] or "live",
                    competitors=norm["competitors"], score=norm["score"], period=norm["period"],
                    clock=norm["clock"], extra={"milestone_id": mid, "title": m.get("title"),
                                                "possession": norm["possession"], **norm["extra"]},
                    source=self.name)
        self.last_ok_ts = now_
        return out
