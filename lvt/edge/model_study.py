"""EXP-006: can any tennis-model variant beat the market's own probability?

Offline and read-only. Every reference value is rebuilt from the tape (raw
live-score payloads + quotes) for settled tennis match-winner markets, so
variants that never ran live can be scored on identical data. Future prices
and outcomes are used ONLY to label observations.

Variants (all output P(competitor1 wins the match)):

  V1_600_60      live EDGE_ENGINE_V1 anchor: median market-implied serve
                 strength over [t-600, t-60]
  SHORT_300_10   same, anchor [t-300, t-10]
  LONG_1800_60   same, anchor [t-1800, t-60]
  SERVE_STATS    market-free: in-match serve points won, shrunk toward the
                 tour base with a fixed 40-point prior
  FIXED_EARLY    median implied strength of the first 5 anchor samples of the
                 match, then held fixed

Pre-registered decision (experiment registry EXP-006): pick the variant on
the DEV half (matches ordered by start) using the disagreement set
(|model - market| >= 3c); score it once on the TEST half. KILL if no variant
reaches disagreement-set Brier <= market Brier on dev, or the chosen one
fails on test. Below 300 disagreement samples across 30 matches per split
the verdict is INSUFFICIENT.
"""
from __future__ import annotations

import bisect
import json
import math
import random
import statistics
from collections import defaultdict
from typing import Optional

from lvt.db import Database
from lvt.edge.reference import MATCH_SERIES, TennisMatch, describe, map_player, parse_state
from lvt.edge.tennis_model import A, implied_strength, p_match
from lvt.fees import fee_usd
from lvt.tape import load_event_meta, load_markets

VARIANTS = ("V1_600_60", "SHORT_300_10", "LONG_1800_60", "SERVE_STATS", "FIXED_EARLY")
WINDOWS = {"V1_600_60": (600, 60), "SHORT_300_10": (300, 10), "LONG_1800_60": (1800, 60)}
HORIZONS = (30, 120)


def _clamp(p: float) -> float:
    return min(max(p, 1e-4), 1 - 1e-4)


class _Quotes:
    """P(competitor1) from whichever of the match's two markets is tighter."""

    def __init__(self, tape: Database, legs: dict, t0: float, t1: float):
        self.legs = []
        for player, ticker in legs.items():
            rows = tape.conn.execute("SELECT ts, yes_bid, yes_ask FROM quotes WHERE ticker=? AND ts>=? AND ts<=? "
                                     "ORDER BY ts", (ticker, t0, t1)).fetchall()
            rows = [r for r in rows if r[1] is not None and r[2] is not None and r[2] > r[1] - 1e-9]
            self.legs.append((player, [r[0] for r in rows], rows))

    def at(self, ts: float, max_age: float = 9.0) -> Optional[tuple[float, float]]:
        """(P(c1), spread cents) or None."""
        best = None
        for player, times, rows in self.legs:
            i = bisect.bisect_right(times, ts) - 1
            if i < 0 or ts - times[i] > max_age:
                continue
            _, b, a = rows[i]
            mid = (a + b) / 200.0
            p1 = mid if player == A else 1 - mid
            if best is None or a - b < best[1]:
                best = (p1, a - b)
        return best


def _serve_stats(raw: dict, base: float, prior: float) -> tuple[float, float]:
    def one(k):
        s = raw.get(f"competitor{k}_statistics") or {}
        won, lost = float(s.get("service_points_won") or 0), float(s.get("service_points_lost") or 0)
        return (won + prior * base) / (won + lost + prior)
    return one(1), one(2)


