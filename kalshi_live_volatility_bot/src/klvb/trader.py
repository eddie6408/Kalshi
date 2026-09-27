"""The trading core, shared verbatim by the live application and the replay/backtest engine.

    SCAN -> IDENTIFY VOLATILITY -> MEASURE MOVEMENT -> CLASSIFY STATE -> IDENTIFY OPPORTUNITY
    -> EXPECTED NET MOVEMENT -> LIQUIDITY/SPREAD/EXECUTION CHECKS -> RISK -> ENTER
    -> MONITOR -> REASSESS -> HOLD / EXIT -> RECORD -> CONTINUE

The Trader never fetches data itself; it is *fed* observations (live provider or
recorded history) and only ever evaluates information with ts <= now. That is
what makes replay deterministic and free of look-ahead.
"""
from __future__ import annotations

import logging
from typing import Any

from .engine.fees import FeeModel
from .engine.scanner import eligibility
from .engine.scoring import generic_movement_score
from .engine.states import MarketState, classify
from .engine.volatility import Features, VolatilityEngine
from .execution.base import ExecutionClient, ExecutionEvent
from .models import MarketMeta, MarketSnapshot, OrderBook, OrderStatus, Signal, SportsContext, TradePrint
from .monitoring.alerts import Alerter
from .monitoring.logs import event
from .portfolio.exits import evaluate_exit
from .portfolio.positions import PositionManager
from .risk.engine import RiskEngine
from .storage.db import Database
from .strategies import Strategy, StrategyContext

log = logging.getLogger("klvb.trader")

STATE_RECORD_SECONDS = 15.0
REJECT_RECORD_SECONDS = 60.0
EQUITY_RECORD_SECONDS = 60.0


