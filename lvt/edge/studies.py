"""Empirical studies for EDGE_ENGINE V2 (V2 brief sections 8-10, 27, 39, 80, 81).

Read-only: they measure, they never trade or change parameters. Future
prices are looked up from the tape ONLY to label past observations; nothing
here feeds a live decision.

Statistical hygiene:
* consecutive observations of one match are strongly correlated, so every
  study thins to one observation per match per `sample_s` and reports the
  number of independent matches next to every count;
* results are reported per bucket with a t-statistic; buckets with too few
  matches are flagged, not hidden;
* the resolution limit is stated: quotes are polled every ~2 s (recorded on
  change + 6 s keepalive) and scores every ~4 s, so horizons shorter than
  that are not measurable and are not reported.
"""
from __future__ import annotations

import bisect
import math
import statistics
from collections import defaultdict

from lvt.db import Database
from lvt.fees import fee_usd

HORIZONS = (5, 10, 30, 60, 120, 300)
GAP_BUCKETS = ((0.25, 1), (1, 2), (2, 3), (3, 5), (5, 8), (8, 100))
MIN_MATCHES = 5


class QuoteIndex:
    """Tape quotes for a set of tickers, queried as 'last quote at or before t'."""

    def __init__(self, tape: Database, tickers, t0: float, t1: float):
        self.ts: dict[str, list[float]] = defaultdict(list)
        self.q: dict[str, list[tuple[float, float]]] = defaultdict(list)
        tickers = list(set(tickers))
        for i in range(0, len(tickers), 400):
            chunk = tickers[i:i + 400]
            rows = tape.conn.execute(
                f"SELECT ticker, ts, yes_bid, yes_ask FROM quotes WHERE ticker IN ({','.join('?' * len(chunk))}) "
                "AND ts>=? AND ts<=? ORDER BY ts", (*chunk, t0, t1)).fetchall()
            for t, ts, b, a in rows:
                if b is None or a is None:
                    continue
                self.ts[t].append(ts)
                self.q[t].append((b, a))

    def at(self, ticker: str, ts: float, max_age: float = 9.0):
        arr = self.ts.get(ticker)
        if not arr:
            return None
        i = bisect.bisect_right(arr, ts) - 1
        if i < 0 or ts - arr[i] > max_age:
            return None                               # the market was not being watched then
        b, a = self.q[ticker][i]
        return b, a, (b + a) / 2.0


