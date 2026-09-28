"""BASELINE_V1 vs EDGE_ENGINE_V1 (brief sections 42-47, 61, 63).

Both run on the same live data in separate paper accounts (env PAPER and
PAPER_EDGE). The comparison window starts when the challenger first
traded. Because the edge engine only trades tennis, the baseline is shown
both overall and restricted to tennis (the like-for-like comparison).

Confidence intervals: expectancy +/- 1.96 standard errors, win rate by the
Wilson interval, and the difference in expectancies by Welch's standard
error. The verdict only says "better" or "worse" when the 95% interval of
the difference excludes zero - never on point estimates alone.
"""
from __future__ import annotations

import json
import math
import time
from collections import defaultdict

from lvt.db import Database
from lvt.reports import metrics

MIN_TRADES = 30


def _ci(rows) -> dict:
    m = metrics(rows)
    n = m.get("trades", 0)
    if n:
        se = m.get("expectancy_stderr") or 0.0
        m["expectancy_ci95"] = [round(m["expectancy_usd"] - 1.96 * se, 4), round(m["expectancy_usd"] + 1.96 * se, 4)]
        w, z = m["win_rate"], 1.96
        den = 1 + z * z / n
        c = (w + z * z / (2 * n)) / den
        h = z * math.sqrt(w * (1 - w) / n + z * z / (4 * n * n)) / den
        m["win_rate_ci95"] = [round(c - h, 4), round(c + h, 4)]
    return m


def _diff(a: dict, b: dict) -> dict:
    if a.get("trades", 0) < 2 or b.get("trades", 0) < 2:
        return {"verdict": "INSUFFICIENT DATA"}
    d = b["expectancy_usd"] - a["expectancy_usd"]
    se = math.sqrt((a.get("expectancy_stderr") or 0) ** 2 + (b.get("expectancy_stderr") or 0) ** 2)
    lo, hi = d - 1.96 * se, d + 1.96 * se
    small = min(a["trades"], b["trades"]) < MIN_TRADES
    if small:
        verdict = f"INSUFFICIENT SAMPLE (< {MIN_TRADES} trades on one side) - no conclusion"
    elif lo > 0:
        verdict = "CHALLENGER BETTER (95% CI of the difference excludes zero)"
    elif hi < 0:
        verdict = "CHALLENGER WORSE (95% CI of the difference excludes zero)"
    else:
        verdict = "NOT DISTINGUISHABLE"
    return {"expectancy_diff_usd": round(d, 4), "diff_ci95": [round(lo, 4), round(hi, 4)], "verdict": verdict}


def _group(rows, key) -> dict:
    g = defaultdict(list)
    for r in rows:
        g[str(key(r))].append(r)
    return {k: _ci(v) for k, v in sorted(g.items())}


def _bucket(x, edges, labels):
    if x is None:
        return "unknown"
    for e, l in zip(edges, labels):
        if x < e:
            return l
    return labels[-1]