class Trader:
    def __init__(self, cfg, db: Database, env: str, run_id: str, execution: ExecutionClient,
                 strategies: list[Strategy], risk: RiskEngine, positions: PositionManager, fees: FeeModel,
                 alerter: Alerter | None = None, record_market_states: bool = True):
        self.cfg = cfg
        self.db = db
        self.env = env
        self.run_id = run_id
        self.exec = execution
        self.strategies = strategies
        self.risk = risk
        self.pm = positions
        self.fees = fees
        self.alerter = alerter
        self.record_states = record_market_states
        self.vol = VolatilityEngine(cfg.volatility, cfg.eligibility.depth_window_cents)
        self.metas: dict[str, MarketMeta] = {}
        self.sports: dict[str, SportsContext] = {}
        self.watch: list[str] = []
        self.features: dict[str, Features] = {}
        self.states: dict[str, MarketState] = {}
        self.scores: dict[str, dict[str, float]] = {}
        self.new_trades: dict[str, list[TradePrint]] = {}
        self._last_state_rec: dict[str, float] = {}
        self._last_reject_rec: dict[tuple, float] = {}
        self._last_equity_rec = 0.0
        self.flatten_reason: str | None = None
        self.counters = {"signals": 0, "rejected": 0, "entries": 0, "exits": 0, "missed": 0}

    # ---------------------------------------------------------------- inputs
    def set_meta(self, meta: MarketMeta) -> None:
        self.metas[meta.ticker] = meta

    def set_watchlist(self, tickers: list[str]) -> None:
        pinned = {p.ticker for p in self.pm.open_positions()} | {o.ticker for o in self.pm.working_orders()}
        self.watch = list(dict.fromkeys(list(tickers) + sorted(pinned)))

    async def on_snapshot(self, snap: MarketSnapshot, now: float) -> None:
        self.vol.on_snapshot(snap)
        series = self.vol.get(snap.ticker)
        trades = self.new_trades.pop(snap.ticker, [])
        evs = await self.exec.on_market(snap.ticker, snap, series.book, trades, now, self.metas.get(snap.ticker))
        await self._handle_events(evs, now)

    def on_book(self, ticker: str, book: OrderBook) -> None:
        self.vol.on_book(ticker, book)

    def on_trades(self, ticker: str, trades: list[TradePrint]) -> None:
        if trades:
            self.vol.on_trades(ticker, trades)
            self.new_trades.setdefault(ticker, []).extend(trades)

    def on_sports(self, ctx: dict[str, SportsContext]) -> None:
        self.sports = ctx

    # ---------------------------------------------------------------- core step
    async def step(self, now: float) -> None:
        unreal = self.pm.unrealized_dollars(self.features)
        tickers = list(dict.fromkeys(self.watch + [p.ticker for p in self.pm.open_positions()]))
        for t in tickers:
            self._evaluate_market(t, now)
        await self._manage_positions(now, unreal)
        await self._find_entries(now, unreal)
        self._record_equity(now)

    def _evaluate_market(self, ticker: str, now: float) -> None:
        f = self.vol.features(ticker, now)
        if f is None:
            self.features.pop(ticker, None)
            return
        meta = self.metas.get(ticker) or MarketMeta(ticker=ticker)
        end = meta.effective_end_ts()
        snap = self.vol.get(ticker).last_snapshot
        st = classify(f, self.cfg.volatility, self.cfg.eligibility, self.cfg.volatility.min_samples,
                      (end - now) if end else None, self.cfg.scanner.min_time_to_close_seconds,
                      snap.status if snap else "active")
        ok, why = eligibility(f, self.cfg.eligibility, self.cfg.volatility)
        self.features[ticker], self.states[ticker] = f, st
        sc = self.scores.setdefault(ticker, {})
        sc["movement"] = generic_movement_score(f, self.cfg.volatility, self.cfg.eligibility)
        sc["eligible"] = 1.0 if (ok and st.tradable) else 0.0
        sc["_why"] = why  # type: ignore[assignment]
        if self.record_states and now - self._last_state_rec.get(ticker, 0) >= STATE_RECORD_SECONDS:
            self._last_state_rec[ticker] = now
            self.db.insert("market_states", {
                "ts": now, "run_id": self.run_id, "ticker": ticker, "sport": meta.sport, "states": st.states,
                "explanations": st.explanations, "features": f.as_dict(), "eligible": ok and st.tradable,
                "ineligible_reasons": why, "movement_score": sc["movement"], "dip_score": sc.get("dip"),
                "momentum_score": sc.get("momentum"), "regime": st.regime})

    # ---------------------------------------------------------------- exits
    async def _manage_positions(self, now: float, unreal: float) -> None:
        r = self.cfg.risk
        flatten = self.flatten_reason
        if self.risk.kill_switch and r.kill_switch_flatten:
            flatten = f"kill switch: {self.risk.kill_reason}"
        if r.on_daily_loss == "flatten" and self.risk.daily_limit_breached(unreal, now):
            flatten = "daily loss limit reached (flatten policy)"
        tp_maker = self.cfg.exits.take_profit_style == "maker"
        for pos in self.pm.open_positions():
            if pos.status != "OPEN" or pos.qty <= 0 or self.pm.has_working_exit(pos):
                continue
            f = self.features.get(pos.ticker)
            if f is None or (f.data_age if f.data_age is not None else 1e9) > self.cfg.data.max_data_age_seconds:
                if f is not None and (f.data_age or 0) > self.cfg.data.emergency_data_age_seconds and self.alerter:
                    self.alerter.alert("STALE_DATA", f"no fresh data for open position {pos.ticker} "
                                                     f"({f.data_age:.0f}s); holding, cannot price an exit",
                                       dedupe_key=f"stale:{pos.ticker}")
                continue   # never act on stale prices
            self.pm.mark(pos, f)
            meta = self.metas.get(pos.ticker)
            m = meta or MarketMeta(ticker=pos.ticker)
            snap = self.vol.get(pos.ticker).last_snapshot
            if snap is not None and snap.status in ("closed", "settled", "determined", "finalized"):
                continue   # settlement is handled by the application via market result
            dec = evaluate_exit(pos, f, meta, now, self.cfg.exits, r, flatten, meta.tick_size if meta else 1.0)
            tp = self.pm.working_take_profit(pos)
            if dec is None:
                if tp_maker and tp is None and pos.target > (f.side_bid(pos.side) or 0) and not flatten:
                    order = self.pm.take_profit_order(pos, now)
                    evs = await self.exec.submit(order, m, now)
                    await self._handle_events(evs, now)
                continue
            if tp is not None:
                ev = await self.exec.cancel(tp, now)
                await self._handle_events([ev], now)
                if pos.qty <= 0 or pos.status == "CLOSED":
                    continue
            order = self.pm.exit_order(pos, f"{dec.reason}: {dec.detail}", dec.limit, now)
            order.ref_price = f.side_bid(pos.side)
            order.data_ts = now - (f.data_age or 0)
            event(log, "exit decision", env=self.env, ticker=pos.ticker, position=pos.id, reason=dec.reason,
                  detail=dec.detail, qty=pos.qty, limit=dec.limit)
            self.counters["exits"] += 1
            evs = await self.exec.submit(order, m, now)
            await self._handle_events(evs, now)

    # ---------------------------------------------------------------- entries
    async def _find_entries(self, now: float, unreal: float) -> None:
        blocked_globally = self.risk.kill_switch or self.risk.halted
        for ticker in self.watch:
            f, st = self.features.get(ticker), self.states.get(ticker)
            meta = self.metas.get(ticker)
            if f is None or st is None or meta is None:
                continue
            if self.pm.by_ticker(ticker) is not None or ticker in self.pm.pending_entry_tickers():
                continue
            ok, why = eligibility(f, self.cfg.eligibility, self.cfg.volatility)
            if not (ok and st.tradable):
                continue
            ctx = StrategyContext(now=now, meta=meta, features=f, state=st, series=self.vol.get(ticker),
                                  fees=self.fees, costs_cfg=self.cfg.costs, eligibility_cfg=self.cfg.eligibility,
                                  vol_cfg=self.cfg.volatility, min_score=self.cfg.strategies.min_signal_score,
                                  sports=self.sports.get(meta.event_ticker),
                                  take_profit_maker=self.cfg.exits.take_profit_style == "maker")
            candidates: list[Signal] = []
            step_scores = {"dip": 0.0, "momentum": 0.0}
            for strat in self.strategies:
                try:
                    sig = strat.evaluate(ctx)
                except Exception:  # noqa: BLE001 - a strategy bug must not take the bot down
                    log.exception("strategy %s failed on %s", strat.name, ticker)
                    continue
                if sig is None:
                    continue
                key = "dip" if sig.strategy in ("DIP", "REVERSAL") else "momentum"
                step_scores[key] = max(step_scores[key], sig.score)
                candidates.append(sig)
            self.scores.setdefault(ticker, {}).update(step_scores)
            buys = sorted([s for s in candidates if s.action == "BUY"], key=lambda s: s.score, reverse=True)
            for s in candidates:
                if s.action != "BUY":
                    self._record_signal(s, meta, st, accepted=False)
            if not buys:
                continue
            best = buys[0]
            for other in buys[1:]:
                other.reject_reasons.append(f"lower score than {best.strategy} on the same market")
                self._record_signal(other, meta, st, accepted=False)
            if blocked_globally:
                best.reject_reasons.append("kill switch" if self.risk.kill_switch else f"halted: {self.risk.halt_reason}")
                self._record_signal(best, meta, st, accepted=False)
                continue
            dec = self.risk.check_entry(best, meta, f, self.pm.open_positions(), unreal, now,
                                        self.pm.pending_entry_tickers(), st.regime)
            best.details["sizing"] = dec.sizing
            if not dec.approved:
                best.reject_reasons += dec.reasons
                self._record_signal(best, meta, st, accepted=False)
                if any("daily loss" in r for r in dec.reasons) and self.alerter:
                    self.alerter.alert("DAILY_LOSS_LIMIT", dec.reasons[0], "CRITICAL", dedupe_key="daily_loss")
                continue
            self._record_signal(best, meta, st, accepted=True)
            pos, order = self.pm.open_from_signal(best, meta, dec.qty, st.regime, f,
                                                  self.cfg.risk.max_slippage_cents, now)
            if best.entry_style == "maker":
                order.expires_ts = now + self.cfg.paper.maker_order_ttl_seconds
            ctx_sports = self.sports.get(meta.event_ticker)
            order.event_ts = ctx_sports.ts if ctx_sports else None
            event(log, "entry", env=self.env, ticker=ticker, strategy=best.strategy, version=best.strategy_version,
                  side=best.side, qty=dec.qty, ref=best.entry_ref, limit=order.limit_price, target=best.target,
                  stop=best.stop, score=best.score, explanation=best.explanation(meta.title))
            self.counters["entries"] += 1
            evs = await self.exec.submit(order, meta, now)
            await self._handle_events(evs, now)

    def _record_signal(self, s: Signal, meta: MarketMeta, st: MarketState, accepted: bool) -> None:
        s.accepted = accepted
        self.counters["signals" if accepted else "rejected"] += 1
        if not accepted:
            key = (s.ticker, s.strategy, (s.reject_reasons[0].split(":")[0] if s.reject_reasons else ""))
            if s.ts - self._last_reject_rec.get(key, -1e18) < REJECT_RECORD_SECONDS:
                return
            self._last_reject_rec[key] = s.ts
        self.db.insert("signals", {
            "id": s.id, "ts": s.ts, "run_id": self.run_id, "env": self.env, "ticker": s.ticker,
            "event_ticker": meta.event_ticker, "sport": meta.sport, "category": meta.category,
            "strategy": s.strategy, "strategy_version": s.strategy_version, "direction": s.direction.value,
            "side": s.side, "action": "BUY" if accepted else "NO_TRADE", "entry_ref": s.entry_ref,
            "target": s.target, "stop": s.stop, "score": s.score, "expected_gross": s.expected_gross,
            "expected_costs": s.expected_costs, "expected_net": s.expected_net, "accepted": accepted,
            "reject_reasons": s.reject_reasons, "reasons": s.reasons, "details": s.details,
            "components": s.score_components, "explanation": s.explanation(meta.title), "regime": st.regime})

    # ---------------------------------------------------------------- events
    async def _handle_events(self, evs: list[ExecutionEvent], now: float) -> None:
        for ev in evs:
            o = ev.order
            closed = self.pm.on_event(ev, now)
            for rec in closed:
                event(log, "trade closed", env=self.env, ticker=rec["ticker"], strategy=rec["strategy"],
                      net_cents=round(rec["net_pnl"], 2), gross_cents=round(rec["gross_pnl"], 2),
                      fees_cents=rec["fees"], exit_reason=rec["exit_reason"], hold_s=round(rec["hold_seconds"], 1))
            for fl in ev.fills:
                if self.alerter and abs(fl.slippage) > self.cfg.alerts.abnormal_slippage_cents:
                    self.alerter.alert("ABNORMAL_SLIPPAGE", f"{fl.ticker} {fl.action} {fl.count}@{fl.price:.1f}c "
                                                            f"slippage {fl.slippage:.1f}c", dedupe_key=f"slip:{fl.ticker}")
            if o.purpose == "ENTRY" and o.status in (OrderStatus.MISSED, OrderStatus.REJECTED):
                self.counters["missed"] += 1
                f = self.features.get(o.ticker)
                self.db.insert("missed_trades", {
                    "ts": now, "env": self.env, "signal_id": o.signal_id, "order_id": o.id, "ticker": o.ticker,
                    "strategy": self.pm.positions.get(o.position_id or "").strategy if o.position_id in self.pm.positions else None,
                    "strategy_version": self.pm.version_by_signal.get(o.signal_id or ""),
                    "reason": f"{o.status.value}: {o.reject_reason}", "ref_price": o.ref_price,
                    "price_after": f.side_ask(o.side) if f else None,
                    "details": {"limit": o.limit_price, "latency_s": (o.final_ts or now) - (o.signal_ts or now)}})
            if o.status is OrderStatus.UNKNOWN:
                self.risk.halt(f"order {o.id} submit outcome unknown")
                if self.alerter:
                    self.alerter.alert("UNKNOWN_ORDER_STATE", f"{o.ticker} order {o.client_order_id}: {o.reject_reason}",
                                       "CRITICAL")

    def _record_equity(self, now: float) -> None:
        if now - self._last_equity_rec < EQUITY_RECORD_SECONDS:
            return
        self._last_equity_rec = now
        unreal = self.pm.unrealized_dollars(self.features)
        eq = self.risk.equity(unreal)
        self.risk.update_peak(eq)
        self.db.insert("equity", {"ts": now, "env": self.env, "equity": eq, "realized": self.risk.realized_total,
                                  "unrealized": unreal, "exposure": self.pm.exposure_dollars(),
                                  "open_positions": len(self.pm.open_positions())})

    # ---------------------------------------------------------------- views
    def scanner_rows(self) -> list[dict[str, Any]]:
        rows = []
        for t in self.watch:
            f, st, meta = self.features.get(t), self.states.get(t), self.metas.get(t)
            if f is None or st is None:
                continue
            pos = self.pm.by_ticker(t)
            sc = self.scores.get(t, {})
            rows.append({
                "ticker": t, "sport": meta.sport if meta else "", "event": meta.event_ticker if meta else "",
                "title": meta.title if meta else "", "price": f.price, "bid": f.bid, "ask": f.ask, "spread": f.spread,
                "volume": f.volume_window, "volume_24h": f.volume_24h,
                "liquidity": min(f.bid_depth or 0, f.ask_depth or 0), "volatility": round(f.realized_vol, 2),
                "recent_high": f.recent_high, "recent_low": f.recent_low, "velocity": round(f.velocity_short, 2),
                "acceleration": round(f.acceleration, 2), "state": ", ".join(s for s in st.states if s != st.regime),
                "regime": st.regime, "movement_score": sc.get("movement"), "dip_score": sc.get("dip"),
                "momentum_score": sc.get("momentum"),
                "position": f"{pos.side.upper()} x{pos.qty}" if pos else "",
                "action": "HOLD" if pos else ("WATCH" if sc.get("eligible") else "NO_TRADE"),
                "data_age": round(f.data_age or 0, 1),
            })
        rows.sort(key=lambda r: r["movement_score"] or 0, reverse=True)
        return rows

    def position_rows(self, now: float) -> list[dict[str, Any]]:
        out = []
        for p in self.pm.open_positions():
            f = self.features.get(p.ticker)
            bid = f.side_bid(p.side) if f else p.last_mark
            out.append({"id": p.id, "ticker": p.ticker, "side": p.side.upper(), "qty": p.qty,
                        "entry": round(p.avg_entry, 2), "current": bid,
                        "unrealized": round(p.unrealized(bid) / 100 - p.fees / 100, 2),
                        "target": p.target, "stop": p.stop, "hold_seconds": round(now - p.opened_ts),
                        "strategy": p.strategy, "version": p.strategy_version, "score": p.signal_score,
                        "status": p.status, "sport": p.sport})
        return out