def _match_obs(tape: Database, ev: str, legs: dict, y: float, meta: dict, edge_cfg: dict, start: float, end: float,
               sample_s: float, anchor_s: float, max_spread_anchor: float, min_anchor: int, prior: float,
               max_state_age: float, max_eval_spread: float) -> list[dict]:
    group = list(dict.fromkeys([ev] + list(meta.get("group") or [])))
    rows = tape.conn.execute(f"SELECT ts, summary FROM context WHERE event_ticker IN ({','.join('?' * len(group))}) "
                             "AND ts>=? AND ts<? ORDER BY ts", (*group, start, end)).fetchall()
    details = meta.get("details") or {}
    best_of = int(details.get("best_of") or 3) or 3
    base = TennisMatch._base_serve(details, edge_cfg)
    states = []                                      # (ts, TennisState, raw)
    for ts, summary in rows:
        raw = (json.loads(summary or "{}").get("raw")) or {}
        st = parse_state(raw, best_of)
        states.append((ts, st, raw))
    live = [s for s in states if s[1] is not None]
    if len(live) < 5:
        return []
    t0, t1 = live[0][0], live[-1][0]
    q = _Quotes(tape, legs, t0 - 60, t1 + max(HORIZONS) + 30)
    st_times = [s[0] for s in states]

    def state_at(t):
        i = bisect.bisect_right(st_times, t) - 1
        if i < 0 or t - st_times[i] > max_state_age:
            return None
        return states[i]

    # market-implied serve strengths, sampled like the live engine does
    anchors = []                                     # (ts, d)
    t = t0
    while t <= t1:
        s = state_at(t)
        m = q.at(t)
        if s and s[1] is not None and m and m[1] <= max_spread_anchor:
            anchors.append((t, implied_strength(m[0], s[1], base)))
        t += anchor_s
    a_times = [a[0] for a in anchors]
    early = statistics.median(d for _, d in anchors[:5]) if len(anchors) >= 5 else None
    early_ready = anchors[4][0] if len(anchors) >= 5 else math.inf

    def windowed(t, w, x):
        lo, hi = bisect.bisect_left(a_times, t - w), bisect.bisect_right(a_times, t - x)
        ds = [anchors[i][1] for i in range(lo, hi)]
        return statistics.median(ds) if len(ds) >= min_anchor else None

    out = []
    t = t0
    while t <= t1:
        s, m = state_at(t), q.at(t)
        if s and s[1] is not None and m and m[1] <= max_eval_spread:
            st, raw = s[1], s[2]
            preds = {}
            for name, (w, x) in WINDOWS.items():
                d = windowed(t, w, x)
                if d is not None:
                    preds[name] = p_match(base + d / 2, base - d / 2, st)
            pa, pb = _serve_stats(raw, base, prior)
            preds["SERVE_STATS"] = p_match(pa, pb, st)
            if early is not None and t >= early_ready:
                preds["FIXED_EARLY"] = p_match(base + early / 2, base - early / 2, st)
            fut = {h: (q.at(t + h) or (None,))[0] for h in HORIZONS}
            out.append({"match": ev, "ts": t, "state": describe(st), "market": m[0], "spread": m[1], "y": y,
                        "preds": preds, "fut": fut})
        t += sample_s
    return out


def _brier(ps, ys):
    return sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ps)


def _logloss(ps, ys):
    return -sum(y * math.log(_clamp(p)) + (1 - y) * math.log(1 - _clamp(p)) for p, y in zip(ps, ys)) / len(ps)


def _boot_ci(pts: list[dict], variant: str, n_boot: int = 1000, seed: int = 6) -> tuple[float, float]:
    """95% CI of (model Brier - market Brier), resampling whole MATCHES."""
    by = defaultdict(list)
    for p in pts:
        by[p["match"]].append(((p["preds"][variant] - p["y"]) ** 2, (p["market"] - p["y"]) ** 2))
    keys = sorted(by)
    rng = random.Random(seed)
    diffs = []
    for _ in range(n_boot):
        mm, mk, n = 0.0, 0.0, 0
        for k in (rng.choice(keys) for _ in keys):
            for a, b in by[k]:
                mm, mk, n = mm + a, mk + b, n + 1
        diffs.append((mm - mk) / n)
    diffs.sort()
    return round(diffs[int(0.025 * n_boot)], 4), round(diffs[int(0.975 * n_boot) - 1], 4)


def _score(obs: list[dict], variant: str, disagree: float) -> dict:
    pts = [o for o in obs if variant in o["preds"]]
    if not pts:
        return {"n": 0}
    ys = [o["y"] for o in pts]
    dis = [o for o in pts if abs(o["preds"][variant] - o["market"]) >= disagree]
    out = {"n": len(pts), "matches": len({o["match"] for o in pts}),
           "model_brier": round(_brier([o["preds"][variant] for o in pts], ys), 4),
           "market_brier": round(_brier([o["market"] for o in pts], ys), 4),
           "model_logloss": round(_logloss([o["preds"][variant] for o in pts], ys), 4),
           "market_logloss": round(_logloss([o["market"] for o in pts], ys), 4)}
    d = {"n": len(dis), "matches": len({o["match"] for o in dis})}
    if dis:
        dy = [o["y"] for o in dis]
        d.update({"model_brier": round(_brier([o["preds"][variant] for o in dis], dy), 4),
                  "market_brier": round(_brier([o["market"] for o in dis], dy), 4)})
        d["diff"] = round(d["model_brier"] - d["market_brier"], 4)
        if d["matches"] >= 5:
            d["diff_ci95"] = _boot_ci(dis, variant)
        conv = {}
        for h in HORIZONS:
            per = defaultdict(list)
            for o in dis:
                if o["fut"][h] is not None:
                    sgn = 1 if o["preds"][variant] > o["market"] else -1
                    per[o["match"]].append(100 * sgn * (o["fut"][h] - o["market"]))
            means = [sum(v) / len(v) for v in per.values()]
            if len(means) >= 2:
                mu, sd = sum(means) / len(means), statistics.stdev(means)
                conv[f"+{h}s"] = {"move_toward_model_c": round(mu, 2), "matches": len(means),
                                  "t": round(mu / (sd / math.sqrt(len(means))), 2) if sd else None}
        d["market_move_toward_model"] = conv
    out["disagree"] = d
    return out


