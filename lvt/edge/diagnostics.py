"""After-the-fact analysis (brief sections 25-29, 31, 33): all of it uses data
recorded AFTER the decision, so none of it is ever an input to a decision.

* event responses   how far the market actually moved 10/30/60/180 s after a
                    score event, vs the model's expected impact
* trade diagnostics why each closed trade won or lost
* counterfactuals   hypothetical P&L of every rejected / watched / no-edge
                    setup, labelled COUNTERFACTUAL and never mixed with P&L
"""
from __future__ import annotations

from lvt.db import Database
from lvt.fees import fee_usd

HORIZONS = (10, 30, 60, 180)
BASE_OFFSET_S = 6.0          # measure from just before the poll that preceded the event


def _mid_path(tape: Database, ticker: str, t0: float, t1: float) -> list[tuple[float, float, float, float]]:
    """(ts, yes_bid, yes_ask, mid) from the tape, t0 <= ts <= t1."""
    rows = tape.query("SELECT ts, yes_bid, yes_ask FROM quotes WHERE ticker=? AND ts>=? AND ts<=? ORDER BY ts",
                      (ticker, t0, t1))
    return [(r["ts"], r["yes_bid"], r["yes_ask"], (r["yes_bid"] + r["yes_ask"]) / 2)
            for r in rows if r["yes_bid"] is not None and r["yes_ask"] is not None]


def _at(path, ts):
    val = None
    for t, _, _, m in path:
        if t <= ts:
            val = m
        else:
            break
    return val


def label_event_responses(db: Database, tape: Database, now: float, limit: int = 2000) -> int:
    rows = db.query("SELECT id, ts, ticker, expected_impact FROM event_responses WHERE labeled_ts IS NULL AND ts<=? "
                    "ORDER BY ts LIMIT ?", (now - max(HORIZONS) - 5, limit))
    out = []
    for r in rows:
        path = _mid_path(tape, r["ticker"], r["ts"] - BASE_OFFSET_S - 10, r["ts"] + max(HORIZONS))
        base = _at(path, r["ts"] - BASE_OFFSET_S)
        acts = [(None if base is None or _at(path, r["ts"] + h) is None else round(_at(path, r["ts"] + h) - base, 3))
                for h in HORIZONS]
        exp, a60 = r["expected_impact"], acts[2]
        if a60 is None:
            resp = "NO_DATA"
        elif abs(exp) < 0.5:
            resp = "NOISE" if abs(a60) < 1.0 else "MOVE_WITHOUT_JUSTIFICATION"
        elif a60 * exp < 0:
            resp = "CONTRARY"
        else:
            ratio = a60 / exp
            resp = "OVERREACTED" if ratio > 1.5 else "UNDERREACTED" if ratio < 0.5 else "PROPORTIONATE"
        out.append((*acts, resp, now, r["id"]))
    if out:
        db.executemany("UPDATE event_responses SET actual_10s=?, actual_30s=?, actual_60s=?, actual_180s=?, "
                       "response=?, labeled_ts=? WHERE id=?", out)
    return len(out)


def _side_bids(path, side):
    return [(t, b if side == "yes" else 100.0 - a) for t, b, a, _ in path]


def classify_trade(p: dict, sig: dict | None, post: list[tuple[float, float]], events: list[dict]) -> tuple[str, str, dict]:
    """(outcome, category, detail). Rules are deliberately simple and
    visible; they describe, they do not decide anything."""
    net = p["realized_pnl_usd"] or 0.0
    strat = p["strategy"] or ""
    held = (p["closed_ts"] or 0) - (p["opened_ts"] or 0)
    reason = (p["exit_reason"] or "").split(":")[0]
    entry, target = p["avg_entry"] or 0.0, p["target"] or 0.0
    mfe = p["mfe_cents"] or 0.0
    against = [e for e in events if (e.get("expected_impact") or 0) * (1 if p["side"] == "yes" else -1) <= -3]
    detail = {"held_s": round(held), "exit": reason, "mfe_c": mfe, "events_against": len(against)}
    if net > 0:
        if strat.startswith("EDGE_UNFINISHED") or strat in ("MOMENTUM", "DOWNTREND"):
            cat = "UNFINISHED_MOMENTUM" if strat.startswith("EDGE_") else "ORDINARY_MOMENTUM"
        elif strat == "EDGE_FLOW_REVERSAL":
            cat = "ORDER_FLOW_REVERSAL"
        elif reason in ("REFERENCE_VALUE_REACHED", "EDGE_COLLAPSED"):
            cat = "REFERENCE_VALUE_CONVERGENCE"
        elif strat.startswith("EDGE_DISLOCATION") or strat in ("DIP", "REVERSAL"):
            cat = "FAST_REVERSION" if held < 120 else "SLOW_REVERSION"
        else:
            cat = "OVERREACTION_REVERSAL"
        return "WIN", cat, detail
    slip = abs(p["slippage_cost_usd"] or 0.0)
    if slip > 0.5 * abs(net) and slip > 0:
        return "LOSS", "SLIPPAGE", detail
    if sig and sig.get("data_ts") and sig.get("ts") and sig["ts"] - sig["data_ts"] > 4:
        return "LOSS", "DATA_DELAY", detail
    if against:
        return "LOSS", ("EVENT_REPRICING" if strat.startswith("EDGE_") else "UNEXPECTED_EVENT"), detail
    if reason == "THESIS_INVALIDATED":
        return "LOSS", "REFERENCE_VALUE_ERROR", detail
    if reason == "LIQUIDITY_EXIT":
        return "LOSS", "LIQUIDITY_FAILURE", detail
    if target and entry and mfe >= 0.5 * (target - entry):
        return "LOSS", "CORRECT_ENTRY_WRONG_EXIT", detail
    if post and target and any(b >= target for _, b in post):
        detail["target_reached_after_exit"] = True
        return "LOSS", "CORRECT_DIRECTION_WRONG_ENTRY", detail
    momentum = strat in ("MOMENTUM", "DOWNTREND") or strat.startswith("EDGE_UNFINISHED")
    if momentum:
        return "LOSS", "MOMENTUM_FAILURE" if mfe <= 0 else "MOMENTUM_EXHAUSTION", detail
    if reason.startswith("MAX_LOSS") and post and p["stop"] and min(b for _, b in post) < p["stop"] - (entry - p["stop"]):
        return "LOSS", "CONTINUED_COLLAPSE", detail
    return "LOSS", "FALSE_REVERSAL", detail


