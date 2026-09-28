"""STRATEGY VALIDATION REPORT (V2 brief sections 45, 47, 76, 77).

Scores the brief's definition of a PROVISIONALLY VALIDATED edge from the
studies and live paper data. Every criterion is PASS, FAIL or INSUFFICIENT
(not enough evidence to judge). The recommendation is "READY FOR HUMAN
REVIEW" only when all ten pass; the report never changes any mode - LIVE
still needs the separate live gate (lvt readiness / authorize-live).
"""
from __future__ import annotations

import json
import time

from lvt.db import Database
from lvt.edge import studies
from lvt.edge.ab import ab_report

PASS, FAIL, INSUFF = "PASS", "FAIL", "INSUFFICIENT"


def _crit(name: str, status: str, evidence: str) -> dict:
    return {"criterion": name, "status": status, "evidence": evidence}


def validation_report(cfg, db: Database, tape: Database, env: str = "PAPER_EDGE") -> dict:
    start = db.scalar("SELECT MIN(ts) FROM dislocations WHERE env=?", (env,), None) or time.time() - 86400
    end = time.time()
    cal = studies.calibration_study(db, tape, start, end, env)
    gap = studies.gap_study(db, tape, start, end, env)
    dec = studies.decay_study(db, tape, start, end, env)
    err = studies.error_study(db, start, end, env)
    lat = studies.latency_study(db, start, end, env)
    ab = ab_report(db, "PAPER", env)
    ch = ab.get("challenger", {}) if ab.get("window") else {}
    crits = []

    d = cal.get("where_they_disagree_by_3c_plus", {}) if cal.get("observations") else {}
    if d.get("n", 0) >= 200:
        crits.append(_crit("1 model calibration", PASS if d["model_brier"] <= d["market_brier"] else FAIL,
                           f"where model and market disagree by >=3c (n={d['n']}): model Brier {d['model_brier']} vs market {d['market_brier']}"))
    else:
        crits.append(_crit("1 model calibration", INSUFF, f"{d.get('n', 0)} settled observations where model and market disagree (need 200)"))

    v = gap.get("verdict", "INSUFFICIENT DATA")
    crits.append(_crit("2 dislocation predicts movement", PASS if v.startswith("GAP PREDICTS") else INSUFF if "INSUFFICIENT" in v else FAIL, v))

    ci = ch.get("expectancy_ci95")
    n = ch.get("trades", 0)
    if n >= 30 and ci:
        crits.append(_crit("3 survives realistic costs", PASS if ci[0] > 0 else FAIL,
                           f"live paper net expectancy ${ch['expectancy_usd']:+.3f}/trade, 95% CI {ci} over {n} trades"))
    else:
        crits.append(_crit("3 survives realistic costs", INSUFF, f"{n} closed challenger trades (need 30)"))

    crits.append(_crit("4 execution assumptions supported by observed fills", INSUFF,
                       "paper fills are modelled; needs SHADOW/LIVE fills to calibrate the maker queue model"))

    oos = db.one("SELECT * FROM experiments WHERE strategy_version LIKE 'EDGE_ENGINE%' AND status='ACCEPTED' "
                 "AND test_period != '' ORDER BY updated_ts DESC LIMIT 1")
    crits.append(_crit("5 out-of-sample test", PASS if oos else INSUFF,
                       f"accepted experiment {oos['experiment_id']}" if oos else "no accepted out-of-sample experiment for this engine"))

    matches = db.scalar("SELECT COUNT(DISTINCT event_ticker) FROM positions WHERE env=? AND status='CLOSED'", (env,), 0)
    crits.append(_crit("6 many independent events", PASS if matches >= 50 else INSUFF, f"{matches} distinct matches traded (need 50)"))

    regimes = {k: m for k, m in (ab.get("challenger_by_trade_type") or {}).items() if m.get("trades", 0) >= 30}
    good = [k for k, m in regimes.items() if (m.get("expectancy_ci95") or [0])[0] > 0]
    crits.append(_crit("7 survives multiple regimes/setups", PASS if len(good) >= 2 else INSUFF if len(regimes) < 2 else FAIL,
                       f"setups with >=30 trades: {list(regimes)}; significantly positive: {good}"))

    bt = db.one("SELECT metrics FROM backtest_runs WHERE kind='baseline_study' AND segment='edge' ORDER BY ts DESC LIMIT 1")
    if bt and n >= 30:
        be = json.loads(bt["metrics"]).get("expectancy_usd", 0)
        agree = ci and ci[0] <= be <= ci[1]
        crits.append(_crit("8 live paper agrees with replay", PASS if agree else FAIL,
                           f"replay expectancy ${be:+.3f} vs live paper CI {ci}"))
    else:
        crits.append(_crit("8 live paper agrees with replay", INSUFF, "needs a replay of the same window and 30+ live trades"))

    crits.append(_crit("9 edge positive after measured latency", crits[2]["status"],
                       "live paper results already include measured polling latency and simulated order latency"))

    ev = db.one("SELECT * FROM readiness_evidence WHERE item='RESTART_RECOVERY_TESTED' AND passed=1 ORDER BY ts DESC LIMIT 1")
    crits.append(_crit("10 no look-ahead / selection bias found", PASS if ev else INSUFF,
                       "adversarial replay test + deterministic replay in the test suite (lvt selftest)" if ev else "run lvt selftest"))

    ready = all(c["status"] == PASS for c in crits)
    fails = [c["criterion"] for c in crits if c["status"] == FAIL]
    return {
        "strategy": env, "generated": end, "data_window": [start, end],
        "criteria": crits,
        "calibration": {k: cal.get(k) for k in ("observations", "markets", "matches", "where_they_disagree_by_3c_plus")},
        "model_vs_market": {"model": (cal.get("model") or {}).get("brier"), "market": (cal.get("market") or {}).get("brier")},
        "gap_study_verdict": v, "edge_half_life_s": dec.get("median_half_life_s"),
        "live_paper": {"challenger": ch, "comparison": ab.get("comparison_vs_baseline_tennis")},
        "errors": {k: err.get(k) for k in ("false_positive_rate", "false_negative_rate (watched/rejected setups)")},
        "latency": lat,
        "known_failure_modes": (ab.get("diagnostics") or {}).get(env, {}),
        "recommendation": ("READY FOR HUMAN REVIEW (controlled production still needs the live gate)" if ready else
                           f"NOT READY - failing: {fails}" if fails else "NOT READY - insufficient evidence"),
    }


def format_validation(rep: dict) -> str:
    lines = [f"=== STRATEGY VALIDATION REPORT: {rep['strategy']} ===",
             f"generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(rep['generated']))}; data from "
             f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(rep['data_window'][0]))}", ""]
    for c in rep["criteria"]:
        lines.append(f"  [{c['status']:<12}] {c['criterion']:<52} {c['evidence']}")
    lines += ["", f"CALIBRATION: {json.dumps(rep['calibration'], default=str)}",
              f"MODEL vs MARKET Brier: {rep['model_vs_market']}",
              f"GAP STUDY: {rep['gap_study_verdict']}   EDGE HALF-LIFE: {rep['edge_half_life_s']}s",
              f"LIVE PAPER: {json.dumps(rep['live_paper'], default=str)[:600]}",
              f"ERRORS: {rep['errors']}",
              f"KNOWN FAILURE MODES: {rep['known_failure_modes']}", "",
              f"RECOMMENDATION: {rep['recommendation']}"]
    return "\n".join(lines)