def _decide(dev: dict, test: dict, min_n: int, min_m: int) -> dict:
    def enough(s):
        d = s.get("disagree", {})
        return d.get("n", 0) >= min_n and d.get("matches", 0) >= min_m

    cand = {v: s for v, s in dev.items() if enough(s)}
    if not cand:
        return {"verdict": "INSUFFICIENT", "why": f"no variant has >= {min_n} disagreement samples across >= {min_m} "
                                                  "matches on DEV"}
    passing = {v: s for v, s in cand.items() if s["disagree"]["diff"] <= 0}
    if not passing:
        best = min(cand, key=lambda v: cand[v]["disagree"]["diff"])
        return {"verdict": "KILL", "why": f"no variant beats the market on DEV where they disagree (best {best}: "
                                          f"model - market Brier = +{cand[best]['disagree']['diff']})"}
    chosen = min(passing, key=lambda v: passing[v]["disagree"]["diff"])
    ts = test.get(chosen, {})
    if not enough(ts):
        return {"verdict": "INSUFFICIENT", "chosen": chosen,
                "why": f"{chosen} beats the market on DEV; TEST lacks samples - keep collecting"}
    ok = ts["disagree"]["diff"] <= 0
    return {"verdict": "SURVIVES" if ok else "KILL", "chosen": chosen,
            "why": f"{chosen} on TEST: model - market Brier = {ts['disagree']['diff']:+}"}


