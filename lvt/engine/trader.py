"""The trading engine: SCAN -> MEASURE -> CLASSIFY -> SIGNAL -> COSTS -> RISK
-> ENTER -> MONITOR -> REASSESS -> HOLD/EXIT -> RECORD (section 7).

The engine is data-source agnostic. The live runner feeds it snapshots
fetched from Kalshi; the replay runner feeds it snapshots read back from the
tape, on a replay clock. Same code path, so a backtest exercises exactly the
logic that trades.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Optional

from lvt.alerts import Alerter
from lvt.data.provider import EventContext
from lvt.db import Database
from lvt.engine.exits import evaluate_exit
from lvt.engine.positions import Position, PositionBook
from lvt.engine.risk import RiskEngine
from lvt.engine.scanner import eligibility
from lvt.engine.states import classify
from lvt.engine.strategies import Strategy, build_strategies
from lvt.engine.volatility import MarketSeries, Metrics, compute_metrics
from lvt.execution.base import Venue
from lvt.execution.order_manager import OrderManager
from lvt.models import (TERMINAL_ORDER_STATES, MarketInfo, OrderRequest, OrderUpdate, Signal,
                        Snapshot, new_id)

log = logging.getLogger("lvt.engine")


class Engine:
    def __init__(self, cfg, env: str, run_id: str, db: Database, venue: Venue,
                 markets: dict[str, MarketInfo], alerter: Alerter, clock,
                 strategies: Optional[list[Strategy]] = None, record_scanner: bool = True):
        self.cfg = cfg
        self.env = env
        self.run_id = run_id
        self.db = db
        self.venue = venue
        self.markets = markets
        self.alerter = alerter
        self.clock = clock
        self.strategies = strategies if strategies is not None else build_strategies(cfg)
        self.series: dict[str, MarketSeries] = {}
        self.metrics: dict[str, Metrics] = {}
        self.book = PositionBook(db, env, run_id)
        self.risk = RiskEngine(cfg, db, env, self.book, cfg.app.timezone)
        self.om = OrderManager(db, venue, env, run_id, self._on_order_update)
        self.contexts: dict[str, EventContext] = {}
        self.watch_seen: dict[tuple, float] = {}
        self.buy_attempts: dict[tuple, float] = {}
        self.cooldown_until: dict[str, float] = {}
        self.last_state: dict[str, str] = {}
        self.last_elig: dict[str, bool] = {}
        self.last_hold_log: dict[str, float] = {}
        self.scanner_rows: dict[str, dict] = {}
        self.emergency_reason = ""
        self.record_scanner = record_scanner
        self.counters = {"signals": 0, "buy_signals": 0, "entries": 0, "exits": 0, "rejected": 0}

    # ------------------------------------------------------------------ setup
    def restore(self, now: float) -> None:
        self.book.load_open()
        self.om.restore()
        self.risk.restore(now)

    # ------------------------------------------------------------------ cycle
    def on_snapshots(self, snaps: dict[str, Snapshot], now: float, tradeable: Optional[set[str]] = None) -> None:
        for t, s in snaps.items():
            ser = self.series.get(t)
            if ser is None:
                ser = self.series[t] = MarketSeries(t, self.cfg.volatility.history_s)
            ser.add(s)

        # 1. working orders advance (paper fills resolve here)
        self.om.process(snaps, now)
        if self.om.unknown:
            self.risk.fail_closed["UNKNOWN_ORDER_STATE"] = f"{len(self.om.unknown)} order(s) in unknown state"
            still = self.om.resolve_unknown(now)
            if not still:
                self.risk.fail_closed.pop("UNKNOWN_ORDER_STATE", None)
            else:
                self.alerter.fire("UNKNOWN_ORDER_STATE", f"{len(still)} order(s) unresolved", "CRITICAL")

        # 2. metrics for everything we are watching or holding
        held = {p.ticker for p in self.book.open_positions()}
        for t in set(snaps) | held:
            ser = self.series.get(t)
            if ser is not None:
                m = compute_metrics(ser, now, self.cfg.volatility)
                if m is not None:
                    self.metrics[t] = m

        # 3. positions: settlement, marking, exits
        self._manage_positions(now)

        # 4. daily loss limit (waived in paper research mode - see risk.py)
        if self.risk.limits_enforced and self.risk.daily_loss_hit(now):
            if not self.risk.entries_halted_reason:
                self.risk.entries_halted_reason = "daily loss limit reached - no new positions today"
                self.alerter.fire("DAILY_LOSS_LIMIT", f"daily P&L ${self.risk.daily_pnl(now):.2f}", "CRITICAL")
                if self.cfg.risk.daily_loss_policy == "flatten":
                    self.emergency_reason = "daily loss limit (flatten policy)"
        elif self.risk.entries_halted_reason.startswith("daily loss"):
            # a new day
            self.risk.entries_halted_reason = ""
            if self.emergency_reason.startswith("daily loss"):
                self.emergency_reason = ""

        # 5. entries
        candidates = tradeable if tradeable is not None else set(snaps)
        for t in candidates:
            if t in snaps:
                self._evaluate_market(t, snaps[t], now)

        self._prune(now)

    # ------------------------------------------------------------- positions
    def _manage_positions(self, now: float) -> None:
        for p in self.book.open_positions():
            ser = self.series.get(p.ticker)
            snap = ser.last_snapshot if ser else None
            info = self.markets.get(p.ticker) or MarketInfo(p.ticker)
            if snap is None:
                continue
            q = snap.quote
            if q.result in ("yes", "no") and q.status in ("settled", "finalized", "determined") and p.status == "OPEN":
                self.book.settle(p, q.result, now)
                self._log_decision(p, "SETTLED", f"market settled {q.result}", now)
                continue
            self.book.mark(p, snap.bid(p.side), info.fee_type, info.fee_multiplier)
            if p.status != "OPEN" or self.om.has_working(p.ticker):
                continue
            m = self.metrics.get(p.ticker)
            age = now - snap.ts
            if age > self.cfg.eligibility.max_data_age_s and not self.emergency_reason:
                self._maybe_hold_log(p, f"HOLD: data {age:.1f}s stale - not acting on untrusted prices", now)
                continue
            dec, hold = evaluate_exit(p, snap, m, info, now, self.cfg.exits,
                                      self.contexts.get(p.event_ticker), self.emergency_reason)
            if dec is None:
                self._maybe_hold_log(p, hold, now)
                if now - p.updated_ts > 30:
                    self.book.save(p)              # persist marks for the dashboard
                continue
            p.status = "CLOSING"
            p.exit_reason = dec.reason
            p.exit_mid = snap.mid(p.side) or dec.bid
            p.exit_ref_bid = dec.bid
            self.book.save(p)
            self._log_decision(p, "EXIT", f"{dec.reason}: {dec.detail}", now,
                               {"bid": dec.bid, "limit": dec.limit})
            req = OrderRequest(client_order_id=new_id("lvt"), ticker=p.ticker, event_ticker=p.event_ticker,
                               action="sell", side=p.side, count=p.qty, limit_price=float(max(1, math.floor(dec.limit))),
                               purpose="EXIT", ref_price=dec.bid, created_ts=now, position_id=p.id,
                               signal_id=p.signal_id, exit_reason=dec.reason, data_ts=snap.ts, signal_ts=now,
                               strategy=p.strategy)
            up = self.om.submit(req, now)
            self.counters["exits"] += 1
            if up.status in ("REJECTED", "UNKNOWN"):
                if up.status == "REJECTED":
                    p.status = "OPEN"
                    self.book.save(p)
                log.warning("exit order for %s %s: %s", p.ticker, up.status, up.reason)

    def _maybe_hold_log(self, p: Position, text: str, now: float) -> None:
        if now - self.last_hold_log.get(p.id, 0.0) >= 60:
            self.last_hold_log[p.id] = now
            self._log_decision(p, "HOLD", text, now)

    def _log_decision(self, p: Position, decision: str, reason: str, now: float, details: dict | None = None) -> None:
        self.db.insert("decisions", {"ts": now, "run_id": self.run_id, "env": self.env, "ticker": p.ticker,
                                     "position_id": p.id, "decision": decision, "reason": reason[:500],
                                     "details": details or {}})

    # --------------------------------------------------------------- entries
    def _evaluate_market(self, t: str, snap: Snapshot, now: float) -> None:
        m = self.metrics.get(t)
        info = self.markets.get(t)
        if m is None or info is None:
            return
        state = classify(m, self.cfg.volatility, self.cfg.eligibility)
        elig = eligibility(m, info, now, self.cfg.eligibility, self.cfg.discovery.min_minutes_to_close)
        if self.last_state.get(t) != state.primary:
            self.last_state[t] = state.primary
            self.db.insert("market_states", {"ts": now, "run_id": self.run_id, "ticker": t, "state": state.primary,
                                             "labels": state.labels, "explanation": state.explanation})
        if self.last_elig.get(t) != elig.ok:
            self.last_elig[t] = elig.ok
            self.db.insert("eligibility_log", {"ts": now, "run_id": self.run_id, "ticker": t, "eligible": elig.ok,
                                               "reasons": elig.reasons, "metrics": m.as_dict()})
        pos = self.book.by_ticker(t)
        row = {"ticker": t, "event": info.event_ticker, "title": info.title, "sub_title": info.sub_title,
               "sport": info.sport, "category": info.category, "bid": m.bid, "ask": m.ask, "spread": m.spread,
               "last": m.last, "volume": round(m.volume_long, 1), "liquidity": round(m.liquidity_usd, 1),
               "volatility": round(m.realized_vol, 3), "recent_high": m.recent_high, "recent_low": m.recent_low,
               "velocity": round(m.velocity_short, 3), "acceleration": round(m.acceleration, 3),
               "state": state.primary, "labels": state.labels, "eligible": elig.ok,
               "elig_reasons": list(elig.reasons.values())[:3] if not elig.ok else [],
               "movement_score": None, "dip_score": None, "momentum_score": None,
               "position": f"{pos.side.upper()} x{pos.qty}" if pos else "", "action": "", "ts": now}
        self.scanner_rows[t] = row
        if not elig.ok or not state.tradeable:
            return
        if m.history_s < self.cfg.signals.min_history_s:
            row["action"] = f"warming up ({m.history_s:.0f}/{self.cfg.signals.min_history_s}s)"
            return

        for strat in self.strategies:
            sig = strat.evaluate(self.series[t], m, state, info, now)
            if sig is None:
                continue
            key_score = "dip_score" if strat.detector == "dip" else "momentum_score"
            row[key_score] = max(row[key_score] or 0, sig.score)
            row["movement_score"] = max(row["movement_score"] or 0, sig.score)
            ctx = self.contexts.get(info.event_ticker)
            if ctx is not None:
                sig.explanation["event_context"] = {"provider": ctx.provider, **ctx.summary()}
            k = (t, strat.name, sig.anchor)
            if sig.action == "WATCH":
                if k in self.watch_seen:
                    continue
                self.watch_seen[k] = now
                sig.decision = "WATCH"
                self._record_signal(sig)
                continue
            last = self.buy_attempts.get(k)
            if last is not None and now - last < self.cfg.signals.cooldown_s:
                continue
            self.buy_attempts[k] = now
            self.counters["buy_signals"] += 1
            row["action"] = f"BUY {sig.side.upper()} ({strat.name})"
            self._try_enter(sig, snap, m, info, now, ctx)

    def _try_enter(self, sig: Signal, snap: Snapshot, m: Metrics, info: MarketInfo, now: float,
                   ctx: Optional[EventContext]) -> None:
        if now < self.cooldown_until.get(sig.ticker, 0.0):
            sig.decision, sig.reject_reason = "REJECTED", "market cooldown after a recent exit"
            self._record_signal(sig)
            return
        dec = self.risk.check_entry(sig, snap, m, info, now, self.om.has_working(sig.ticker),
                                    self.venue.balance_usd() if self.venue.is_real else None)
        if dec.waived:
            sig.explanation["would_block_live"] = dec.waived
        for name, ok, detail in dec.checks:
            if not ok or name == "position_size" or detail.startswith("NOT ENFORCED"):
                self.db.insert("risk_events", {"ts": now, "run_id": self.run_id, "env": self.env, "ticker": sig.ticker,
                                               "signal_id": sig.id, "check_name": name, "passed": ok, "detail": detail})
        if not dec.ok:
            sig.decision, sig.reject_reason = "REJECTED", dec.reason
            self.counters["rejected"] += 1
            self._record_signal(sig)
            return
        pos = self.book.open_pending(sig, info, now)
        sig.decision = "TAKEN"
        self._record_signal(sig, pos.id)
        req = OrderRequest(client_order_id=new_id("lvt"), ticker=sig.ticker, event_ticker=sig.event_ticker,
                           action="buy", side=sig.side, count=dec.qty, limit_price=dec.limit_price,
                           purpose="ENTRY", ref_price=sig.ask or 0.0, created_ts=now, position_id=pos.id,
                           signal_id=sig.id, data_ts=sig.data_ts, signal_ts=sig.ts,
                           event_ts=(ctx.event_ts if ctx else 0.0), strategy=sig.strategy)
        up = self.om.submit(req, now)
        self.risk.note_entry(now)
        self.counters["entries"] += 1
        if self.risk.limits_enforced and self.risk.trades_last_hour(now) >= self.cfg.alerts.max_trades_per_hour_alert:
            self.alerter.fire("EXCESSIVE_TRADE_FREQUENCY", f"{self.risk.trades_last_hour(now)} entries in 1h")
        if up.status == "REJECTED":
            self.book.entry_finished(pos, now, f"ENTRY_REJECTED: {up.reason}")

    def _record_signal(self, sig: Signal, position_id: str = "") -> None:
        self.counters["signals"] += 1
        self.db.insert("signals", {
            "id": sig.id, "run_id": self.run_id, "env": self.env, "ts": sig.ts, "ticker": sig.ticker,
            "event_ticker": sig.event_ticker, "category": sig.category, "sport": sig.sport,
            "strategy": sig.strategy, "strategy_version": sig.strategy_version, "side": sig.side,
            "action": sig.action, "stage": sig.stage, "decision": sig.decision, "reject_reason": sig.reject_reason,
            "score": sig.score, "bid": sig.bid, "ask": sig.ask, "mid": sig.mid, "spread": sig.spread,
            "target": sig.target, "stop": sig.stop, "expected_net_cents": sig.expected_net_cents,
            "reward_risk": sig.reward_risk, "state": sig.state, "regime": sig.regime,
            "components": sig.components, "explanation": sig.explanation, "text": sig.text(),
            "anchor": sig.anchor, "data_ts": sig.data_ts, "position_id": position_id or None,
            "hour_utc": time.gmtime(sig.ts).tm_hour,
        })

    # -------------------------------------------------------- order updates
    def _on_order_update(self, req: OrderRequest, up: OrderUpdate) -> None:
        p = self.book.positions.get(req.position_id)
        if p is None:
            log.error("order %s references unknown position %s", req.client_order_id, req.position_id)
            return
        if req.purpose == "ENTRY":
            for f in up.fills:
                self.book.apply_entry_fill(p, f)
            if up.status in TERMINAL_ORDER_STATES:
                self.book.entry_finished(p, up.ts, f"ENTRY_{up.status}: {up.reason}")
        else:
            for f in up.fills:
                self.book.apply_exit_fill(p, f)
            if up.status in TERMINAL_ORDER_STATES and p.status == "CLOSING":
                p.status = "OPEN"                          # remainder: exit engine retries next cycle
                self.book.save(p)
            if p.status == "CLOSED":
                self.cooldown_until[p.ticker] = up.ts + self.cfg.signals.market_cooldown_after_exit_s
                log.info("CLOSED %s %s %s net $%.2f (%s)", p.strategy, p.ticker, p.side, p.realized_pnl_usd, p.exit_reason)
        n = sum(f.count for f in up.fills)
        if n:
            avg = sum(f.price * f.count for f in up.fills) / n
            slip = (avg - req.ref_price) if req.action == "buy" else (req.ref_price - avg)
            if slip > self.cfg.alerts.max_slippage_alert_cents:
                self.alerter.fire("ABNORMAL_SLIPPAGE", f"{req.ticker} {req.purpose} slipped {slip:.1f}c",
                                  key=req.ticker)

    # ----------------------------------------------------------- kill switch
    def set_kill_switch(self, on: bool, reason: str, now: float) -> None:
        if on and not self.risk.kill_switch:
            self.risk.kill_switch = True
            self.alerter.fire("KILL_SWITCH", f"kill switch ON: {reason}", "CRITICAL")
            if self.cfg.kill_switch.cancel_open_orders:
                self.om.cancel_all(now)
            if self.cfg.kill_switch.flatten_positions:
                self.emergency_reason = f"kill switch: {reason}"
        elif not on and self.risk.kill_switch:
            self.risk.kill_switch = False
            if self.emergency_reason.startswith("kill switch"):
                self.emergency_reason = ""
            self.alerter.fire("KILL_SWITCH", "kill switch OFF", "WARNING", key="off")

    # ---------------------------------------------------------------- misc
    def _prune(self, now: float) -> None:
        if len(self.watch_seen) > 20000 or len(self.buy_attempts) > 20000:
            self.watch_seen = {k: v for k, v in self.watch_seen.items() if now - v < 3600}
            self.buy_attempts = {k: v for k, v in self.buy_attempts.items() if now - v < 3600}

    def scanner_json(self) -> list[dict]:
        return sorted(self.scanner_rows.values(), key=lambda r: -(r.get("movement_score") or 0))

    def status(self) -> dict:
        return {"open_positions": len(self.book.open_positions()), "working_orders": len(self.om.working),
                "unknown_orders": len(self.om.unknown), "fail_closed": dict(self.risk.fail_closed),
                "risk_limits": ("ENFORCED" if self.risk.limits_enforced
                                else "OFF (paper research mode) - REVISIT BEFORE SHADOW/LIVE"),
                "kill_switch": self.risk.kill_switch, "entries_halted": self.risk.entries_halted_reason,
                "emergency": self.emergency_reason, "counters": dict(self.counters)}
