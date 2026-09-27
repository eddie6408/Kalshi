"""Reconciliation.

LIVE: Kalshi is the source of truth. On start (and every reconcile interval):
  CONNECT -> ACCOUNT -> ORDERS -> FILLS -> POSITIONS -> RECONCILE -> VERIFY RISK -> RESUME
Any mismatch halts new trading (fail closed) and raises an alert.

PAPER/SHADOW: the simulated ledger is the source of truth; reconciliation checks
internal consistency (positions == sum of their fills; no orphaned working
orders after a restart).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from ..models import OrderStatus
from ..storage.db import Database

log = logging.getLogger("klvb.reconcile")


@dataclass
class ReconcileResult:
    ok: bool
    issues: list[str] = field(default_factory=list)
    info: dict = field(default_factory=dict)


def reconcile_simulated(db: Database, env: str, positions, restart: bool = False) -> ReconcileResult:
    issues: list[str] = []
    info: dict = {}
    if restart:
        # a simulated working order cannot survive a restart: its market context is gone
        stale = db.query("SELECT id FROM orders WHERE env=? AND status IN ('PENDING_SUBMIT','SUBMITTED','RESTING',"
                         "'PARTIALLY_FILLED','UNKNOWN')", (env,))
        for r in stale:
            db.execute("UPDATE orders SET status=?, reject_reason=?, final_ts=? WHERE id=?",
                       (OrderStatus.CANCELED.value, "canceled on restart (simulated order)", time.time(), r["id"]))
        info["orphaned_orders_canceled"] = len(stale)
    for p in positions:
        rows = db.query("SELECT f.action, SUM(f.count) AS n FROM fills f JOIN orders o ON o.id=f.order_id "
                        "WHERE o.position_id=? GROUP BY f.action", (p.id,))
        bought = sum(r["n"] for r in rows if r["action"] == "buy")
        sold = sum(r["n"] for r in rows if r["action"] == "sell")
        if bought - sold != p.qty:
            issues.append(f"{p.ticker}: position qty {p.qty} != fills {bought}-{sold}")
        if restart and p.status in ("OPENING", "CLOSING"):
            p.status = "OPEN" if p.qty > 0 else "CLOSED"
    info["positions_checked"] = len(positions)
    return ReconcileResult(ok=not issues, issues=issues, info=info)


async def reconcile_live(execution, positions, db: Database, env: str = "LIVE") -> ReconcileResult:
    issues: list[str] = []
    info: dict = {}
    try:
        bal = await execution.balance()
        info["balance"] = bal.get("balance")
        exch_positions = await execution.positions()
        resting = await execution.orders(status="resting")
    except Exception as e:  # noqa: BLE001 - cannot reach the source of truth: fail closed
        return ReconcileResult(False, [f"exchange unreachable during reconciliation: {e}"], info)

    exch: dict[str, float] = {}
    for mp in exch_positions:
        pos = mp.get("position_fp", mp.get("position"))
        try:
            n = float(pos or 0)
        except (TypeError, ValueError):
            issues.append(f"unparseable position for {mp.get('ticker')}")
            continue
        if n != 0:
            exch[mp.get("ticker", "")] = n
    local: dict[str, float] = {}
    for p in positions:
        if p.qty:
            local[p.ticker] = local.get(p.ticker, 0.0) + (p.qty if p.side == "yes" else -p.qty)
    for t in sorted(set(exch) | set(local)):
        if abs(exch.get(t, 0.0) - local.get(t, 0.0)) > 1e-9:
            issues.append(f"POSITION MISMATCH {t}: exchange {exch.get(t, 0)} vs local {local.get(t, 0)}")
    known = {o.exchange_order_id for o in execution.working_orders() if o.exchange_order_id}
    known |= {r["exchange_order_id"] for r in db.query(
        "SELECT exchange_order_id FROM orders WHERE env=? AND exchange_order_id IS NOT NULL", (env,))}
    for o in resting:
        if o.get("order_id") not in known:
            issues.append(f"UNKNOWN RESTING ORDER {o.get('order_id')} on {o.get('ticker')} (not placed by this bot?)")
    unknown_local = [o for o in execution.working_orders() if o.status is OrderStatus.UNKNOWN]
    if unknown_local:
        issues.append(f"{len(unknown_local)} order(s) with unknown submit outcome")
    info.update({"exchange_positions": len(exch), "local_positions": len(local), "resting_orders": len(resting)})
    return ReconcileResult(ok=not issues, issues=issues, info=info)
