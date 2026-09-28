"""Which live-score requests and which extra markets to fetch each cycle.

Pure functions (no network), so the request budget logic is testable.

Live-score polling (GET /live_data per event) is prioritised:
  * live TENNIS matches        every context.tennis_live_interval_s (point-level)
  * other live events          every context.live_data_interval_s
  * scheduled / unknown        every context.idle_interval_s
  * finished                   not polled again for idle_interval_s * 10
  * events whose live data 404s are backed off for an hour (futures etc.)
Most overdue first, at most max_live_data_requests_per_cycle per cycle.

Companion markets: every market in the same match group (match winner, set
winner, game spread, exact score ...) as a live tennis match on the hot tier.
They are quoted and recorded every cycle for lead-lag and fair-value
research, and never traded (the engine only trades the hot tier).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PollState:
    last_ts: float = 0.0
    status: str = "unknown"        # last normalised status: live | scheduled | final | unknown
    sport: str = ""
    failed_until: float = 0.0


def interval_for(st: PollState, ccfg) -> float:
    if st.status == "live":
        if st.sport == "TENNIS":
            return float(ccfg.get("tennis_live_interval_s", 4.0))
        return float(ccfg.live_data_interval_s)
    idle = float(ccfg.get("idle_interval_s", 120.0))
    if st.status == "final":
        return idle * 10
    return idle


def plan_context_polls(events: list[str], states: dict[str, PollState], now: float, ccfg,
                       covered) -> list[str]:
    """Events to poll this cycle, most overdue first, within the per-cycle cap.
    `covered(event) -> bool` says whether any provider has data for it."""
    due: list[tuple[float, int, str]] = []
    for ev in dict.fromkeys(events):
        st = states.get(ev) or PollState()
        if now < st.failed_until or not covered(ev):
            continue
        overdue = now - st.last_ts - interval_for(st, ccfg)
        if overdue >= 0:
            # live tennis wins ties: point-by-point data is what the research needs
            prio = 0 if (st.status == "live" and st.sport == "TENNIS") else 1
            due.append((prio, -overdue, ev))
    due.sort()
    return [ev for _, _, ev in due[: int(ccfg.max_live_data_requests_per_cycle)]]


def companion_tickers(hot_events: list[str], groups: dict[str, tuple], live_status: dict[str, str],
                      markets_by_event: dict[str, list[str]], hot: set[str], limit: int) -> list[str]:
    """Tickers in the same match group as live hot-tier events, excluding the
    hot tier itself, capped at `limit` (live matches in hot order)."""
    out: list[str] = []
    seen = set(hot)
    for ev in dict.fromkeys(hot_events):
        if live_status.get(ev) != "live":
            continue
        for rel in groups.get(ev, (ev,)):
            for t in markets_by_event.get(rel, []):
                if t not in seen:
                    seen.add(t)
                    out.append(t)
                    if len(out) >= limit:
                        return out
    return out
