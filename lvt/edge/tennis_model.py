"""Deterministic tennis win-probability model (edge-engine phase 3).

Classic independent-points Markov model. Inputs are the two players'
probabilities of winning a point on their own serve (pa, pb). Everything
else follows exactly from the rules:

    point -> game (deuce closed form) -> tiebreak (first to 7 or 10, win by
    two, serve alternating after the first point then every two) -> set
    (first to 6, win by two, tiebreak at 6-6) -> match (best of 3 or 5)

The server of the first game of the next set is tracked through every path,
so set and match probabilities are exact for the model, not approximated.
Explainable by construction: every reference value is "the probability the
rules give from this score, for these serve strengths".
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

A, B = 0, 1


def _r(x: float) -> float:
    return round(min(max(x, 1e-4), 1 - 1e-4), 4)       # cache key + numerical safety


@lru_cache(maxsize=20_000)   # bounded: implied-strength searches create many keys
def game_win(p: float, i: int, j: int) -> float:
    """P(server wins the game) from server points i, receiver points j
    (0,1,2,3 = 0,15,30,40; >=3-3 is deuce territory)."""
    if i >= 4 and i - j >= 2:
        return 1.0
    if j >= 4 and j - i >= 2:
        return 0.0
    if i >= 3 and j >= 3:
        d = p * p / (p * p + (1 - p) * (1 - p))            # P(win from deuce)
        if i == j:
            return d
        return p + (1 - p) * d if i > j else p * d
    return p * game_win(p, i + 1, j) + (1 - p) * game_win(p, i, j + 1)


@lru_cache(maxsize=20_000)   # bounded: implied-strength searches create many keys
def tiebreak_win(pa: float, pb: float, i: int, j: int, first: int, target: int = 7) -> float:
    """P(A wins the tiebreak) from A points i, B points j; `first` served
    point 1. Point n (0-based) is served by `first` if n % 4 in (0, 3)."""
    if i >= target and i - j >= 2:
        return 1.0
    if j >= target and j - i >= 2:
        return 0.0
    if i >= target - 1 and j >= target - 1 and i == j:
        w = pa * (1 - pb)                                  # A wins the next two (one on each serve)
        l = (1 - pa) * pb
        return w / (w + l)
    n = i + j
    server = first if n % 4 in (0, 3) else 1 - first
    q = pa if server == A else 1 - pb                      # P(A wins this point)
    return q * tiebreak_win(pa, pb, i + 1, j, first, target) + (1 - q) * tiebreak_win(pa, pb, i, j + 1, first, target)


@lru_cache(maxsize=20_000)   # bounded: implied-strength searches create many keys
def set_outcomes(pa: float, pb: float, ga: int, gb: int, server: int, tb_target: int = 7) -> tuple:
    """From games (ga, gb) with `server` serving the next game, returns
    (P(A wins set, A serves next set first), P(A wins, B next),
     P(B wins, A next), P(B wins, B next))."""
    if ga >= 6 and ga - gb >= 2 or ga == 7:
        # the player who served the last game does not serve first next set
        nxt = 1 - (1 - server)                                   # last game server = other of `server`
        return (1.0, 0.0, 0.0, 0.0) if nxt == A else (0.0, 1.0, 0.0, 0.0)
    if gb >= 6 and gb - ga >= 2 or gb == 7:
        nxt = server
        return (0.0, 0.0, 1.0, 0.0) if nxt == A else (0.0, 0.0, 0.0, 1.0)
    if ga == 6 and gb == 6:
        pw = tiebreak_win(pa, pb, 0, 0, server, tb_target)
        nxt = 1 - server                                         # receiver of the first tiebreak point
        if nxt == A:
            return (pw, 0.0, 1 - pw, 0.0)
        return (0.0, pw, 0.0, 1 - pw)
    hold = game_win(pa if server == A else pb, 0, 0)
    a_wins_game = hold if server == A else 1 - hold
    w = set_outcomes(pa, pb, ga + 1, gb, 1 - server, tb_target)
    l = set_outcomes(pa, pb, ga, gb + 1, 1 - server, tb_target)
    return tuple(a_wins_game * x + (1 - a_wins_game) * y for x, y in zip(w, l))


@lru_cache(maxsize=20_000)   # bounded: implied-strength searches create many keys
def match_win(pa: float, pb: float, sa: int, sb: int, server: int, need: int, final_tb: int = 7) -> float:
    """P(A wins the match) at the START of a set with sets (sa, sb)."""
    if sa >= need:
        return 1.0
    if sb >= need:
        return 0.0
    tb = final_tb if (sa == need - 1 and sb == need - 1) else 7
    aa, ab, ba, bb = set_outcomes(pa, pb, 0, 0, server, tb)
    return (aa * match_win(pa, pb, sa + 1, sb, A, need, final_tb) + ab * match_win(pa, pb, sa + 1, sb, B, need, final_tb)
            + ba * match_win(pa, pb, sa, sb + 1, A, need, final_tb) + bb * match_win(pa, pb, sa, sb + 1, B, need, final_tb))


@dataclass(frozen=True)
class TennisState:
    sets: tuple[int, int]            # sets won (A, B)
    games: tuple[int, int]           # games in the current set
    points: tuple[int, int]          # points in the current game (0..3 = 0/15/30/40, 4 = advantage) or tiebreak points
    server: int                      # A or B serving the current game / point
    best_of: int = 3
    in_tiebreak: bool = False
    tb_first: int = A                # who served the first point of the current tiebreak
    final_tb_target: int = 7

    @property
    def need(self) -> int:
        return self.best_of // 2 + 1


def current_game_a(pa: float, pb: float, st: TennisState) -> float:
    """P(A wins the current (non-tiebreak) game)."""
    i, j = st.points
    if st.server == A:
        return game_win(pa, i, j)
    return 1 - game_win(pb, j, i)


def set_from_state(pa: float, pb: float, st: TennisState) -> tuple:
    """(P A wins set & A next, A & B next, B & A next, B & B next) from the live state."""
    pa, pb = _r(pa), _r(pb)
    ga, gb = st.games
    deciding = st.sets[0] == st.need - 1 and st.sets[1] == st.need - 1
    tb_target = st.final_tb_target if deciding else 7
    if st.in_tiebreak:
        pw = tiebreak_win(pa, pb, st.points[0], st.points[1], st.tb_first, tb_target)
        nxt = 1 - st.tb_first
        return (pw, 0.0, 1 - pw, 0.0) if nxt == A else (0.0, pw, 0.0, 1 - pw)
    g = current_game_a(pa, pb, st)
    w = set_outcomes(pa, pb, ga + 1, gb, 1 - st.server, tb_target)
    l = set_outcomes(pa, pb, ga, gb + 1, 1 - st.server, tb_target)
    return tuple(g * x + (1 - g) * y for x, y in zip(w, l))


def p_set(pa: float, pb: float, st: TennisState) -> float:
    return sum(set_from_state(pa, pb, st)[:2])


def p_match(pa: float, pb: float, st: TennisState) -> float:
    """P(A wins the match) from any live state."""
    pa, pb = _r(pa), _r(pb)
    sa, sb = st.sets
    if sa >= st.need:
        return 1.0
    if sb >= st.need:
        return 0.0
    aa, ab, ba, bb = set_from_state(pa, pb, st)
    need, ftb = st.need, st.final_tb_target
    return (aa * match_win(pa, pb, sa + 1, sb, A, need, ftb) + ab * match_win(pa, pb, sa + 1, sb, B, need, ftb)
            + ba * match_win(pa, pb, sa, sb + 1, A, need, ftb) + bb * match_win(pa, pb, sa, sb + 1, B, need, ftb))


def implied_strength(target: float, st: TennisState, base: float, fn=p_match, lo: float = -0.35, hi: float = 0.35) -> float:
    """Serve-strength difference d (pa = base + d/2, pb = base - d/2) at which
    the model price from state `st` equals `target` (bisection; the model is
    monotone in d)."""
    target = min(max(target, 0.005), 0.995)
    for _ in range(40):
        mid = (lo + hi) / 2
        if fn(base + mid / 2, base - mid / 2, st) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def clear_caches() -> None:
    for f in (game_win, tiebreak_win, set_outcomes, match_win):
        f.cache_clear()
