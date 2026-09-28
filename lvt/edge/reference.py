"""fair_value_engine - short-horizon reference values (edge-engine phase 3).

For TENNIS match-winner and current-set-winner markets:

  reference = tennis_model(live score state | serve strengths)

Serve strengths come from the MARKET, not from us: every ~20 s while the
match is live we solve for the strength difference d that makes the model
reproduce the match-winner price at that moment's score. The reference uses
the median d over the last 10 minutes EXCLUDING the most recent 60 s. So:

  * information the market has priced for minutes (form, injury, conditions)
    is absorbed into d;
  * a sudden move in the last minute is NOT absorbed - the reference only
    moves with the score, which is exactly what makes "observed move vs
    justified move" measurable.

Range = model at the 25th/75th percentile of those d samples; confidence
falls with a wide range, few samples, stale score data or an unconfirmed
player-to-market mapping. No model, stale data or no mapping -> no
reference (callers must answer NO_EDGE, never guess).
"""
from __future__ import annotations

import re
import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from lvt.edge.tennis_model import A, B, TennisState, implied_strength, p_match, p_set

POINTS = {0: 0, 15: 1, 30: 2, 40: 3, 50: 3}
MATCH_SERIES = re.compile(r"MATCH$")
SET_SERIES = re.compile(r"SETWINNER$")
DATE_CODE = re.compile(r"^\d{2}[A-Z]{3}\d{2}(?:\d{4})?")


