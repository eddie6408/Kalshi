"""Performance statistics. Every report is scoped to ONE environment (PAPER, SHADOW,
LIVE or BACKTEST); simulated and real results are never combined.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable


def _streaks(nets: list[float]) -> tuple[int, int]:
    best_w = best_l = cur_w = cur_l = 0
    for n in nets:
        if n > 0:
            cur_w, cur_l = cur_w + 1, 0
        else:
            cur_l, cur_w = cur_l + 1, 0
        best_w, best_l = max(best_w, cur_w), max(best_l, cur_l)
    return best_w, best_l


def summarize(trades: Iterable[dict[str, Any]], starting_equity: float | None = None) -> dict[str, Any]:
    ts = sorted(trades, key=lambda t: t["closed_ts"] or 0)
    n = len(ts)
    if n == 0:
        return {"total_trades": 0}
    nets = [t["net_pnl"] / 100 for t in ts]            # dollars
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    gross = sum(t["gross_pnl"] for t in ts) / 100
    fees = sum(t["fees"] for t in ts) / 100
    slip = sum(t["slippage"] or 0 for t in ts) / 100
    net = sum(nets)
    cum = peak = mdd = 0.0
    for x in nets:
        cum += x
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    contracts = sum(t["qty"] for t in ts)
    span_days = max(((ts[-1]["closed_ts"] or 0) - (ts[0]["opened_ts"] or 0)) / 86400, 1e-9)
    best_w, best_l = _streaks(nets)
    out = {
        "total_trades": n, "profitable_trades": len(wins), "losing_trades": len(losses),
        "win_rate": round(len(wins) / n, 4),
        "avg_win": round(sum(wins) / len(wins), 4) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 4) if losses else 0.0,
        "expectancy": round(net / n, 4),
        "expectancy_cents_per_contract": round(100 * net / contracts, 4) if contracts else 0.0,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 4) if losses and sum(losses) != 0 else None,
        "gross_pnl": round(gross, 2), "fees": round(fees, 2), "slippage": round(slip, 2), "net_pnl": round(net, 2),
        "max_drawdown": round(mdd, 2),
        "avg_hold_seconds": round(sum(t["hold_seconds"] or 0 for t in ts) / n, 1),
        "trades_per_day": round(n / span_days, 2) if span_days >= 1 else n,
        "longest_win_streak": best_w, "longest_loss_streak": best_l, "contracts": contracts,
    }
    if starting_equity:
        out["return_pct"] = round(100 * net / starting_equity, 3)
        out["max_drawdown_pct"] = round(100 * mdd / starting_equity, 3)
    return out


def breakdown(trades: Iterable[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list] = defaultdict(list)
    for t in trades:
        groups[str(t.get(key))].append(t)
    return {k: summarize(v) for k, v in sorted(groups.items())}


def full_report(db, env: str, since_ts: float | None = None) -> dict[str, Any]:
    q = "SELECT * FROM trades WHERE env=?"
    params: list[Any] = [env]
    if since_ts:
        q += " AND closed_ts>=?"
        params.append(since_ts)
    trades = db.query(q, params)
    sig = db.query("SELECT accepted, COUNT(*) AS n FROM signals WHERE env=? GROUP BY accepted", (env,))
    missed = db.scalar("SELECT COUNT(*) FROM missed_trades WHERE env=?", (env,)) or 0
    entries = db.scalar("SELECT COUNT(*) FROM orders WHERE env=? AND purpose='ENTRY'", (env,)) or 0
    util = db.query("SELECT AVG(exposure) AS e, AVG(equity) AS q FROM equity WHERE env=?", (env,))
    cap_util = None
    if util and util[0]["q"]:
        cap_util = round((util[0]["e"] or 0) / util[0]["q"], 4)
    lat = db.query("SELECT AVG(latency_ms) AS l FROM trades WHERE env=? AND latency_ms IS NOT NULL", (env,))
    return {
        "environment": env,
        "label": "REAL MONEY - REALIZED LIVE NET P&L" if env == "LIVE" else f"SIMULATED ({env}) - NOT REAL MONEY",
        "summary": summarize(trades), "capital_utilization": cap_util,
        "entry_fill_rate": round(1 - missed / entries, 4) if entries else None,
        "missed_entries": missed, "entry_orders": entries,
        "avg_entry_latency_ms": round(lat[0]["l"], 1) if lat and lat[0]["l"] else None,
        "signals": {("accepted" if r["accepted"] else "rejected"): r["n"] for r in sig},
        "by_strategy_version": breakdown(trades, "strategy_version"),
        "by_strategy": breakdown(trades, "strategy"), "by_sport": breakdown(trades, "sport"),
        "by_category": breakdown(trades, "category"), "by_regime": breakdown(trades, "regime"),
        "by_hour": breakdown(trades, "hour_of_day"), "by_exit_reason": breakdown(
            [dict(t, exit_kind=(t["exit_reason"] or "").split(":")[0]) for t in trades], "exit_kind"),
    }


def evidence_note(summary: dict[str, Any], min_trades: int = 300) -> str:
    n = summary.get("total_trades", 0)
    if n < min_trades:
        return (f"INSUFFICIENT EVIDENCE: {n} trades (< {min_trades}). No conclusion about profitability "
                f"can be drawn from this sample.")
    return "Sample size adequate for a preliminary read; still verify out-of-sample and across regimes."
