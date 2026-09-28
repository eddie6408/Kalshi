"""Sport-event timeline (edge-engine brief, phase 1: sections 32, 33, 57).

Live-score snapshots are polled, so an "event" is the difference between two
consecutive snapshots of the same match. This module turns those
differences into timestamped events:

    TENNIS   POINT_WON, GAME_WON (HOLD / BREAK), SET_WON, STATUS
    others   SCORE_CHANGE, STATUS

Every event carries two clocks: when WE received the snapshot and, when the
provider supplies one, the provider's own update time. The market side
already has exchange timestamps (trade prints) and receive timestamps
(quotes), so the timeline can answer what happened, when, and when the
market reacted.

Hold vs break is inferred from the server-side statistics in Kalshi's
payload (a game won together with an increase in that player's
service_games_won is a hold; otherwise a break). If a single poll spans
several points or games, the counts are recorded and the event is flagged
MULTI - the resolution limit of polling is never hidden.

Data-quality flags (section 57): OUT_OF_ORDER (provider time went
backwards), DUPLICATE (identical state), LAGGED (receive - provider time
above a threshold), NO_PROVIDER_TS, GAP (a poll skipped at least one point).
Consumers must not treat flagged events as clean event timing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from lvt.data.provider import EventContext

LAG_FLAG_S = 10.0


@dataclass
class SportEvent:
    event_ticker: str
    sport: str
    kind: str
    actor: str                        # "1" / "2" (tennis competitor), "" if n/a
    ts_received: float
    ts_provider: float
    detail: dict = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)

    def row(self) -> tuple:
        import json
        return (self.ts_received, self.ts_provider or None, self.event_ticker, self.sport, self.kind,
                self.actor, json.dumps(self.detail, default=str), ",".join(self.flags))


def _i(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def tennis_state(raw: dict) -> dict:
    s1 = (raw.get("competitor1_statistics") or {})
    s2 = (raw.get("competitor2_statistics") or {})
    games1 = sum(_i(r.get("score")) for r in raw.get("competitor1_round_scores") or [])
    games2 = sum(_i(r.get("score")) for r in raw.get("competitor2_round_scores") or [])
    srv = raw.get("server")
    return {
        "server_idx": 0 if srv and srv == raw.get("competitor1_id") else 1 if srv and srv == raw.get("competitor2_id") else None,
        "status": str(raw.get("match_status") or ""),
        "sets": (_i(raw.get("competitor1_overall_score")), _i(raw.get("competitor2_overall_score"))),
        "games": (games1, games2),
        "points": (str(raw.get("competitor1_current_round_score", "")), str(raw.get("competitor2_current_round_score", ""))),
        "advantage": str(raw.get("advantage") or ""),
        "points_won": (_i(s1.get("points_won")), _i(s2.get("points_won"))),
        "service_games_won": (_i(s1.get("service_games_won")), _i(s2.get("service_games_won"))),
        "breakpoints_won": (_i(s1.get("breakpoints_won")), _i(s2.get("breakpoints_won"))),
    }


class EventDetector:
    def __init__(self, lag_flag_s: float = LAG_FLAG_S):
        self.last: dict[str, tuple[dict, EventContext]] = {}
        self.lag_flag_s = lag_flag_s

    def update(self, ctx: EventContext) -> list[SportEvent]:
        raw = (ctx.detail or {}).get("raw") if ctx.sport == "TENNIS" else None
        state = tennis_state(raw) if raw else {"status": ctx.status, "score": ctx.score, "period": ctx.period}
        prev = self.last.get(ctx.event_ticker)
        self.last[ctx.event_ticker] = (state, ctx)
        if prev is None:
            return []
        pstate, pctx = prev
        flags: list[str] = []
        if not ctx.event_ts:
            flags.append("NO_PROVIDER_TS")
        else:
            if pctx.event_ts and ctx.event_ts < pctx.event_ts:
                flags.append("OUT_OF_ORDER")
            if ctx.fetched_ts - ctx.event_ts > self.lag_flag_s:
                flags.append("LAGGED")
        if state == pstate:
            return []                                  # nothing happened (DUPLICATE polls are not events)

        def ev(kind, actor="", **detail):
            return SportEvent(ctx.event_ticker, ctx.sport, kind, actor, ctx.fetched_ts, ctx.event_ts,
                              {"before": _brief(pstate), "after": _brief(state), **detail}, list(flags))

        out: list[SportEvent] = []
        if state.get("status") != pstate.get("status"):
            out.append(ev("STATUS", status_from=pstate.get("status"), status_to=state.get("status")))
        if raw:
            out += self._tennis(pstate, state, ev)
        elif state.get("score") != pstate.get("score"):
            out.append(ev("SCORE_CHANGE", score_from=pstate.get("score"), score_to=state.get("score")))
        return out

    @staticmethod
    def _tennis(a: dict, b: dict, ev) -> list[SportEvent]:
        out: list[SportEvent] = []
        for k, actor in ((0, "1"), (1, "2")):
            dsets = b["sets"][k] - a["sets"][k]
            dgames = b["games"][k] - a["games"][k]
            dpts = b["points_won"][k] - a["points_won"][k]
            dhold = b["service_games_won"][k] - a["service_games_won"][k]
            dbreak = b["breakpoints_won"][k] - a["breakpoints_won"][k]
            if dsets > 0:
                out.append(ev("SET_WON", actor, count=dsets))
            if dgames > 0:
                kind = ("HOLD" if dhold > 0 and dbreak <= 0 else
                        "BREAK" if dbreak > 0 and dhold <= 0 else "GAME")    # GAME = stats can't tell
                if kind == "GAME" and dgames == 1 and a.get("server_idx") is not None:
                    kind = "HOLD" if a["server_idx"] == k else "BREAK"      # who served the game that just ended
                e = ev("GAME_WON", actor, game_type=kind, count=dgames, holds=dhold, breaks=dbreak)
                if dgames > 1:
                    e.flags.append("MULTI")
                out.append(e)
            if dpts > 0:
                e = ev("POINT_WON", actor, count=dpts)
                if dpts > 1:
                    e.flags.append("GAP")
                out.append(e)
        return out


def _brief(s: dict) -> dict:
    return {k: v for k, v in s.items() if k in ("status", "sets", "games", "points", "advantage", "score", "period")}