def _i(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def parse_state(raw: dict, best_of: int = 3) -> Optional[TennisState]:
    """Live tennis state from Kalshi's live_data payload, or None if it is not
    a live, fully specified state (then there is no reference)."""
    if not raw or str(raw.get("status") or raw.get("widget_status") or "") not in ("live", "started"):
        return None
    c1, c2 = raw.get("competitor1_id"), raw.get("competitor2_id")
    srv = raw.get("server")
    if not srv or srv not in (c1, c2):
        return None
    server = A if srv == c1 else B
    sets = (_i(raw.get("competitor1_overall_score")), _i(raw.get("competitor2_overall_score")))
    r1, r2 = raw.get("competitor1_round_scores") or [], raw.get("competitor2_round_scores") or []
    games = (0, 0)
    for x, y in zip(r1, r2):
        if x.get("outcome") == "ongoing" or y.get("outcome") == "ongoing":
            games = (_i(x.get("score")), _i(y.get("score")))
    in_tb = games == (6, 6)
    p1, p2 = _i(raw.get("competitor1_current_round_score")), _i(raw.get("competitor2_current_round_score"))
    tb_first = server
    if in_tb:
        points = (p1, p2)
        n = p1 + p2
        tb_first = server if n % 4 in (0, 3) else 1 - server
    else:
        adv = raw.get("advantage") or ""
        if adv:
            points = (4, 3) if adv == c1 else (3, 4)
        else:
            if p1 not in POINTS or p2 not in POINTS:
                return None
            points = (POINTS[p1], POINTS[p2])
    ftb = 10 if "10" in str(raw.get("last_set_scoring_type") or "") else 7
    return TennisState(sets, games, points, server, best_of, in_tb, tb_first, ftb)


def describe(st: TennisState) -> str:
    names = {0: "0", 1: "15", 2: "30", 3: "40", 4: "AD"}
    pts = f"{st.points[0]}-{st.points[1]}" if st.in_tiebreak else f"{names.get(st.points[0], st.points[0])}-{names.get(st.points[1], st.points[1])}"
    return (f"sets {st.sets[0]}-{st.sets[1]}, games {st.games[0]}-{st.games[1]}, "
            f"{'tiebreak ' if st.in_tiebreak else ''}points {pts}, P{st.server + 1} serving")


def event_code(event_ticker: str) -> tuple[str, int]:
    """KXATPMATCH-26SEP28DHADUR -> ('DHADUR', 0); KXATPSETWINNER-26SEP28KECMAN-2 -> ('KECMAN', 2)."""
    parts = event_ticker.split("-")
    if len(parts) < 2:
        return "", 0
    set_no = _i(parts[2]) if len(parts) > 2 else 0
    return DATE_CODE.sub("", parts[1]), set_no


def map_player(ticker: str, event_ticker: str, sub_title: str, match_title: str) -> Optional[int]:
    """Which competitor (A = competitor1, B = competitor2) a market's YES is.
    Kalshi event codes are the first letters of both surnames in title order
    and titles list competitor1 first; both checks must agree when present."""
    code, _ = event_code(event_ticker)
    suffix = ticker.rsplit("-", 1)[-1]
    by_code = None
    if code and suffix and code != suffix:
        s, e = code.startswith(suffix), code.endswith(suffix)
        by_code = A if s and not e else B if e and not s else None
    by_name = None
    if " vs " in match_title and sub_title:
        first, second = [x.strip().lower() for x in match_title.split(" vs ", 1)]
        st = sub_title.lower()
        in1, in2 = bool(first) and first in st, bool(second) and second in st
        by_name = A if in1 and not in2 else B if in2 and not in1 else None
    if by_code is not None and by_name is not None and by_code != by_name:
        return None
    return by_code if by_code is not None else by_name


BASE_SERVE_DEFAULT = {"ATP": 0.64, "ATP Challenger": 0.62, "ITF_M": 0.61, "WTA": 0.56, "WTA Challenger": 0.555,
                      "ITF_W": 0.55, "men": 0.63, "women": 0.555}


@dataclass
class Reference:
    value: float                   # YES cents
    lo: float
    hi: float
    confidence: float
    kind: str                      # MATCH | SET
    player: int
    d: float
    pa: float
    pb: float
    samples: int
    state_text: str
    context_age_s: float
    mapping: str                   # CONFIRMED | PROVISIONAL
    reasons: list[str] = field(default_factory=list)

    def side(self, side: str) -> tuple[float, float, float]:
        if side == "yes":
            return self.value, self.lo, self.hi
        return 100.0 - self.value, 100.0 - self.hi, 100.0 - self.lo


class TennisMatch:
    """Everything the reference engine knows about one live match."""

    def __init__(self, event_ticker: str, meta: dict, ecfg):
        self.event_ticker = event_ticker
        self.meta = meta or {}
        details = self.meta.get("details") or {}
        self.title = self.meta.get("title") or ""
        self.best_of = _i(details.get("best_of"), 3) or 3
        self.base = self._base_serve(details, ecfg)
        self.state: Optional[TennisState] = None
        self.state_ts = 0.0
        self.samples: deque = deque(maxlen=200)          # (ts, d)
        self.last_sample_ts = 0.0
        self.mapping_votes: dict[str, list[int]] = {}    # ticker -> [agree, disagree]

    @staticmethod
    def _base_serve(details: dict, ecfg) -> float:
        table = dict(BASE_SERVE_DEFAULT)
        table.update(ecfg.get("base_serve", {}) if hasattr(ecfg, "get") else {})
        tour = str(details.get("tour") or "")
        gender = str(details.get("gender") or "")
        if tour.startswith("ITF"):
            return table["ITF_W"] if gender == "women" else table["ITF_M"]
        if tour in table:
            return table[tour]
        return table["women"] if gender == "women" else table["men"]

    def pa_pb(self, d: float) -> tuple[float, float]:
        return self.base + d / 2, self.base - d / 2

    def anchor(self, now: float, ecfg) -> Optional[tuple[float, float, float, int]]:
        """(d, d_lo, d_hi, n) from samples in [now-window, now-exclude]."""
        lo_t = now - float(ecfg.anchor_window_s)
        hi_t = now - float(ecfg.anchor_exclude_s)
        ds = sorted(d for t, d in self.samples if lo_t <= t <= hi_t)
        if len(ds) < int(ecfg.anchor_min_samples):
            return None
        q = statistics.quantiles(ds, n=4) if len(ds) >= 4 else [ds[0], statistics.median(ds), ds[-1]]
        return statistics.median(ds), q[0], q[-1], len(ds)


class ReferenceEngine:
    def __init__(self, ecfg):
        self.ecfg = ecfg
        self.matches: dict[str, TennisMatch] = {}        # match event -> state
        self.market_match: dict[str, tuple[str, str, int, int]] = {}   # ticker -> (match event, kind, player, set_no)

    # -- registration -----------------------------------------------------------------
    def register_market(self, info, meta: Optional[dict]) -> bool:
        """Attach a market to its match if it is a modelled type. Returns True if modelled."""
        if info.ticker in self.market_match:
            return True
        if info.sport != "TENNIS" or not meta:
            return False
        series = info.series_ticker
        kind = "MATCH" if MATCH_SERIES.search(series) else "SET" if SET_SERIES.search(series) else ""
        if not kind:
            return False
        group = meta.get("group") or [info.event_ticker]
        match_ev = next((g for g in group if MATCH_SERIES.search(g.split("-")[0])), info.event_ticker)
        player = map_player(info.ticker, info.event_ticker, info.sub_title or info.title, meta.get("title") or "")
        if player is None:
            return False
        _, set_no = event_code(info.event_ticker)
        self.market_match[info.ticker] = (match_ev, kind, player, set_no)
        if match_ev not in self.matches:
            self.matches[match_ev] = TennisMatch(match_ev, meta, self.ecfg)
        return True

    def match_for_event(self, event_ticker: str) -> Optional[TennisMatch]:
        return self.matches.get(event_ticker)

    # -- live updates ---------------------------------------------------------------
    def on_context(self, ctx, meta: Optional[dict]) -> Optional[TennisMatch]:
        if ctx.sport != "TENNIS":
            return None
        m = self.matches.get(ctx.event_ticker)
        if m is None and meta:
            m = self.matches[ctx.event_ticker] = TennisMatch(ctx.event_ticker, meta, self.ecfg)
        if m is None:
            return None
        raw = (ctx.detail or {}).get("raw") or {}
        m.state = parse_state(raw, m.best_of)
        m.state_ts = ctx.fetched_ts
        return m

    def sample_anchor(self, match_ticker: str, mid_yes: float, spread: float, now: float) -> None:
        """Implied strength from a match-winner market's mid at the current score."""
        hit = self.market_match.get(match_ticker)
        if not hit or hit[1] != "MATCH":
            return
        m = self.matches.get(hit[0])
        if m is None or m.state is None or now - m.state_ts > float(self.ecfg.context_stale_s):
            return
        if spread is None or spread > float(self.ecfg.anchor_max_spread_cents) or now - m.last_sample_ts < float(self.ecfg.anchor_sample_s):
            return
        target = mid_yes / 100.0 if hit[2] == A else 1 - mid_yes / 100.0     # always "P(competitor1)"
        m.samples.append((now, implied_strength(target, m.state, m.base)))
        m.last_sample_ts = now

    def note_response(self, ticker: str, expected: float, actual: float) -> None:
        """Mapping confirmation: a scored point should move the mapped market
        the way the model says. Votes only on clear, material events."""
        if abs(expected) < 1.0 or abs(actual) < 0.5:
            return
        hit = self.market_match.get(ticker)
        m = self.matches.get(hit[0]) if hit else None
        if m is None:
            return
        v = m.mapping_votes.setdefault(ticker, [0, 0])
        v[0 if expected * actual > 0 else 1] += 1

    def mapping_status(self, ticker: str) -> str:
        hit = self.market_match.get(ticker)
        m = self.matches.get(hit[0]) if hit else None
        agree, disagree = (m.mapping_votes.get(ticker, [0, 0]) if m else [0, 0])
        n = agree + disagree
        if n >= int(self.ecfg.mapping_min_events):
            return "CONFIRMED" if agree / n >= float(self.ecfg.mapping_agree) else "CONTRADICTED"
        return "PROVISIONAL"

    # -- reference ------------------------------------------------------------------
    def reference(self, ticker: str, now: float, state: Optional[TennisState] = None) -> tuple[Optional[Reference], str]:
        """(reference or None, reason). `state` overrides the live state (used
        to price an event's before/after)."""
        hit = self.market_match.get(ticker)
        if not hit:
            return None, "no model for this market"
        match_ev, kind, player, set_no = hit
        m = self.matches.get(match_ev)
        if m is None:
            return None, "no match data"
        st = state or m.state
        if st is None:
            return None, "match not live or score incomplete"
        age = now - m.state_ts
        if state is None and age > float(self.ecfg.context_stale_s):
            return None, f"score data {age:.0f}s old - event-aware signal not trusted"
        mapping = self.mapping_status(ticker)
        if mapping == "CONTRADICTED":
            return None, "player-to-market mapping contradicted by price reactions"
        anc = m.anchor(now, self.ecfg)
        if anc is None:
            return None, "not enough market history to anchor serve strengths"
        d, dlo, dhi, n = anc
        if kind == "SET":
            if set_no != st.sets[0] + st.sets[1] + 1:
                return None, f"set {set_no} is not the current set"
            fn = p_set
        else:
            fn = p_match

        def val(dd):
            pa, pb = m.pa_pb(dd)
            p1 = fn(pa, pb, st)
            return 100.0 * (p1 if player == A else 1 - p1)

        v, a, b = val(d), val(dlo), val(dhi)
        lo, hi = min(a, b, v), max(a, b, v)
        width = hi - lo
        conf = max(0.0, 1.0 - width / float(self.ecfg.max_range_cents))
        conf *= min(1.0, n / (2.0 * float(self.ecfg.anchor_min_samples)))
        conf *= max(0.0, 1.0 - max(age, 0.0) / (2.0 * float(self.ecfg.context_stale_s))) if state is None else 1.0
        if mapping == "PROVISIONAL":
            conf *= float(self.ecfg.provisional_mapping_factor)
        pa, pb = m.pa_pb(d)
        return Reference(round(v, 3), round(lo, 3), round(hi, 3), round(conf, 3), kind, player, round(d, 4),
                         round(pa, 4), round(pb, 4), n, describe(st), round(age, 1), mapping), "ok"