def _thin(rows, sample_s: float):
    """One row per match per sample_s bin (first seen), newest code first."""
    seen, out = set(), []
    for r in rows:
        key = (r["match_event"] or r["ticker"], int(r["ts"] // sample_s))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _stats(vals):
    n = len(vals)
    if n == 0:
        return None, None, None
    m = sum(vals) / n
    sd = statistics.stdev(vals) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n > 1 else None
    return m, se, (m / se if se else None)


def _taker_roundtrip(g: float, now_q, later_q) -> float:
    """Cents per contract: take the side the gap favours now, sell it at the
    later bid, both taker fees."""
    b0, a0, _ = now_q
    b1, a1, _ = later_q
    if g > 0:                                          # buy YES at ask, sell YES at later bid
        entry, exit_ = a0, b1
    else:                                              # buy NO at 100-bid, sell NO at 100-later ask
        entry, exit_ = 100 - b0, 100 - a1
    return exit_ - entry - 100 * (fee_usd(1, entry) + fee_usd(1, max(exit_, 1)))


def gap_study(db: Database, tape: Database, start: float, end: float, env: str = "PAPER_EDGE",
              min_conf: float = 0.0, sample_s: float = 30.0) -> dict:
    """V2 brief section 81: does the fair-value gap predict the market's next move?"""
    rows = db.query("SELECT * FROM dislocations WHERE env=? AND ts>=? AND ts<? AND confidence>=? ORDER BY ts",
                    (env, start, end, min_conf))
    obs = _thin(rows, sample_s)
    if not obs:
        return {"observations": 0, "note": "no dislocation rows in the window"}
    qi = QuoteIndex(tape, [r["ticker"] for r in obs], start - 10, end + max(HORIZONS) + 10)
    cells = defaultdict(lambda: {"move": [], "toward": 0, "taker": [], "matches": set()})
    for r in obs:
        g = r["gap"]
        if g is None or abs(g) < GAP_BUCKETS[0][0]:
            continue
        now_q = qi.at(r["ticker"], r["ts"])
        if now_q is None:
            continue
        bucket = next((f"{lo}-{hi}c" for lo, hi in GAP_BUCKETS if lo <= abs(g) < hi), None)
        for h in HORIZONS:
            later = qi.at(r["ticker"], r["ts"] + h)
            if later is None:
                continue
            move = (later[2] - now_q[2]) * (1 if g > 0 else -1)       # + = toward fair value
            c = cells[(bucket, h)]
            c["move"].append(move)
            c["toward"] += move > 0
            c["taker"].append(_taker_roundtrip(g, now_q, later))
            c["matches"].add(r["match_event"] or r["ticker"])
    table = []
    order = {f"{lo}-{hi}c": i for i, (lo, hi) in enumerate(GAP_BUCKETS)}
    for (bucket, h), c in sorted(cells.items(), key=lambda kv: (order[kv[0][0]], kv[0][1])):
        m, se, t = _stats(c["move"])
        tm, tse, tt = _stats(c["taker"])
        table.append({"gap": bucket, "horizon_s": h, "n": len(c["move"]), "matches": len(c["matches"]),
                      "mean_move_toward_fair_c": round(m, 3), "t_stat": None if t is None else round(t, 2),
                      "share_toward": round(c["toward"] / len(c["move"]), 3),
                      "taker_roundtrip_c": round(tm, 3), "taker_t": None if tt is None else round(tt, 2),
                      "reliable": len(c["matches"]) >= MIN_MATCHES})
    big = [x for x in table if x["gap"] in ("3-5c", "5-8c", "8-100c") and x["reliable"]]
    predicts = any(x["mean_move_toward_fair_c"] > 0 and (x["t_stat"] or 0) > 2 for x in big)
    executable = any(x["taker_roundtrip_c"] > 0 and (x["taker_t"] or 0) > 2 for x in big)
    verdict = ("INSUFFICIENT DATA" if not big else
               "GAP PREDICTS MOVEMENT AND SURVIVES TAKER COSTS" if predicts and executable else
               "GAP PREDICTS MOVEMENT BUT NOT AFTER TAKER COSTS" if predicts else
               "NO EVIDENCE THE GAP PREDICTS MOVEMENT")
    return {"observations": len(obs), "independent_matches": len({r["match_event"] for r in obs}),
            "window": [start, end], "sample_s": sample_s, "table": table, "verdict": verdict,
            "note": "move = mid change toward fair value; taker = buy the favoured side at the ask now, "
                    "sell at the bid later, taker fees both ways (maker costs are lower but fills uncertain)"}


def decay_study(db: Database, tape: Database, start: float, end: float, env: str = "PAPER_EDGE",
                min_gap: float = 3.0) -> dict:
    """V2 brief sections 10 and 69: how fast does a detected gap close?"""
    rows = db.query("SELECT * FROM dislocations WHERE env=? AND ts>=? AND ts<? AND ABS(gap)>=? ORDER BY ts",
                    (env, start, end, min_gap))
    starts = _thin(rows, 60.0)
    fair_rows = defaultdict(list)
    for r in db.query("SELECT ticker, ts, fair FROM dislocations WHERE env=? AND ts>=? AND ts<? ORDER BY ts",
                      (env, start, end + 120)):
        fair_rows[r["ticker"]].append((r["ts"], r["fair"]))
    qi = QuoteIndex(tape, [r["ticker"] for r in starts], start - 10, end + 130)
    curve = defaultdict(list)
    for r in starts:
        g0 = r["gap"]
        fr = fair_rows[r["ticker"]]
        fts = [x[0] for x in fr]
        for h in (2, 5, 10, 20, 30, 60, 120):
            q = qi.at(r["ticker"], r["ts"] + h)
            i = bisect.bisect_right(fts, r["ts"] + h) - 1
            if q is None or i < 0:
                continue
            gh = fr[i][1] - q[2]
            curve[h].append(gh / g0)                       # 1 = gap unchanged, 0 = closed, <0 = overshot
    out = []
    for h in sorted(curve):
        v = curve[h]
        out.append({"after_s": h, "n": len(v), "median_remaining": round(statistics.median(v), 3),
                    "mean_remaining": round(sum(v) / len(v), 3),
                    "share_half_closed": round(sum(1 for x in v if x <= 0.5) / len(v), 3)})
    half = next((x["after_s"] for x in out if x["median_remaining"] <= 0.5), None)
    return {"signals": len(starts), "min_gap_c": min_gap, "curve": out,
            "median_half_life_s": half,
            "resolution_note": "quotes ~2 s, scores ~4 s: horizons under 2 s are not measurable"}


def calibration_study(db: Database, tape: Database, start: float, end: float, env: str = "PAPER_EDGE",
                      sample_s: float = 60.0) -> dict:
    """V2 brief sections 8 and 27: model vs market against settled outcomes."""
    res = {r["ticker"]: r["result"] for r in tape.query("SELECT ticker, result FROM market_results")}
    rows = [r for r in db.query("SELECT * FROM dislocations WHERE env=? AND ts>=? AND ts<? ORDER BY ts", (env, start, end))
            if res.get(r["ticker"]) in ("yes", "no") and r["fair"] is not None and r["mid"] is not None]
    obs = []
    seen = set()
    for r in rows:                                         # one per MARKET per bin (outcomes are per market)
        k = (r["ticker"], int(r["ts"] // sample_s))
        if k not in seen:
            seen.add(k)
            obs.append(r)
    if not obs:
        return {"observations": 0, "note": "no settled outcomes for recorded dislocations yet"}

    def score(pairs):
        n = len(pairs)
        brier = sum((p - y) ** 2 for p, y in pairs) / n
        ll = -sum(y * math.log(min(max(p, 1e-4), 1 - 1e-4)) + (1 - y) * math.log(1 - min(max(p, 1e-4), 1 - 1e-4))
                  for p, y in pairs) / n
        buckets = defaultdict(list)
        for p, y in pairs:
            buckets[min(int(p * 10), 9)].append((p, y))
        rel = [{"bucket": f"{b / 10:.1f}-{(b + 1) / 10:.1f}", "n": len(v), "mean_pred": round(sum(p for p, _ in v) / len(v), 3),
                "observed": round(sum(y for _, y in v) / len(v), 3)} for b, v in sorted(buckets.items())]
        ece = sum(len(v) * abs(sum(p for p, _ in v) / len(v) - sum(y for _, y in v) / len(v)) for v in buckets.values()) / n
        return {"n": n, "brier": round(brier, 4), "log_loss": round(ll, 4), "calibration_error": round(ece, 4),
                "reliability": rel}

    def seg(pred):
        grp = defaultdict(lambda: ([], []))
        for r in obs:
            y = 1.0 if res[r["ticker"]] == "yes" else 0.0
            st = r["state"] or ""
            key = pred(r, st)
            grp[key][0].append((r["fair"] / 100, y))
            grp[key][1].append((r["mid"] / 100, y))
        return {k: {"model": score(m), "market": score(mk)} for k, (m, mk) in grp.items() if len(m) >= 10}

    y_all = [(1.0 if res[r["ticker"]] == "yes" else 0.0) for r in obs]
    model = score([(r["fair"] / 100, y) for r, y in zip(obs, y_all)])
    market = score([(r["mid"] / 100, y) for r, y in zip(obs, y_all)])
    disl = [(r, y) for r, y in zip(obs, y_all) if abs(r["gap"] or 0) >= 3]
    return {
        "observations": len(obs), "markets": len({r["ticker"] for r in obs}),
        "matches": len({r["match_event"] for r in obs}), "model": model, "market": market,
        "where_they_disagree_by_3c_plus": ({"n": len(disl),
                                            "model_brier": round(sum((r["fair"] / 100 - y) ** 2 for r, y in disl) / len(disl), 4),
                                            "market_brier": round(sum((r["mid"] / 100 - y) ** 2 for r, y in disl) / len(disl), 4)}
                                           if disl else {"n": 0}),
        "by_segment": {
            "match_phase": seg(lambda r, st: "early (no set finished)" if st.startswith("sets 0-0") else "later"),
            "tiebreak": seg(lambda r, st: "tiebreak" if "tiebreak" in st else "regular"),
            "since_event": seg(lambda r, st: "<30s after event" if (r["since_event_s"] or 1e9) < 30 else "no recent event"),
            "confidence": seg(lambda r, st: "high (>=0.5)" if (r["confidence"] or 0) >= 0.5 else "low"),
        },
        "note": "the model is anchored to the market's own price, so overall scores will be close; the "
                "informative comparison is where they disagree",
    }


def funnel_study(db: Database, tape: Database, start: float, end: float, env: str = "PAPER_EDGE") -> dict:
    """V2 brief section 39: where are opportunities lost?"""
    q1 = lambda sql, a=(): db.scalar(sql, a, 0)
    tennis = [r["ticker"] for r in tape.query("SELECT ticker FROM markets WHERE sport='TENNIS'")]
    moves = 0
    if tennis:
        qi = QuoteIndex(tape, tennis, start, end)
        for t, ts in qi.ts.items():
            last_t, last_m = None, None
            for i, x in enumerate(ts):
                m = sum(qi.q[t][i]) / 2
                if last_m is not None and abs(m - last_m) >= 3 and x - last_t <= 60:
                    moves += 1
                    last_t, last_m = x, m
                elif last_m is None or x - last_t > 60:
                    last_t, last_m = x, m
    stages = [
        ("market moves >=3c in <=60s (tennis)", moves),
        ("score events detected", tape.scalar("SELECT COUNT(*) FROM sport_events WHERE ts_received>=? AND ts_received<?", (start, end), 0)),
        ("score events with clean timing flags", tape.scalar("SELECT COUNT(*) FROM sport_events WHERE ts_received>=? AND ts_received<? "
                                                             "AND (flags='' OR flags='NO_PROVIDER_TS')", (start, end), 0)),
        ("fair-value dislocations >=3c (observations)", q1("SELECT COUNT(*) FROM dislocations WHERE env=? AND ts>=? AND ts<? AND ABS(gap)>=3", (env, start, end))),
        ("tradeable setups (WATCH + BUY signals)", q1("SELECT COUNT(*) FROM signals WHERE env=? AND ts>=? AND ts<? AND action IN ('WATCH','BUY')", (env, start, end))),
        ("executable (BUY signals)", q1("SELECT COUNT(*) FROM signals WHERE env=? AND ts>=? AND ts<? AND action='BUY'", (env, start, end))),
        ("orders placed", q1("SELECT COUNT(*) FROM orders WHERE env=? AND purpose='ENTRY' AND created_ts>=? AND created_ts<?", (env, start, end))),
        ("trades (entries filled)", q1("SELECT COUNT(*) FROM positions WHERE env=? AND entry_qty>0 AND opened_ts>=? AND opened_ts<?", (env, start, end))),
        ("profitable trades", q1("SELECT COUNT(*) FROM positions WHERE env=? AND status='CLOSED' AND realized_pnl_usd>0 AND opened_ts>=? AND opened_ts<?", (env, start, end))),
    ]
    out, prev = [], None
    for name, n in stages:
        out.append({"stage": name, "count": n, "conversion": None if not prev else round(n / prev, 3)})
        prev = n or prev
    no_edge = db.query("SELECT substr(text, instr(text,'REASON: ')+8, 60) r, COUNT(*) n FROM signals WHERE env=? AND ts>=? AND ts<? "
                       "AND action='NO_EDGE' GROUP BY 1 ORDER BY 2 DESC LIMIT 12", (env, start, end))
    return {"funnel": out, "top_no_edge_reasons": no_edge}


def _breakdown(rows, key) -> dict:
    from lvt.reports import metrics
    g = defaultdict(list)
    for r in rows:
        g[str(key(r))].append(r)
    return {k: {x: m.get(x) for x in ("trades", "expectancy_usd", "win_rate", "net_pnl_usd", "fees_usd")}
            for k, v in sorted(g.items()) for m in [metrics(v)]}


def _engine_summary(db: Database, env: str, start: float, end: float) -> dict:
    from lvt.reports import metrics
    rows = db.query("SELECT * FROM positions WHERE env=? AND status='CLOSED' AND opened_ts>=? AND opened_ts<?", (env, start, end))
    m = metrics(rows)
    mids = sorted(r["realized_pnl_usd"] for r in rows)
    orders = db.query("SELECT status FROM orders WHERE env=? AND purpose='ENTRY' AND created_ts>=? AND created_ts<?", (env, start, end))
    sigs = db.query("SELECT decision FROM signals WHERE env=? AND ts>=? AND ts<?", (env, start, end))
    filled = sum(1 for o in orders if o["status"] in ("FILLED", "PARTIAL"))
    price = lambda r: next(l for lo, l in ((15, "<15c"), (30, "15-30c"), (70, "30-70c"), (85, "70-85c"), (101, ">85c")) if (r["avg_entry"] or 0) < lo)
    return {
        "summary": {**{k: m.get(k) for k in ("trades", "gross_pnl_usd", "net_pnl_usd", "fees_usd", "expectancy_usd",
                                             "win_rate", "max_drawdown_usd", "profit_factor")},
                    "median_trade_usd": mids[len(mids) // 2] if mids else None,
                    "mean_mfe_c": round(sum(r["mfe_cents"] or 0 for r in rows) / len(rows), 2) if rows else None,
                    "mean_mae_c": round(sum(r["mae_cents"] or 0 for r in rows) / len(rows), 2) if rows else None,
                    "entry_fill_rate": round(filled / len(orders), 3) if orders else None,
                    "signal_rejection_rate": round(sum(1 for s in sigs if s["decision"] != "TAKEN") / len(sigs), 3) if sigs else None},
        "by_sport": _breakdown(rows, lambda r: r.get("sport")),
        "by_strategy": _breakdown(rows, lambda r: r.get("strategy")),
        "by_regime": _breakdown(rows, lambda r: r.get("regime") or "?"),
        "by_price_range": _breakdown(rows, price),
        "by_market": _breakdown(rows, lambda r: (r.get("ticker") or "").split("-")[0]),
    }


def baseline_study(cfg, db: Database, tape: Database, start: float, end: float, out_dir) -> dict:
    """V2 brief section 80: BASELINE_V1 and EDGE_ENGINE_V1 on identical data -
    replayed from the tape AND as traded live in paper over the same window."""
    from lvt.replay import run_replay
    b = run_replay(cfg, tape, start, end, out_dir, "baseline_study", "baseline", db, label=False)
    e = run_replay(cfg, tape, start, end, out_dir, "baseline_study", "edge", db, label=False, edge=True)
    bdb, edb = Database(b["db_path"], readonly=True), Database(e["db_path"], readonly=True)
    return {"window": [start, end],
            "replay_BASELINE_V1": _engine_summary(bdb, "BACKTEST", start, end),
            "replay_EDGE_ENGINE_V1": _engine_summary(edb, "BACKTEST", start, end),
            "live_paper_BASELINE_V1": _engine_summary(db, "PAPER", start, end),
            "live_paper_EDGE_ENGINE_V1": _engine_summary(db, "PAPER_EDGE", start, end),
            "note": "EDGE_ENGINE_V1 can only be replayed where dense scores exist (from 2026-09-28 11:54 UTC)"}


def format_study(name: str, rep: dict) -> str:
    import json
    lines = [f"=== STUDY: {name} ==="]
    for k, v in rep.items():
        if isinstance(v, list):
            lines.append(f"{k}:")
            lines += ["  " + json.dumps(x, default=str) for x in v]
        elif isinstance(v, dict):
            lines.append(f"{k}: " + json.dumps(v, default=str)[:3000])
        else:
            lines.append(f"{k}: {v}")
    return "\n".join(lines)
