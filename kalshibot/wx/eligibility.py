"""WX-7: production eligibility per contract class - a RECORD, not a switch.

Each class is evaluated against recorded criteria:

    settled sample     enough settled contracts to judge (prediction)
    skill vs market    Brier better than the market's own mid, with the 95%
                       interval above zero (beating climatology is not enough;
                       the market is the benchmark that pays)
    calibration        reliability gap small in well-populated bins
    paper performance  enough settled paper trades, mean P&L interval above 0
    execution cost     paid spread + fees below the expected edge
    risk review        operator sign-off  (data/control/wx_reviews.json)
    authorization      operator sign-off  (same file)

The first three move a class RESEARCH_ONLY -> MODEL_READY; with the paper
criteria it is PAPER_TRADING; only with all seven is it labelled
PRODUCTION_ELIGIBLE. That label changes nothing that trades: production
execution is locked by env_guard and nothing in this package holds a trading
client. Opening production is a separate, explicit operator action.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .paper import class_report
from .store import utcnow
from .taxonomy import MODELLED

log = logging.getLogger(__name__)

DEFAULTS = {"min_settled": 100, "max_reliability_gap": 0.10, "min_paper_trades": 50}


def load_reviews(path: str | Path) -> dict:
    try:
        return json.loads(Path(path).read_text()) or {}
    except (OSError, ValueError):
        return {}


def evaluate(store, reviews_path: str | Path, thresholds: dict | None = None) -> dict:
    th = {**DEFAULTS, **(thresholds or {})}
    rep = class_report(store)
    reviews = load_reviews(reviews_path)
    out = {}
    for cls in sorted(MODELLED):
        pred = (rep.get(cls) or {}).get("prediction") or {}
        trad = (rep.get(cls) or {}).get("trading") or {}
        rv = reviews.get(cls) or {}
        ci = pred.get("skill_ci95")
        pci = trad.get("pnl_ci95")
        crit = {
            "settled_sample": {"ok": (pred.get("n") or 0) >= th["min_settled"],
                               "value": pred.get("n") or 0, "need": th["min_settled"]},
            "skill_vs_market": {"ok": bool(ci and ci[0] > 0), "value": pred.get("skill_vs_market"),
                                "ci95": ci, "need": "95% interval above 0"},
            "calibration": {"ok": pred.get("max_reliability_gap") is not None
                            and pred["max_reliability_gap"] <= th["max_reliability_gap"],
                            "value": pred.get("max_reliability_gap"),
                            "need": f"<= {th['max_reliability_gap']}"},
            "paper_performance": {"ok": (trad.get("settled") or 0) >= th["min_paper_trades"]
                                  and bool(pci and pci[0] > 0),
                                  "value": trad.get("pnl_mean"), "ci95": pci,
                                  "trades": trad.get("settled") or 0,
                                  "need": f">= {th['min_paper_trades']} trades, mean P&L interval above 0"},
            "execution_cost": {"ok": trad.get("mean_expected_edge") is not None
                               and trad["mean_expected_edge"] > 0
                               and (trad.get("pnl_mean") or -1) > 0,
                               "value": trad.get("mean_expected_edge"),
                               "need": "expected edge after spread + fees > 0 and realised > 0"},
            "risk_review": {"ok": bool(rv.get("risk_review")), "value": rv.get("risk_review"),
                            "need": "operator sign-off"},
            "operator_authorization": {"ok": bool(rv.get("authorized")), "value": rv.get("authorized"),
                                       "need": "operator sign-off"},
        }
        model_ready = all(crit[k]["ok"] for k in ("settled_sample", "skill_vs_market", "calibration"))
        paper = model_ready and crit["paper_performance"]["ok"] and crit["execution_cost"]["ok"]
        prod = paper and crit["risk_review"]["ok"] and crit["operator_authorization"]["ok"]
        status = ("PRODUCTION_ELIGIBLE" if prod else "PAPER_TRADING" if paper
                  else "MODEL_READY" if model_ready else "RESEARCH_ONLY")
        blockers = [k for k, v in crit.items() if not v["ok"]]
        out[cls] = {"status": status, "blockers": blockers, "criteria": crit}
        prev = store.one("SELECT status FROM wx_class_status WHERE cls=?", (cls,))
        store.execute("INSERT INTO wx_class_status (cls, status, reason, at) VALUES (?,?,?,?) "
                      "ON CONFLICT(cls) DO UPDATE SET status=excluded.status, reason=excluded.reason, "
                      "at=excluded.at", (cls, status, "blockers: " + ", ".join(blockers) if blockers
                                         else "all criteria met", utcnow()))
        store.execute("INSERT INTO wx_eligibility (at, cls, eligible, status, criteria) VALUES (?,?,?,?,?)",
                      (utcnow(), cls, int(prod), status, json.dumps(crit, default=str)))
        if prev and prev["status"] != status:
            log.warning("WX ELIGIBILITY  %s: %s -> %s", cls, prev["status"], status)
            store.alert("class_status", cls, {"old": prev["status"], "new": status})
    out["_note"] = ("PRODUCTION_ELIGIBLE is a label. Production execution is locked by env_guard "
                    "and stays locked until the operator explicitly opens it.")
    return out
