"""Package 2: what the opportunity ledger needs besides storage.

    stage_for(reason)      which kind of decision a skip reason is
    hypothetical_pnl(...)  what one contract / one set would have made
    resolve_outcomes(...)  the hourly pass that fills in how it turned out

The counterfactual is deliberately simple and labelled as such everywhere
(research brief section 21: observed vs simulated vs projected never mix):
ONE contract - or one set of legs - bought at the touch recorded at decision
time, taker fee, held to settlement. It ignores whether the order would have
filled and adverse selection; Packages 6-7 replace it with a fill model.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from .opportunity import DecisionStage
from .settlement import resolution_of

log = logging.getLogger(__name__)

HYPOTHETICAL_BASIS = ("SIMULATED: 1 contract/set at the touch recorded at decision "
                      "time, taker fee, held to settlement; ignores fill probability "
                      "and adverse selection")

#: Every reason ForecastStrategy._skip can give, and the kind of decision it
#: is. tests/test_opportunity_ledger.py fails if a reason is missing here.
SKIP_STAGES: dict[str, DecisionStage] = {
    # the model's own verdict
    "estimator declined": DecisionStage.MODEL,
    "low confidence": DecisionStage.MODEL,
    "edge too small": DecisionStage.MODEL,
    "fees eat the edge": DecisionStage.MODEL,
    "edge gone at size": DecisionStage.MODEL,
    "disagreement too large to trust": DecisionStage.MODEL,
    "disagreement too large in relative terms": DecisionStage.MODEL,
    "event distribution is incoherent": DecisionStage.MODEL,
    "sized to zero": DecisionStage.MODEL,
    # the data behind it
    "degraded data source": DecisionStage.DATA,
    "data conflict": DecisionStage.DATA,
    "source unavailable": DecisionStage.DATA,
    "stale source": DecisionStage.DATA,
    "observation qc": DecisionStage.DATA,
    # the model has not shown it beats the market here
    "market forecasts better here": DecisionStage.MARKET_SKILL,
    # deliberate rules
    "too far out to have skill": DecisionStage.POLICY,
    "price is in the tail": DecisionStage.POLICY,
    # the book
    "no book": DecisionStage.LIQUIDITY,
    "no two-sided market": DecisionStage.LIQUIDITY,
    "book too thin to exit": DecisionStage.LIQUIDITY,
    "spread too wide to cross": DecisionStage.LIQUIDITY,
    "book moved while pricing": DecisionStage.LIQUIDITY,
}


def stage_for(reason: str) -> DecisionStage | None:
    return SKIP_STAGES.get(reason)


def _pays(side: str, resolved_yes: bool) -> bool:
    return resolved_yes if side == "yes" else not resolved_yes


def hypothetical_pnl(row, outcomes: dict[str, bool], fees) -> tuple[float | None, str]:
    """One contract (a stub) or one set (a priced opportunity) at the touch."""
    rec = json.loads(row["record_json"]) if row["record_json"] else {}
    legs = rec.get("legs") or []
    if legs:
        total = 0.0
        for leg in legs:
            ask = leg.get("best_ask_cents")
            if ask is None or leg["ticker"] not in outcomes:
                return None, "no touch recorded on a leg"
            total += (1.0 if _pays(leg["side"], outcomes[leg["ticker"]]) else 0.0) \
                - ask / 100.0 - fees.entry_fee_usd(ask, 1)
        return total, HYPOTHETICAL_BASIS

    # A stub: one market, the side our probability favoured at the touch.
    touch = json.loads(row["touch_json"] or "{}")
    q = row["probability_yes"]
    ticker = (row["markets"] or "").split(",")[0]
    if q is None or ticker not in outcomes:
        return None, "no probability at decision time"
    options = []
    for side, prob in (("yes", q), ("no", 1.0 - q)):
        ask = touch.get(f"{side}_ask")
        if ask is not None:
            options.append((prob - ask / 100.0, side, ask))
    if not options:
        return None, "no price at decision time"
    _edge, side, ask = max(options)
    pnl = (1.0 if _pays(side, outcomes[ticker]) else 0.0) - ask / 100.0 \
        - fees.entry_fee_usd(ask, 1)
    return pnl, f"{HYPOTHETICAL_BASIS}; side {side.upper()} @ {ask}c"


TRADED = ("FILLED", "PARTIALLY_FILLED", "EXITED", "SETTLED")


def resolve_outcomes(journal, market_client, fees, now: datetime | None = None,
                     limit: int = 200, retention_days: int = 90) -> dict:
    """Fill in how every opportunity past its settlement time turned out."""
    now = now or datetime.now(timezone.utc)
    stats = {"resolved": 0, "pending": 0, "errors": 0}
    rows = journal.pending_outcomes(now.isoformat(), limit)
    cache: dict[str, bool | None] = {}
    for row in rows:
        tickers = [t for t in (row["markets"] or "").split(",") if t]
        outcomes: dict[str, bool] = {}
        unresolved = False
        for t in tickers:
            if t not in cache:
                try:
                    cache[t] = resolution_of(market_client.get_market(t))
                except Exception as exc:  # noqa: BLE001
                    log.debug("LEDGER  could not read %s: %s", t, exc)
                    cache[t] = None
                    stats["errors"] += 1
            if cache[t] is None:
                unresolved = True
                break
            outcomes[t] = cache[t]
        if unresolved:
            stats["pending"] += 1
            continue

        realised = None
        if row["final_state"] in TRADED:
            realised, closed = journal.realised_for(row["opportunity_id"])
            if not closed:
                stats["pending"] += 1          # settlement legs not booked yet
                continue
        hypo, basis = hypothetical_pnl(row, outcomes, fees)
        journal.set_outcome(row["opportunity_id"],
                            {t: ("YES" if v else "NO") for t, v in outcomes.items()},
                            realised, hypo, basis)
        stats["resolved"] += 1

    if stats["resolved"] or stats["pending"]:
        log.info("LEDGER  outcomes: %d resolved, %d still pending, %d read errors",
                 stats["resolved"], stats["pending"], stats["errors"])
    pruned = journal.prune_ledger_records(retention_days)
    if pruned:
        log.info("LEDGER  pruned the full record of %d rejected rows older than "
                 "%d days", pruned, retention_days)
    return stats
