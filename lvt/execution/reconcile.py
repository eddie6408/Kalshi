"""Reconciliation (section 39).

Compares the engine's position book with the venue's record. In LIVE the
venue is Kalshi and Kalshi is the source of truth; in PAPER/SHADOW it is
the paper venue's own fill ledger.

Startup sequence: CONNECT -> ACCOUNT -> ORDERS -> FILLS -> POSITIONS ->
RECONCILE -> VERIFY RISK -> RESUME. Until reconcile() returns ok, the
engine keeps the RECONCILIATION fail-closed flag set and opens nothing.

The bot never "fixes" a mismatch by guessing. Mismatches raise an alert and
keep entries blocked until a human looks (or the mismatch clears, e.g. an
in-flight fill lands).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from lvt.engine.positions import PositionBook
from lvt.execution.base import Venue


@dataclass
class ReconcileResult:
    ok: bool
    mismatches: list[dict] = field(default_factory=list)
    venue_positions: dict = field(default_factory=dict)
    book_positions: dict = field(default_factory=dict)
    unresolved_orders: list[str] = field(default_factory=list)
    error: str = ""

    def summary(self) -> str:
        if self.error:
            return f"reconciliation error: {self.error}"
        if self.ok:
            return f"reconciled: {len(self.book_positions)} positions agree"
        return f"{len(self.mismatches)} mismatches, {len(self.unresolved_orders)} unresolved orders"


def reconcile(book: PositionBook, venue: Venue, unresolved_orders: list[str]) -> ReconcileResult:
    try:
        vpos = venue.positions()
    except Exception as exc:                          # noqa: BLE001
        return ReconcileResult(False, error=f"venue positions unavailable: {exc}")
    bpos: dict[str, tuple[str, int]] = {}
    for p in book.open_positions():
        if p.qty > 0:
            bpos[p.ticker] = (p.side, p.qty)
    mismatches = []
    for t in sorted(set(vpos) | set(bpos)):
        v, b = vpos.get(t), bpos.get(t)
        if v != b:
            kind = ("UNEXPECTED_POSITION" if b is None else "MISSING_AT_VENUE" if v is None else "QUANTITY_MISMATCH")
            mismatches.append({"ticker": t, "kind": kind, "venue": v, "book": b})
    ok = not mismatches and not unresolved_orders
    return ReconcileResult(ok, mismatches, vpos, bpos, list(unresolved_orders))
