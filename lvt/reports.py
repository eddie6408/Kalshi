"""Performance reporting (sections 42, 45-47, 51, 61, 67).

Every report is for ONE environment (PAPER, SHADOW, LIVE or a BACKTEST
database) and says so in its header; simulated and real results are never
combined. A report never calls a strategy "profitable" - it prints the
numbers, the sample size and a plain statement of whether the sample is big
enough to mean anything.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict

from lvt.db import Database
from lvt.models import report_bucket

MIN_MEANINGFUL_TRADES = 100


def metrics(rows: list[dict], equity: float = 1000.0) -> dict:
    rows = sorted(rows, key=lambda r: r.get("closed_ts") or 0)
    n = len(rows)
    if n == 0:
        return {"trades": 0}
    net = [r["realized_pnl_usd"] or 0.0 for r in rows]
    wins = [x for x in net if x > 0]
    losses = [x for x in net if x <= 0]
    cum, peak, mdd = 0.0, 0.0, 0.0
    for x in net:
        cum += x
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    streak_w = streak_l = cur_w = cur_l = 0
    for x in net:
        if x > 0:
            cur_w += 1; cur_l = 0
        else:
            cur_l += 1; cur_w = 0
        streak_w, streak_l = max(streak_w, cur_w), max(streak_l, cur_l)
    holds = [(r["closed_ts"] - r["opened_ts"]) for r in rows if r.get("closed_ts") and r.get("opened_ts")]
    span = max((rows[-1].get("closed_ts") or 0) - (rows[0].get("opened_ts") or 0), 1.0)
    capital_time = sum((r.get("avg_entry") or 0) * (r.get("entry_qty") or 0) / 100.0 *
                       ((r.get("closed_ts") or 0) - (r.get("opened_ts") or 0)) for r in rows)
    mean = sum(net) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in net) / (n - 1)) if n > 1 else 0.0
    gl = -sum(losses)
    return {
        "trades": n, "profitable": len(wins), "losing": len(losses),
        "win_rate": round(len(wins) / n, 4),
        "avg_win_usd": round(sum(wins) / len(wins), 4) if wins else 0.0,
        "avg_loss_usd": round(sum(losses) / len(losses), 4) if losses else 0.0,
        "expectancy_usd": round(mean, 4),
        "expectancy_stderr": round(sd / math.sqrt(n), 4) if n > 1 else None,
        "profit_factor": round(sum(wins) / gl, 3) if gl > 0 else None,
        "gross_mid_pnl_usd": round(sum(r.get("gross_mid_pnl_usd") or 0 for r in rows), 2),
        "gross_pnl_usd": round(sum(r.get("gross_pnl_usd") or 0 for r in rows), 2),
        "fees_usd": round(sum((r.get("entry_fees_usd") or 0) + (r.get("exit_fees_usd") or 0) for r in rows), 2),
        "spread_cost_usd": round(sum(r.get("spread_cost_usd") or 0 for r in rows), 2),
        "slippage_usd": round(sum(r.get("slippage_cost_usd") or 0 for r in rows), 2),
        "net_pnl_usd": round(sum(net), 2),
        "max_drawdown_usd": round(mdd, 2),
        "avg_hold_s": round(sum(holds) / len(holds), 1) if holds else None,
        "capital_utilization": round(capital_time / (equity * span), 4),
        "longest_win_streak": streak_w, "longest_loss_streak": streak_l,
        "trades_per_day": round(n / (span / 86400.0), 2),
    }


def verdict(m: dict) -> str:
    n = m.get("trades", 0)
    if n < MIN_MEANINGFUL_TRADES:
        return (f"INSUFFICIENT SAMPLE: {n} trades (< {MIN_MEANINGFUL_TRADES}). "
                "No conclusion about profitability can be drawn.")
    e, se = m["expectancy_usd"], m.get("expectancy_stderr") or 0
    if se and e - 2 * se > 0:
        return f"Net expectancy ${e:.3f}/trade is more than 2 standard errors above zero in this sample."
    if se and e + 2 * se < 0:
        return f"Net expectancy ${e:.3f}/trade is more than 2 standard errors BELOW zero in this sample."
    return f"Net expectancy ${e:.3f}/trade is not distinguishable from zero (+/-{2 * se:.3f})."


def closed(db: Database, env: str, since: float = 0.0) -> list[dict]:
    return db.query("SELECT * FROM positions WHERE env=? AND status='CLOSED' AND closed_ts>=?", (env, since))


def breakdown(rows: list[dict], key, equity: float) -> dict[str, dict]:
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        groups[str(key(r))].append(r)
    return {k: metrics(v, equity) for k, v in sorted(groups.items())}


def full_report(db: Database, env: str, equity: float = 1000.0, since: float = 0.0) -> dict:
    rows = closed(db, env, since)
    overall = metrics(rows, equity)
    return {
        "environment": env,
        "label": {"PAPER": "PAPER (live data, simulated execution)",
                  "SHADOW": "SHADOW (live data, simulated execution via the live order path)",
                  "LIVE": "LIVE (real money)", "BACKTEST": "BACKTEST (replayed data, simulated execution)"}.get(env, env),
        "overall": overall,
        "verdict": verdict(overall),
        "by_strategy": breakdown(rows, lambda r: r["strategy"], equity),
        "by_strategy_version": breakdown(rows, lambda r: r["strategy_version"], equity),
        "by_sport": breakdown(rows, lambda r: report_bucket(r.get("sport") or ""), equity),
        "by_market_type": breakdown(rows, lambda r: r.get("category") or "?", equity),
        "by_regime": breakdown(rows, lambda r: r.get("regime") or "?", equity),
        "by_hour_utc": breakdown(rows, lambda r: f"{(r.get('hour_utc') or 0):02d}", equity),
        "by_exit_reason": breakdown(rows, lambda r: (r.get("exit_reason") or "?").split(":")[0], equity),
    }


def format_metrics(m: dict) -> str:
    if not m.get("trades"):
        return "no closed trades"
    return (f"trades {m['trades']}  win {m['win_rate']:.0%}  exp ${m['expectancy_usd']:+.3f}  "
            f"net ${m['net_pnl_usd']:+.2f}  gross ${m['gross_pnl_usd']:+.2f}  fees ${m['fees_usd']:.2f}  "
            f"slip ${m['slippage_usd']:.2f}  PF {m['profit_factor']}  maxDD ${m['max_drawdown_usd']:.2f}  "
            f"hold {m['avg_hold_s']}s")


def format_report(rep: dict) -> str:
    lines = [f"=== PERFORMANCE - {rep['label']} ===", "", "OVERALL: " + format_metrics(rep["overall"]),
             "VERDICT: " + rep["verdict"], ""]
    for sec in ("by_strategy", "by_strategy_version", "by_sport", "by_market_type", "by_regime",
                "by_exit_reason", "by_hour_utc"):
        lines.append(sec.replace("_", " ").upper())
        for k, m in rep[sec].items():
            lines.append(f"  {k:<32} {format_metrics(m)}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- analysis
def analyze(db: Database, tape: Database | None, env: str) -> dict:
    """Answers to the research questions in section 67, straight from the DB."""
    q = db.query
    out: dict = {"environment": env}
    if tape is not None:
        out["most_moving_markets"] = tape.query(
            "SELECT ticker, MAX(yes_bid) - MIN(yes_bid) AS range_cents, COUNT(*) AS changes FROM quotes "
            "WHERE ts > strftime('%s','now') - 7*86400 GROUP BY ticker ORDER BY range_cents DESC LIMIT 15")
    out["profitable_movements_by_state"] = q(
        "SELECT state, strategy, COUNT(*) n, ROUND(AVG(realized_pnl_usd),4) avg_net, "
        "ROUND(SUM(realized_pnl_usd),2) total FROM positions WHERE env=? AND status='CLOSED' "
        "GROUP BY state, strategy ORDER BY total DESC", (env,))
    out["setup_outcomes_5min"] = q(
        "SELECT s.strategy, s.decision, o.first_hit, COUNT(*) n, ROUND(AVG(o.mid_change),2) avg_mid_change, "
        "ROUND(AVG(o.mfe),2) avg_mfe, ROUND(AVG(o.mae),2) avg_mae FROM signals s JOIN signal_outcomes o "
        "ON o.signal_id=s.id AND o.horizon_s=300 WHERE s.env=? GROUP BY s.strategy, s.decision, o.first_hit "
        "ORDER BY s.strategy, s.decision", (env,))
    out["dips_reversed_vs_collapsed"] = q(
        "SELECT s.strategy, CASE WHEN o.mid_change > 0 THEN 'REVERSED' ELSE 'CONTINUED' END outcome, COUNT(*) n "
        "FROM signals s JOIN signal_outcomes o ON o.signal_id=s.id AND o.horizon_s=900 "
        "WHERE s.env=? AND s.strategy IN ('DIP','REVERSAL') GROUP BY 1,2", (env,))
    out["momentum_continued_vs_reversed"] = q(
        "SELECT s.strategy, CASE WHEN o.mid_change > 0 THEN 'CONTINUED' ELSE 'REVERSED' END outcome, COUNT(*) n "
        "FROM signals s JOIN signal_outcomes o ON o.signal_id=s.id AND o.horizon_s=300 "
        "WHERE s.env=? AND s.strategy IN ('MOMENTUM','DOWNTREND') GROUP BY 1,2", (env,))
    out["eligibility_failures"] = q(
        "SELECT key AS check_name, COUNT(*) n FROM eligibility_log, json_each(eligibility_log.reasons) "
        "WHERE eligible=0 GROUP BY key ORDER BY n DESC")
    out["opportunities_by_sport"] = q(
        "SELECT sport, strategy, COUNT(*) signals, SUM(action='BUY') buy_signals, SUM(decision='TAKEN') taken "
        "FROM signals WHERE env=? GROUP BY sport, strategy ORDER BY signals DESC", (env,))
    out["expectancy_by_strategy"] = q(
        "SELECT strategy_version, COUNT(*) n, ROUND(AVG(realized_pnl_usd),4) expectancy FROM positions "
        "WHERE env=? AND status='CLOSED' GROUP BY strategy_version ORDER BY expectancy DESC", (env,))
    out["opportunities_by_hour_utc"] = q(
        "SELECT hour_utc, COUNT(*) signals, SUM(action='BUY') buys FROM signals WHERE env=? GROUP BY hour_utc", (env,))
    out["latency"] = q(
        "SELECT purpose, COUNT(*) n, ROUND(AVG((signal_ts - data_ts)*1000),0) data_to_signal_ms, "
        "ROUND(AVG((arrival_ts - created_ts)*1000),0) order_to_fill_ms, "
        "ROUND(AVG(CASE WHEN event_ts > 0 THEN (signal_ts - event_ts)*1000 END),0) event_to_signal_ms "
        "FROM orders WHERE env=? GROUP BY purpose", (env,))
    out["slippage"] = q(
        "SELECT purpose, COUNT(*) n, ROUND(AVG(slippage_cents),3) avg_slip_c, ROUND(MAX(slippage_cents),2) max_slip_c "
        "FROM orders WHERE env=? AND filled_count>0 GROUP BY purpose", (env,))
    out["fill_rates"] = q(
        "SELECT purpose, status, COUNT(*) n FROM orders WHERE env=? GROUP BY purpose, status", (env,))
    out["chased_too_late"] = q(
        "SELECT s.strategy, COUNT(*) n, ROUND(AVG(o.mae),2) avg_mae FROM signals s JOIN signal_outcomes o "
        "ON o.signal_id=s.id AND o.horizon_s=60 WHERE s.env=? AND s.decision='TAKEN' AND o.mfe <= 0 "
        "GROUP BY s.strategy", (env,))
    out["rejections"] = q(
        "SELECT strategy, substr(reject_reason,1,40) reason, COUNT(*) n FROM signals WHERE env=? AND "
        "decision='REJECTED' GROUP BY 1,2 ORDER BY n DESC LIMIT 25", (env,))
    return out


def format_analysis(a: dict) -> str:
    lines = [f"=== RESEARCH ANALYSIS - {a['environment']} ==="]
    for k, v in a.items():
        if k == "environment":
            continue
        lines.append("")
        lines.append(k.replace("_", " ").upper())
        if not v:
            lines.append("  (no data yet)")
        for row in v or []:
            lines.append("  " + json.dumps(row, default=str))
    return "\n".join(lines)
