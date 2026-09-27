"""Position manager: turns execution events into positions, trades and P&L records.

Every order, fill and position change is persisted immediately, so any trade can
be reconstructed from the database and state survives a restart.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from ..engine.volatility import Features
from ..execution.base import ExecutionEvent
from ..models import Direction, Fill, MarketMeta, Order, OrderStatus, Position, Signal
from ..risk.engine import RiskEngine
from ..storage.db import Database

log = logging.getLogger("klvb.positions")


def order_row(o: Order, run_id: str, strategy_version: str | None = None) -> dict:
    return {
        "id": o.id, "env": o.env, "run_id": run_id, "ticker": o.ticker, "side": o.side, "action": o.action,
        "count": o.count, "limit_price": o.limit_price, "purpose": o.purpose, "style": o.style,
        "reason": o.reason, "signal_id": o.signal_id, "position_id": o.position_id,
        "client_order_id": o.client_order_id, "exchange_order_id": o.exchange_order_id,
        "status": o.status.value, "filled": o.filled, "avg_fill_price": o.avg_fill_price, "fees": o.fees,
        "reject_reason": o.reject_reason, "ref_price": o.ref_price, "expires_ts": o.expires_ts,
        "event_ts": o.event_ts, "data_ts": o.data_ts, "signal_ts": o.signal_ts, "created_ts": o.created_ts,
        "submitted_ts": o.submitted_ts, "ack_ts": o.ack_ts, "first_fill_ts": o.first_fill_ts,
        "final_ts": o.final_ts, "payload": o.payload, "strategy_version": strategy_version,
        "updated_ts": o.final_ts or o.ack_ts or o.submitted_ts or o.created_ts,
    }


def fill_row(f: Fill) -> dict:
    return {"id": f.id, "order_id": f.order_id, "env": f.env, "ticker": f.ticker, "side": f.side,
            "action": f.action, "count": f.count, "price": f.price, "fee": f.fee, "is_taker": f.is_taker,
            "ts": f.ts, "ref_price": f.ref_price, "slippage": f.slippage, "exchange_fill_id": f.exchange_fill_id}


def position_row(p: Position, now: float) -> dict:
    return {
        "id": p.id, "env": p.env, "ticker": p.ticker, "event_ticker": p.event_ticker, "side": p.side,
        "direction": p.direction.value, "strategy": p.strategy, "strategy_version": p.strategy_version,
        "signal_id": p.signal_id, "qty": p.qty, "avg_entry": p.avg_entry, "bought": p.bought, "sold": p.sold,
        "entry_cost": p.entry_cost, "exit_proceeds": p.exit_proceeds, "fees": p.fees,
        "slippage_cost": p.slippage_cost, "target": p.target, "stop": p.stop, "status": p.status,
        "opened_ts": p.opened_ts, "closed_ts": p.closed_ts, "entry_reason": p.entry_reason,
        "exit_reason": p.exit_reason, "signal_score": p.signal_score, "sport": p.sport, "category": p.category,
        "regime": p.regime, "peak_mark": p.peak_mark, "trough_mark": p.trough_mark, "last_mark": p.last_mark,
        "max_hold_seconds": p.max_hold_seconds, "entry_features": p.entry_features, "updated_ts": now,
    }


def position_from_row(r: dict) -> Position:
    p = Position(env=r["env"], ticker=r["ticker"], event_ticker=r["event_ticker"] or "", side=r["side"],
                 direction=Direction(r["direction"]), strategy=r["strategy"],
                 strategy_version=r["strategy_version"], signal_id=r["signal_id"], target=r["target"],
                 stop=r["stop"], max_hold_seconds=r["max_hold_seconds"] or 900,
                 entry_reason=r["entry_reason"] or "", signal_score=r["signal_score"] or 0,
                 sport=r["sport"] or "OTHER", category=r["category"] or "", regime=r["regime"] or "")
    p.id = r["id"]
    for k in ("qty", "bought", "sold"):
        setattr(p, k, int(r[k] or 0))
    for k in ("avg_entry", "entry_cost", "exit_proceeds", "fees", "slippage_cost"):
        setattr(p, k, float(r[k] or 0.0))
    p.status, p.opened_ts, p.closed_ts = r["status"], r["opened_ts"], r["closed_ts"]
    p.exit_reason = r["exit_reason"] or ""
    p.peak_mark, p.trough_mark, p.last_mark = r["peak_mark"], r["trough_mark"], r["last_mark"]
    p.entry_features = r["entry_features"] if isinstance(r["entry_features"], dict) else {}
    return p


class PositionManager:
    def __init__(self, db: Database, env: str, risk: RiskEngine, run_id: str, tz: str = "America/New_York"):
        self.db = db
        self.env = env
        self.risk = risk
        self.run_id = run_id
        self.tz = ZoneInfo(tz)
        self.positions: dict[str, Position] = {}
        self.orders: dict[str, Order] = {}
        self.version_by_signal: dict[str, str] = {}
        self.closed_callbacks = []

    # ---- queries ----------------------------------------------------------
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.status != "CLOSED"]

    def by_ticker(self, ticker: str) -> Position | None:
        for p in self.positions.values():
            if p.ticker == ticker and p.status != "CLOSED":
                return p
        return None

    def working_orders(self) -> list[Order]:
        return [o for o in self.orders.values() if not o.status.terminal]

    def pending_entry_tickers(self) -> set[str]:
        return {o.ticker for o in self.working_orders() if o.purpose == "ENTRY"}

    def has_working_exit(self, pos: Position) -> bool:
        return any(o.position_id == pos.id and o.purpose == "EXIT" for o in self.working_orders())

    def working_take_profit(self, pos: Position) -> Order | None:
        return next((o for o in self.working_orders()
                     if o.position_id == pos.id and o.purpose == "TAKE_PROFIT"), None)

    def unrealized_dollars(self, features: dict[str, Features]) -> float:
        tot = 0.0
        for p in self.open_positions():
            f = features.get(p.ticker)
            bid = f.side_bid(p.side) if f else p.last_mark
            tot += p.unrealized(bid if bid is not None else p.last_mark)
        return tot / 100.0

    def exposure_dollars(self) -> float:
        return sum(p.avg_entry * p.qty for p in self.open_positions()) / 100.0

    # ---- lifecycle ----------------------------------------------------------
    def open_from_signal(self, sig: Signal, meta: MarketMeta, qty: int, regime: str, features: Features,
                         limit_slippage_cents: float, now: float) -> tuple[Position, Order]:
        pos = Position(env=self.env, ticker=sig.ticker, event_ticker=meta.event_ticker, side=sig.side,
                       direction=sig.direction, strategy=sig.strategy, strategy_version=sig.strategy_version,
                       signal_id=sig.id, target=sig.target, stop=sig.stop, max_hold_seconds=sig.max_hold_seconds,
                       entry_reason="; ".join(sig.reasons), signal_score=sig.score, sport=meta.sport,
                       category=meta.category, regime=regime, opened_ts=now,
                       entry_features={"signal_details": sig.details, "features": features.as_dict()})
        maker = sig.entry_style == "maker"
        bid = features.side_bid(sig.side)
        limit = (bid if maker and bid is not None else sig.entry_ref + limit_slippage_cents)
        order = Order(env=self.env, ticker=sig.ticker, side=sig.side, action="buy", count=qty,
                      limit_price=round(limit, 4), purpose="ENTRY", style="maker" if maker else "taker",
                      reason=f"{sig.strategy} entry", signal_id=sig.id, position_id=pos.id,
                      ref_price=sig.entry_ref, data_ts=features.ts - (features.data_age or 0.0),
                      signal_ts=sig.ts, created_ts=now)
        self.positions[pos.id] = pos
        self.version_by_signal[sig.id] = sig.strategy_version
        self.persist_position(pos, now)
        self.persist_order(order)
        return pos, order

    def exit_order(self, pos: Position, reason: str, limit: float, now: float) -> Order:
        order = Order(env=self.env, ticker=pos.ticker, side=pos.side, action="sell", count=pos.qty,
                      limit_price=round(max(1.0, limit), 4), purpose="EXIT", style="taker", reason=reason,
                      signal_id=pos.signal_id, position_id=pos.id, ref_price=pos.last_mark, signal_ts=now,
                      created_ts=now)
        pos.status = "CLOSING"
        pos.exit_reason = reason
        self.persist_position(pos, now)
        self.persist_order(order)
        return order

    def take_profit_order(self, pos: Position, now: float) -> Order:
        """Post-only, reduce-only resting SELL at the target (maker fee, no spread crossing)."""
        order = Order(env=self.env, ticker=pos.ticker, side=pos.side, action="sell", count=pos.qty,
                      limit_price=round(pos.target, 4), purpose="TAKE_PROFIT", style="maker",
                      reason="FIXED_PROFIT_TARGET (resting maker)", signal_id=pos.signal_id, position_id=pos.id,
                      ref_price=pos.target, signal_ts=now, created_ts=now,
                      expires_ts=pos.opened_ts + pos.max_hold_seconds + 60)
        self.persist_order(order)
        return order

    def persist_order(self, o: Order) -> None:
        self.orders[o.id] = o
        ver = self.version_by_signal.get(o.signal_id or "")
        self.db.upsert("orders", order_row(o, self.run_id, ver))

    def persist_position(self, p: Position, now: float) -> None:
        self.db.upsert("positions", position_row(p, now))

    def mark(self, pos: Position, f: Features | None) -> None:
        if f is None:
            return
        bid = f.side_bid(pos.side)
        if bid is None:
            return
        pos.last_mark = bid
        pos.peak_mark = bid if pos.peak_mark is None else max(pos.peak_mark, bid)
        pos.trough_mark = bid if pos.trough_mark is None else min(pos.trough_mark, bid)

    def on_event(self, ev: ExecutionEvent, now: float) -> list[dict]:
        """Apply an execution event. Returns closed-trade records (for alerts/logs)."""
        o = ev.order
        self.persist_order(o)
        closed = []
        pos = self.positions.get(o.position_id or "")
        for f in ev.fills:
            self.db.insert("fills", fill_row(f))
            if pos is not None:
                pos.apply_fill(f)
        if pos is None:
            return closed
        if o.purpose == "ENTRY":
            if o.filled > 0 and pos.status == "OPENING":
                pos.status = "OPEN"
                self.risk.on_entry(pos.ticker, now)
            if o.status.terminal and pos.bought == 0:
                pos.status = "CLOSED"
                pos.closed_ts = now
                pos.exit_reason = f"ENTRY_NOT_FILLED: {o.status.value} {o.reject_reason}"
            elif o.status.terminal and pos.status == "OPENING":
                pos.status = "OPEN"
        elif o.purpose in ("EXIT", "TAKE_PROFIT"):
            if pos.qty <= 0 and pos.bought > 0 and pos.status != "CLOSED":
                if o.purpose == "TAKE_PROFIT":
                    pos.exit_reason = "FIXED_PROFIT_TARGET: resting maker sell filled at target"
                closed.append(self._close(pos, now))
            elif o.status.terminal and o.purpose == "EXIT":
                pos.status = "OPEN"   # exit incomplete: the exit engine will try again
        self.persist_position(pos, now)
        return closed

    def settle(self, pos: Position, settlement_side_price: float, now: float) -> dict:
        """Market settled while held: contracts pay 100 or 0 (no trading fee on settlement)."""
        if pos.qty > 0:
            pos.exit_proceeds += settlement_side_price * pos.qty
            pos.sold += pos.qty
            pos.qty = 0
        pos.exit_reason = pos.exit_reason or "SETTLED"
        rec = self._close(pos, now)
        self.persist_position(pos, now)
        return rec

    def _close(self, pos: Position, now: float) -> dict:
        pos.status = "CLOSED"
        pos.closed_ts = now
        gross = pos.realized_gross
        net = gross - pos.fees
        avg_exit = pos.exit_proceeds / pos.sold if pos.sold else None
        entry_order = next((o for o in self.orders.values()
                            if o.position_id == pos.id and o.purpose == "ENTRY"), None)
        latency = None
        if entry_order and entry_order.first_fill_ts and entry_order.signal_ts:
            latency = (entry_order.first_fill_ts - entry_order.signal_ts) * 1000
        rec = {
            "id": pos.id, "env": pos.env, "run_id": self.run_id, "ticker": pos.ticker,
            "event_ticker": pos.event_ticker, "sport": pos.sport, "category": pos.category,
            "strategy": pos.strategy, "strategy_version": pos.strategy_version, "side": pos.side,
            "qty": pos.bought, "avg_entry": pos.avg_entry, "avg_exit": avg_exit, "gross_pnl": gross,
            "fees": pos.fees, "slippage": pos.slippage_cost, "net_pnl": net, "opened_ts": pos.opened_ts,
            "closed_ts": now, "hold_seconds": now - pos.opened_ts, "entry_reason": pos.entry_reason,
            "exit_reason": pos.exit_reason, "signal_score": pos.signal_score, "regime": pos.regime,
            "mfe": (pos.peak_mark - pos.avg_entry) if pos.peak_mark is not None else None,
            "mae": (pos.trough_mark - pos.avg_entry) if pos.trough_mark is not None else None,
            "hour_of_day": datetime.fromtimestamp(pos.opened_ts, self.tz).hour, "signal_id": pos.signal_id,
            "latency_ms": latency,
        }
        self.db.insert("trades", rec, replace=True)
        self.risk.on_position_closed(pos, net / 100.0, now)
        for cb in self.closed_callbacks:
            cb(rec)
        return rec

    # ---- restart recovery ---------------------------------------------------
    def load_open(self) -> list[Position]:
        rows = self.db.query("SELECT * FROM positions WHERE env=? AND status != 'CLOSED'", (self.env,))
        for r in rows:
            p = position_from_row(r)
            self.positions[p.id] = p
            self.version_by_signal[p.signal_id or ""] = p.strategy_version
        return list(self.positions.values())

    def restore_risk(self, now: float) -> None:
        realized_total = (self.db.scalar("SELECT COALESCE(SUM(net_pnl),0) FROM trades WHERE env=?", (self.env,)) or 0) / 100
        day_start = datetime.fromtimestamp(now, self.tz).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        today = (self.db.scalar("SELECT COALESCE(SUM(net_pnl),0) FROM trades WHERE env=? AND closed_ts>=?",
                                (self.env, day_start)) or 0) / 100
        strat = {r["strategy"]: (r["pnl"] or 0) / 100 for r in self.db.query(
            "SELECT strategy, SUM(net_pnl) AS pnl FROM trades WHERE env=? AND closed_ts>=? GROUP BY strategy",
            (self.env, day_start))}
        entries = [(r["ticker"], r["opened_ts"]) for r in self.db.query(
            "SELECT ticker, opened_ts FROM positions WHERE env=? AND opened_ts>=? AND bought>0",
            (self.env, now - 3600))]
        self.risk.restore(realized_total, today, strat, entries, now)