def ab_report(db: Database, base_env: str = "PAPER", chal_env: str = "PAPER_EDGE", since: float | None = None) -> dict:
    start = since or db.scalar("SELECT MIN(opened_ts) FROM positions WHERE env=?", (chal_env,), None)
    if start is None:
        return {"status": f"{chal_env} has no trades yet", "window": None}
    q = "SELECT * FROM positions WHERE env=? AND status='CLOSED' AND opened_ts>=?"
    base = db.query(q, (base_env, start))
    base_tennis = [r for r in base if r.get("sport") == "TENNIS"]
    chal = db.query(q, (chal_env, start))
    sig = {r["id"]: r for r in db.query("SELECT id, explanation, strategy FROM signals WHERE env=? AND ts>=?", (chal_env, start))}

    def expl(p):
        s = sig.get(p.get("signal_id"))
        try:
            return json.loads(s["explanation"]) if s else {}
        except (TypeError, ValueError):
            return {}

    held = lambda r: _bucket((r["closed_ts"] or 0) - (r["opened_ts"] or 0), [30, 120, 600], ["<30s", "30-120s", "2-10min", ">10min"])
    rep = {
        "window": [start, time.time()],
        "baseline_all": _ci(base), "baseline_tennis": _ci(base_tennis), "challenger": _ci(chal),
        "comparison_vs_baseline_tennis": _diff(_ci(base_tennis), _ci(chal)),
        "primary_experiment": {
            "baseline_dip_expectancy": _ci([r for r in base_tennis if r["strategy"] in ("DIP", "REVERSAL")]),
            "enhanced_dip_expectancy": _ci([r for r in chal if r["strategy"] in ("EDGE_DISLOCATION_REVERSAL", "EDGE_FLOW_REVERSAL")]),
            "baseline_momentum_expectancy": _ci([r for r in base_tennis if r["strategy"] in ("MOMENTUM", "DOWNTREND")]),
            "enhanced_momentum_expectancy": _ci([r for r in chal if r["strategy"] == "EDGE_UNFINISHED_MOMENTUM"]),
        },
        "challenger_by_trade_type": _group(chal, lambda r: r["strategy"]),
        "challenger_by_event": _group(chal, lambda r: (expl(r).get("event") or "none").split("(")[0]),
        "challenger_by_dislocation": _group(chal, lambda r: _bucket(
            (expl(r).get("reference") or 0) - (expl(r).get("current") or 0), [3, 5, 8], ["<3c", "3-5c", "5-8c", ">=8c"])),
        "challenger_by_holding_period": _group(chal, held),
        "baseline_by_holding_period": _group(base_tennis, held),
        "by_sport": {"baseline": _group(base, lambda r: r.get("sport")), "challenger": _group(chal, lambda r: r.get("sport"))},
        "by_regime": {"baseline": _group(base, lambda r: r.get("regime") or "?")},
    }
    rej = db.query("SELECT decision, reject_reason, strategy, text FROM signals WHERE env=? AND ts>=? AND decision IN "
                   "('NO_EDGE','WATCH','REJECTED')", (chal_env, start))
    filt = defaultdict(int)
    for r in rej:
        t = (r["text"] or "").lower()
        key = ("event filter (justified repricing)" if "event_repricing" in (r["strategy"] or "").lower() else
               "confirmation pending (WATCH)" if r["decision"] == "WATCH" else
               "cost filter" if "does not cover costs" in t else
               "execution filter (trade quality)" if "trade quality" in t else
               "dislocation filter" if "dislocation" in t else
               "confidence filter" if "confidence" in t else
               "correlation (already exposed)" if "already exposed" in t else
               "risk" if r["decision"] == "REJECTED" else "other")
        filt[key] += 1
    rep["filtering"] = dict(filt)
    cf = db.query("SELECT c.hyp_pnl_cents FROM counterfactuals c JOIN signals s ON s.id=c.signal_id WHERE s.env=? "
                  "AND s.ts>=? AND c.hyp_pnl_cents IS NOT NULL", (chal_env, start))
    rep["counterfactual"] = {"label": "COUNTERFACTUAL / MISSED OPPORTUNITY - not P&L",
                             "rejected_would_have_won": sum(1 for r in cf if r["hyp_pnl_cents"] > 0),
                             "rejected_would_have_lost": sum(1 for r in cf if r["hyp_pnl_cents"] <= 0)}
    rep["diagnostics"] = {env: {f"{r['outcome']}:{r['category']}": r["n"] for r in db.query(
        "SELECT d.outcome, d.category, COUNT(*) n FROM trade_diagnostics d JOIN positions p ON p.id=d.position_id "
        "WHERE d.env=? AND p.opened_ts>=? GROUP BY 1,2 ORDER BY n DESC", (env, start))} for env in (base_env, chal_env)}
    rep["event_responses"] = db.query(
        "SELECT kinds, severity, response, COUNT(*) n, ROUND(AVG(expected_impact),2) expected, ROUND(AVG(actual_60s),2) actual_60s "
        "FROM event_responses WHERE ts>=? AND labeled_ts IS NOT NULL GROUP BY 1,2,3 ORDER BY n DESC LIMIT 30", (start,))
    return rep


def fmt(m: dict) -> str:
    if not m.get("trades"):
        return "no closed trades"
    ci = m.get("expectancy_ci95", [None, None])
    return (f"n={m['trades']:<4} exp ${m['expectancy_usd']:+.3f} [95% {ci[0]:+.3f}, {ci[1]:+.3f}]  win {m['win_rate']:.0%}  "
            f"PF {m['profit_factor']}  net ${m['net_pnl_usd']:+.2f}  fees ${m['fees_usd']:.2f}  slip ${m['slippage_usd']:.2f}  "
            f"maxDD ${m['max_drawdown_usd']:.2f}  hold {m['avg_hold_s']}s")


def format_ab(rep: dict) -> str:
    if not rep.get("window"):
        return rep.get("status", "no data")
    L = ["=== A/B: BASELINE_V1 (PAPER) vs EDGE_ENGINE_V1 (PAPER_EDGE) - paper, simulated execution ===",
         f"window: {time.strftime('%Y-%m-%d %H:%M', time.gmtime(rep['window'][0]))} -> now (UTC)", "",
         "BASELINE (all sports)     " + fmt(rep["baseline_all"]),
         "BASELINE (tennis only)    " + fmt(rep["baseline_tennis"]),
         "EDGE_ENGINE_V1            " + fmt(rep["challenger"]),
         "DIFFERENCE vs tennis baseline: " + json.dumps(rep["comparison_vs_baseline_tennis"]), "",
         "PRIMARY EXPERIMENT"]
    for k, v in rep["primary_experiment"].items():
        L.append(f"  {k:<32} {fmt(v)}")
    for sec in ("challenger_by_trade_type", "challenger_by_event", "challenger_by_dislocation",
                "challenger_by_holding_period", "baseline_by_holding_period"):
        L += ["", sec.replace("_", " ").upper()]
        for k, v in rep[sec].items():
            L.append(f"  {k:<32} {fmt(v)}")
    L += ["", "FILTERING (challenger signals not traded)"] + [f"  {k:<40} {v}" for k, v in rep["filtering"].items()]
    L += ["", "COUNTERFACTUAL (not P&L): " + json.dumps(rep["counterfactual"])]
    L += ["", "WHY TRADES WON / LOST"] + [f"  {env}: {json.dumps(d)}" for env, d in rep["diagnostics"].items()]
    L += ["", "EVENT RESPONSES (expected vs actual 60s move)"] + [f"  {json.dumps(r)}" for r in rep["event_responses"]]
    return "\n".join(L)