def diagnose_trades(db: Database, tape: Database, now: float, limit: int = 500) -> int:
    rows = db.query("SELECT p.* FROM positions p WHERE p.status='CLOSED' AND p.closed_ts<=? AND NOT EXISTS "
                    "(SELECT 1 FROM trade_diagnostics d WHERE d.position_id=p.id) ORDER BY p.closed_ts LIMIT ?",
                    (now - 600, limit))
    for p in rows:
        sig = db.one("SELECT * FROM signals WHERE id=?", (p["signal_id"],)) if p.get("signal_id") else None
        post = _side_bids(_mid_path(tape, p["ticker"], p["closed_ts"], p["closed_ts"] + 600), p["side"])
        events = db.query("SELECT expected_impact FROM event_responses WHERE ticker=? AND ts>=? AND ts<=?",
                          (p["ticker"], p["opened_ts"], p["closed_ts"]))
        outcome, cat, detail = classify_trade(p, sig, post, events)
        db.insert("trade_diagnostics", {"position_id": p["id"], "env": p["env"], "strategy": p["strategy"],
                                        "outcome": outcome, "category": cat, "detail": detail, "ts": now},
                  replace=True)
    return len(rows)


def counterfactuals(db: Database, tape: Database, now: float, horizon: int = 900, limit: int = 500) -> int:
    """Hypothetical taker round trip for setups we did not take (1 contract):
    buy at the signal's ask, exit at the target or the stop (whichever the
    side's bid reached first) or at the bid at the horizon; fees included."""
    rows = db.query("SELECT s.* FROM signals s WHERE s.decision IN ('REJECTED','WATCH','NO_EDGE') AND s.ts<=? "
                    "AND s.ask IS NOT NULL AND s.target IS NOT NULL AND s.stop IS NOT NULL AND NOT EXISTS "
                    "(SELECT 1 FROM counterfactuals c WHERE c.signal_id=s.id AND c.horizon_s=?) ORDER BY s.ts LIMIT ?",
                    (now - horizon - 5, horizon, limit))
    out = []
    for s in rows:
        path = _side_bids(_mid_path(tape, s["ticker"], s["ts"], s["ts"] + horizon), s["side"])
        if not path:
            out.append((s["id"], horizon, "COUNTERFACTUAL", None, None, None, None, None, now))
            continue
        entry = s["ask"]
        exit_px, tt, ts_ = None, None, None
        for t, b in path:
            if b >= s["target"]:
                exit_px, tt = s["target"], t - s["ts"]
                break
            if b <= s["stop"]:
                exit_px, ts_ = b, t - s["ts"]
                break
        if exit_px is None:
            exit_px = path[-1][1]
        fees = (fee_usd(1, entry) + fee_usd(1, max(exit_px, 1.0))) * 100.0
        bids = [b for _, b in path]
        out.append((s["id"], horizon, "COUNTERFACTUAL", round(exit_px - entry - fees, 3), tt, ts_,
                    round(max(bids) - entry, 3), round(min(bids) - entry, 3), now))
    if out:
        db.executemany("INSERT OR REPLACE INTO counterfactuals VALUES (?,?,?,?,?,?,?,?,?)", out)
    return len(out)


def run_all(db: Database, tape: Database, now: float) -> dict:
    return {"event_responses": label_event_responses(db, tape, now),
            "trade_diagnostics": diagnose_trades(db, tape, now),
            "counterfactuals": counterfactuals(db, tape, now)}

