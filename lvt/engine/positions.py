"""Position manager (section 34).

One position per market at a time: Kalshi nets YES and NO holdings in a
market into a single position, so running two strategies on the same
market would make their P&L unattributable.

P&L decomposition (section 24), per closed position, all in USD:

    gross_mid  = (exit_mid - entry_mid) * qty            what the market moved
  - spread     = (entry_ask - entry_mid + exit_mid - exit_bid) * qty
  - slippage   = (avg_entry - entry_ask + exit_bid - avg_exit) * qty
  = gross      = (avg_exit - avg_entry) * qty            what the fills made
  - fees
  = net (realized_pnl_usd)

entry_ask / exit_bid are the side-relative touch prices when the decision
was taken, so slippage includes latency and book-walk costs.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, fields
from typing import Optional

from lvt.db import Database
from lvt.fees import fee_usd
from lvt.models import FillEvent, MarketInfo, Signal, new_id

log = logging.getLogger("lvt.positions")


@dataclass
class Position:
    id: str
    run_id: str
    env: str
    ticker: str
    event_ticker: str
    category: str
    sport: str
    strategy: str
    strategy_version: str
    side: str
    qty: int = 0
    entry_qty: int = 0
    exit_qty: int = 0
    avg_entry: float = 0.0
    avg_exit: float = 0.0
    cost_usd: float = 0.0
    entry_fees_usd: float = 0.0
    exit_fees_usd: float = 0.0
    realized_pnl_usd: float = 0.0
    gross_pnl_usd: float = 0.0
    gross_mid_pnl_usd: float = 0.0
    spread_cost_usd: float = 0.0
    slippage_cost_usd: float = 0.0
    target: float = 0.0
    stop: float = 0.0
    peak_bid: float = 0.0
    trail_stop: float = 0.0
    entry_mid: float = 0.0
    exit_mid: float = 0.0
    entry_ref_ask: float = 0.0
    exit_ref_bid: float = 0.0
    last_bid: float = 0.0
    unrealized_pnl_usd: float = 0.0
    mfe_cents: float = 0.0
    mae_cents: float = 0.0
    opened_ts: float = 0.0
    closed_ts: float = 0.0
    status: str = "PENDING"         # PENDING (entry order out) | OPEN | CLOSING | CLOSED | VOID
    entry_reason: str = ""
    exit_reason: str = ""
    signal_id: str = ""
    signal_score: float = 0.0
    regime: str = ""
    state: str = ""
    hour_utc: int = 0
    updated_ts: float = 0.0
    # bookkeeping kept in memory only
    _exit_proceeds_cents: float = 0.0
    _entry_cost_cents: float = 0.0
    _maker_exit_tried: bool = False     # one maker attempt per exit, then taker
    _tp_next_ts: float = 0.0            # earliest time to (re)place a take-profit

    def row(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if not k.startswith("_")}

    @property
    def is_open(self) -> bool:
        return self.status in ("PENDING", "OPEN", "CLOSING")

    def exposure_usd(self) -> float:
        return self.qty * self.avg_entry / 100.0


class PositionBook:
    def __init__(self, db: Database, env: str, run_id: str):
        self.db = db
        self.env = env
        self.run_id = run_id
        self.positions: dict[str, Position] = {}

    # -- persistence ---------------------------------------------------------
    def save(self, p: Position) -> None:
        p.updated_ts = time.time()
        self.db.insert("positions", p.row(), replace=True)

    def load_open(self) -> list[Position]:
        rows = self.db.query("SELECT * FROM positions WHERE env=? AND status IN ('PENDING','OPEN','CLOSING')",
                             (self.env,))
        names = {f.name for f in fields(Position)}
        for r in rows:
            p = Position(**{k: v for k, v in r.items() if k in names and v is not None})
            p._entry_cost_cents = p.avg_entry * p.entry_qty
            p._exit_proceeds_cents = p.avg_exit * p.exit_qty
            self.positions[p.id] = p
        return list(self.positions.values())

    # -- queries -------------------------------------------------------------
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.is_open]

    def by_ticker(self, ticker: str) -> Optional[Position]:
        for p in self.positions.values():
            if p.ticker == ticker and p.is_open:
                return p
        return None

    def exposure_usd(self) -> float:
        return sum(p.exposure_usd() for p in self.open_positions())

    def event_exposure_usd(self, event_ticker: str) -> float:
        return sum(p.exposure_usd() for p in self.open_positions() if p.event_ticker == event_ticker)

    def unrealized_usd(self) -> float:
        return sum(p.unrealized_pnl_usd for p in self.open_positions() if p.status != "PENDING")

    # -- lifecycle -----------------------------------------------------------
    def open_pending(self, sig: Signal, info: MarketInfo, now: float) -> Position:
        p = Position(id=new_id("pos"), run_id=self.run_id, env=self.env, ticker=sig.ticker,
                     event_ticker=sig.event_ticker, category=info.category, sport=info.sport,
                     strategy=sig.strategy, strategy_version=sig.strategy_version, side=sig.side,
                     target=sig.target or 0.0, stop=sig.stop or 0.0,
                     entry_mid=sig.mid or 0.0, entry_ref_ask=sig.ask or 0.0,
                     opened_ts=now, status="PENDING", entry_reason="; ".join(sig.reasons)[:1000],
                     signal_id=sig.id, signal_score=sig.score, regime=sig.regime, state=sig.state,
                     hour_utc=time.gmtime(now).tm_hour)
        self.positions[p.id] = p
        self.save(p)
        return p

    def apply_entry_fill(self, p: Position, f: FillEvent) -> None:
        p._entry_cost_cents += f.price * f.count
        p.entry_qty += f.count
        p.qty += f.count
        p.avg_entry = p._entry_cost_cents / p.entry_qty
        p.entry_fees_usd = round(p.entry_fees_usd + f.fee_usd, 4)
        p.cost_usd = round(p._entry_cost_cents / 100.0 + p.entry_fees_usd, 4)
        p.status = "OPEN"
        if not p.peak_bid:
            p.peak_bid = f.price
        self.save(p)

    def entry_finished(self, p: Position, now: float, reason: str = "") -> None:
        """Entry order reached a terminal state. No fill at all -> VOID."""
        if p.entry_qty == 0:
            p.status = "VOID"
            p.closed_ts = now
            p.exit_reason = reason or "ENTRY_NOT_FILLED"
            self.save(p)

    def apply_exit_fill(self, p: Position, f: FillEvent) -> None:
        p._exit_proceeds_cents += f.price * f.count
        p.exit_qty += f.count
        p.qty -= f.count
        p.avg_exit = p._exit_proceeds_cents / p.exit_qty
        p.exit_fees_usd = round(p.exit_fees_usd + f.fee_usd, 4)
        if p.qty <= 0:
            self._close(p, f.ts)
        else:
            self.save(p)

    def settle(self, p: Position, result: str, now: float) -> None:
        """Market settled while held: paid out 100c on the winning side, no fee."""
        payout = 100.0 if result == p.side else 0.0
        self.apply_exit_fill(p, FillEvent("settlement", now, payout, p.qty, 0.0))
        p.exit_reason = p.exit_reason or f"SETTLED_{result.upper()}"
        self.save(p)

    def _close(self, p: Position, ts: float) -> None:
        q = p.entry_qty
        p.qty = 0
        p.status = "CLOSED"
        p.closed_ts = ts
        p.gross_pnl_usd = round((p._exit_proceeds_cents - p._entry_cost_cents) / 100.0, 4)
        p.realized_pnl_usd = round(p.gross_pnl_usd - p.entry_fees_usd - p.exit_fees_usd, 4)
        if p.entry_mid and p.exit_mid:
            p.gross_mid_pnl_usd = round((p.exit_mid - p.entry_mid) * q / 100.0, 4)
            p.spread_cost_usd = round(((p.entry_ref_ask - p.entry_mid) + (p.exit_mid - p.exit_ref_bid)) * q / 100.0, 4)
            p.slippage_cost_usd = round(((p.avg_entry - p.entry_ref_ask) + (p.exit_ref_bid - p.avg_exit)) * q / 100.0, 4)
        p.unrealized_pnl_usd = 0.0
        self.save(p)

    def mark(self, p: Position, bid: Optional[float], fee_type: str, fee_mult: float) -> None:
        """Mark to what we could actually sell for: the held side's BID, less
        the exit fee. Never the mid."""
        if p.status not in ("OPEN", "CLOSING") or bid is None or p.qty <= 0:
            return
        p.last_bid = bid
        if bid > p.peak_bid:
            p.peak_bid = bid
        move = bid - p.avg_entry
        p.mfe_cents = max(p.mfe_cents, move)
        p.mae_cents = min(p.mae_cents, move)
        exit_fee = fee_usd(p.qty, bid, fee_type, fee_mult)
        # Whole-position P&L if we sold the rest now: includes any contracts
        # already sold on a partial exit, and every fee paid or still due.
        p.unrealized_pnl_usd = round(
            (p._exit_proceeds_cents + bid * p.qty - p._entry_cost_cents) / 100.0
            - p.entry_fees_usd - p.exit_fees_usd - exit_fee, 4)