def _longshots(obs: list[dict]) -> dict:
    """EXP-007 (passive): first time per match either side trades at 10-20c."""
    seen, rows = set(), []
    for o in sorted(obs, key=lambda o: o["ts"]):
        if o["match"] in seen:
            continue
        for p, y in ((o["market"], o["y"]), (1 - o["market"], 1 - o["y"])):
            if 0.10 <= p < 0.20:
                seen.add(o["match"])
                rows.append((p, y, o["spread"]))
                break
    if not rows:
        return {"n": 0}
    n = len(rows)
    rate = sum(y for _, y, _ in rows) / n
    z = 1.96
    c = (rate + z * z / (2 * n)) / (1 + z * z / n)
    h = z * math.sqrt(rate * (1 - rate) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    mean_p = sum(p for p, _, _ in rows) / n
    cost = sum(fee_usd(1, 100 * p) + s / 200.0 for p, _, s in rows) / n    # one taker fee + half spread, held to settlement
    return {"n": n, "mean_price": round(mean_p, 3), "observed_yes_rate": round(rate, 3),
            "wilson95": (round(c - h, 3), round(c + h, 3)), "breakeven_rate": round(mean_p + cost, 3),
            "disproof_threshold_n": 100,
            "status": ("INSUFFICIENT (< 100 matches)" if n < 100 else
                       "NO TRADEABLE BIAS" if c - h <= mean_p + cost else "BIAS SURVIVES COSTS")}


def model_study(tape: Database, start: float, end: float, edge_cfg: dict, sample_s: float = 30.0,
                anchor_s: float = 20.0, disagree_c: float = 3.0, prior_points: float = 40.0,
                min_samples: int = 300, min_matches: int = 30, max_state_age: float = 300.0,
                max_eval_spread: float = 10.0) -> dict:
    markets = load_markets(tape)
    meta = load_event_meta(tape)
    res = {r["ticker"]: r["result"] for r in tape.query("SELECT ticker, result FROM market_results")}
    legs_by_ev: dict[str, dict] = defaultdict(dict)
    unmapped = 0
    for t, mi in markets.items():
        if mi.sport != "TENNIS" or not MATCH_SERIES.search(mi.series_ticker) or res.get(t) not in ("yes", "no"):
            continue
        m = meta.get(mi.event_ticker)
        pl = map_player(t, mi.event_ticker, mi.sub_title or mi.title, (m or {}).get("title") or "") if m else None
        if pl is None:
            unmapped += 1
            continue
        legs_by_ev[mi.event_ticker][pl] = t
    obs, skipped = [], defaultdict(int)
    for ev, legs in sorted(legs_by_ev.items()):
        ys = {1.0 if (res[t] == "yes") == (pl == A) else 0.0 for pl, t in legs.items()}
        if len(ys) != 1:
            skipped["inconsistent results"] += 1
            continue
        got = _match_obs(tape, ev, legs, ys.pop(), meta[ev], edge_cfg, start, end, sample_s, anchor_s,
                         float(edge_cfg.get("anchor_max_spread_cents", 4.0)), int(edge_cfg.get("anchor_min_samples", 5)),
                         prior_points, max_state_age, max_eval_spread)
        if not got:
            skipped["no live score data in window"] += 1
        obs += got
    if not obs:
        return {"observations": 0, "note": "no settled match-winner markets with live score data in the window",
                "skipped": dict(skipped)}
    first = {}
    for o in obs:
        first.setdefault(o["match"], o["ts"])
    order = sorted(first, key=first.get)
    dev_set = set(order[:len(order) // 2])
    dev = [o for o in obs if o["match"] in dev_set]
    test = [o for o in obs if o["match"] not in dev_set]
    dis = disagree_c / 100.0
    sc = {"DEV": {v: _score(dev, v, dis) for v in VARIANTS}, "TEST": {v: _score(test, v, dis) for v in VARIANTS},
          "ALL": {v: _score(obs, v, dis) for v in VARIANTS}}
    return {
        "experiment": "EXP-006", "observations": len(obs), "matches": len(order),
        "dev_matches": len(dev_set), "test_matches": len(order) - len(dev_set),
        "split_at": first[order[len(order) // 2]] if len(order) > 1 else None,
        "unmapped_markets": unmapped, "skipped": dict(skipped),
        "settings": {"sample_s": sample_s, "anchor_s": anchor_s, "disagree_c": disagree_c,
                     "serve_prior_points": prior_points, "max_eval_spread_c": max_eval_spread,
                     "max_state_age_s": max_state_age, "floor": f">= {min_samples} disagreement samples across "
                                                               f">= {min_matches} matches per split"},
        "decision": _decide(sc["DEV"], sc["TEST"], min_samples, min_matches),
        "scores": sc,
        "longshots_EXP007": _longshots(obs),
        "note": "Brier/log loss are for P(competitor1 wins); market = tighter of the two match-winner mids. "
                "Observations every sample_s are correlated within a match: the CI resamples whole matches.",
    }


def format_model_study(rep: dict) -> str:
    if not rep.get("observations"):
        return f"=== STUDY: model (EXP-006) ===\n{json.dumps(rep, indent=1, default=str)}"
    L = ["=== STUDY: model (EXP-006) ===",
         f"observations {rep['observations']}  matches {rep['matches']} (dev {rep['dev_matches']} / test "
         f"{rep['test_matches']})  unmapped markets {rep['unmapped_markets']}  skipped {rep['skipped']}",
         f"floor: {rep['settings']['floor']}", ""]
    hdr = (f"{'split':<5} {'variant':<13} {'n':>6} {'mtch':>5} {'brier m/mkt':>14} | {'dis n':>6} {'mtch':>5} "
           f"{'brier m/mkt':>14} {'diff':>8} {'95% CI':>18} {'mv+30s':>7} {'mv+120s':>8}")
    L.append(hdr)
    for split in ("DEV", "TEST", "ALL"):
        for v in VARIANTS:
            s = rep["scores"][split][v]
            if not s.get("n"):
                L.append(f"{split:<5} {v:<13} {'-':>6}")
                continue
            d = s["disagree"]
            mv = d.get("market_move_toward_model", {})
            L.append(f"{split:<5} {v:<13} {s['n']:>6} {s['matches']:>5} {s['model_brier']:>6}/{s['market_brier']:<7} | "
                     f"{d['n']:>6} {d['matches']:>5} "
                     + (f"{d['model_brier']:>6}/{d['market_brier']:<7} {d['diff']:>+8.4f} {str(d.get('diff_ci95', '')):>18} "
                        f"{str(mv.get('+30s', {}).get('move_toward_model_c', '')):>7} "
                        f"{str(mv.get('+120s', {}).get('move_toward_model_c', '')):>8}" if d["n"] else ""))
        L.append("")
    dec = rep["decision"]
    L.append(f"DECISION (pre-registered): {dec['verdict']} - {dec['why']}")
    L.append(f"EXP-007 longshots: {json.dumps(rep['longshots_EXP007'])}")
    return "\n".join(L)
