"""Centralized risk engine for this bot only.

Every entry passes through `check_entry`, which either approves a contract count
or returns the list of reasons it was blocked. Exits are never blocked by risk
(reducing exposure is always allowed), except that nothing at all is sent while
the engine is HALTED for an integrity problem in LIVE (unknown order state,
reconciliation mismatch) until a human or the reconciler clears it.
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from ..engine.volatility import Features
from ..models import MarketMeta, Position, Signal


@dataclass
class RiskDecision:
    approved: bool
    qty: int = 0
    reasons: list[str] = field(default_factory=list)
    sizing: dict[str, float] = field(default_factory=dict)


class RiskEngine:
    def __init__(self, risk_cfg, tz: str = "America/New_York", starting_equity: float | None = None):
        self.cfg = risk_cfg
        self.tz = ZoneInfo(tz)
        self.starting_equity = float(starting_equity if starting_equity is not None else risk_cfg.starting_paper_equity)
        self.realized_total = 0.0           # dollars, net of fees, all time for this env
        self.peak_equity = self.starting_equity
        self.day_key = ""
        self.realized_today = 0.0           # dollars
        self.strategy_today: dict[str, float] = {}
        self.entries: deque[float] = deque()
        self.market_entries: dict[str, deque[float]] = {}
        self.cooldown_until: dict[str, float] = {}
        self.kill_switch = False
        self.kill_reason = ""
        self.halted = False
        self.halt_reason = ""
        self.daily_loss_hit = False

    # ---- lifecycle -------------------------------------------------------
    def _roll_day(self, now: float) -> None:
        key = datetime.fromtimestamp(now, self.tz).strftime("%Y-%m-%d")
        if key != self.day_key:
            self.day_key = key
            self.realized_today = 0.0
            self.strategy_today = {}
            self.daily_loss_hit = False

    def restore(self, realized_total: float, realized_today: float, strategy_today: dict[str, float],
                entry_times: list[tuple[str, float]], now: float) -> None:
        """Rebuild counters from the DB after a restart."""
        self._roll_day(now)
        self.realized_total = realized_total
        self.realized_today = realized_today
        self.strategy_today = dict(strategy_today)
        for ticker, ts in sorted(entry_times, key=lambda x: x[1]):
            if now - ts <= 3600:
                self.entries.append(ts)
                self.market_entries.setdefault(ticker, deque()).append(ts)
        self.peak_equity = max(self.peak_equity, self.starting_equity + realized_total)

    def set_kill_switch(self, on: bool, reason: str = "") -> None:
        self.kill_switch = on
        self.kill_reason = reason if on else ""

    def halt(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = reason

    def clear_halt(self) -> None:
        self.halted = False
        self.halt_reason = ""

    # ---- accounting ------------------------------------------------------
    def on_position_closed(self, pos: Position, net_dollars: float, now: float) -> None:
        self._roll_day(now)
        self.realized_total += net_dollars
        self.realized_today += net_dollars
        self.strategy_today[pos.strategy] = self.strategy_today.get(pos.strategy, 0.0) + net_dollars
        self.cooldown_until[pos.ticker] = now + self.cfg.market_cooldown_seconds

    def on_entry(self, ticker: str, now: float) -> None:
        self.entries.append(now)
        self.market_entries.setdefault(ticker, deque()).append(now)

    def equity(self, unrealized_dollars: float) -> float:
        return self.starting_equity + self.realized_total + unrealized_dollars

    def daily_pnl(self, unrealized_dollars: float, now: float) -> float:
        self._roll_day(now)
        return self.realized_today + unrealized_dollars

    def update_peak(self, equity: float) -> None:
        self.peak_equity = max(self.peak_equity, equity)

    def drawdown(self, equity: float) -> float:
        return 0.0 if self.peak_equity <= 0 else max(0.0, (self.peak_equity - equity) / self.peak_equity)

    def daily_limit_breached(self, unrealized_dollars: float, now: float) -> bool:
        breached = self.daily_pnl(unrealized_dollars, now) <= -abs(self.cfg.max_daily_loss)
        if breached:
            self.daily_loss_hit = True
        return self.daily_loss_hit

    # ---- entry gate ------------------------------------------------------
    def check_entry(self, sig: Signal, meta: MarketMeta, f: Features, open_positions: list[Position],
                    unrealized_dollars: float, now: float, pending_entry_tickers: set[str] | None = None,
                    regime: str = "") -> RiskDecision:
        c = self.cfg
        self._roll_day(now)
        why: list[str] = []
        if self.kill_switch:
            why.append(f"kill switch active ({self.kill_reason or 'no reason given'})")
        if self.halted:
            why.append(f"trading halted: {self.halt_reason}")
        if self.daily_limit_breached(unrealized_dollars, now):
            why.append(f"daily loss limit reached ({self.daily_pnl(unrealized_dollars, now):.2f} <= "
                       f"-{c.max_daily_loss}); no new positions today")
        strat_pnl = self.strategy_today.get(sig.strategy, 0.0)
        if strat_pnl <= -abs(c.max_strategy_daily_loss):
            why.append(f"{sig.strategy} daily loss {strat_pnl:.2f} <= -{c.max_strategy_daily_loss}")
        active = [p for p in open_positions if p.status != "CLOSED"]
        if len(active) >= c.max_concurrent_positions:
            why.append(f"max concurrent positions {c.max_concurrent_positions} reached")
        if any(p.ticker == sig.ticker for p in active) or (pending_entry_tickers and sig.ticker in pending_entry_tickers):
            why.append("already have a position/order in this market (Kalshi nets YES/NO)")
        same_event = [p for p in active if meta.event_ticker and p.event_ticker == meta.event_ticker]
        if len(same_event) >= c.max_positions_per_event:
            why.append(f"max {c.max_positions_per_event} position(s) per event (correlation)")
        while self.entries and now - self.entries[0] > 3600:
            self.entries.popleft()
        if len(self.entries) >= c.max_trades_per_hour:
            why.append(f"trade frequency: {len(self.entries)} entries in the last hour")
        me = self.market_entries.get(sig.ticker)
        if me:
            while me and now - me[0] > 3600:
                me.popleft()
            if len(me) >= c.max_trades_per_market_per_hour:
                why.append(f"{len(me)} entries in this market in the last hour")
        cd = self.cooldown_until.get(sig.ticker, 0.0)
        if now < cd:
            why.append(f"market cooldown for {cd - now:.0f}s after last exit")
        if f.spread is None or f.spread > c.max_spread_cents:
            why.append(f"spread {f.spread} > {c.max_spread_cents}c")
        depth = f.side_ask_depth(sig.side) or 0.0
        if depth < c.min_liquidity_contracts:
            why.append(f"entry-side liquidity {depth:.0f} < {c.min_liquidity_contracts}")

        # ---- sizing ----
        entry = sig.entry_ref
        risk_pc = max(entry - sig.stop, 1.0) + max(sig.expected_costs, 0.0)   # cents / contract
        caps = {
            "risk": c.max_risk_per_trade * 100.0 / risk_pc,
            "notional": c.max_position_notional * 100.0 / max(entry, 1.0),
            "contracts": float(c.max_position_contracts),
            "depth": depth * c.max_depth_participation,
        }
        exposure = sum(p.avg_entry * p.qty for p in active) / 100.0
        caps["account_exposure"] = max(0.0, c.max_account_exposure - exposure) * 100.0 / max(entry, 1.0)
        mkt_exp = sum(p.avg_entry * p.qty for p in active if p.ticker == sig.ticker) / 100.0
        caps["market_exposure"] = max(0.0, c.max_market_exposure - mkt_exp) * 100.0 / max(entry, 1.0)
        ev_exp = sum(p.avg_entry * p.qty for p in same_event) / 100.0
        caps["event_exposure"] = max(0.0, c.max_event_exposure - ev_exp) * 100.0 / max(entry, 1.0)
        base = min(caps.values())

        quality = 0.5 + 0.5 * max(0.0, min(1.0, (sig.score - 50.0) / 50.0))
        vol_mult = 0.5 if regime == "EXTREME_VOLATILITY" else 1.0
        eq = self.equity(unrealized_dollars)
        dd = self.drawdown(eq)
        dd_mult = 1.0
        if dd > c.drawdown_size_reduction_start:
            dd_mult = max(c.drawdown_size_floor, 1.0 - (dd - c.drawdown_size_reduction_start) * 5.0)
        qty = int(math.floor(base * quality * vol_mult * dd_mult))
        sizing = {**{f"cap_{k}": round(v, 2) for k, v in caps.items()}, "quality_mult": round(quality, 3),
                  "vol_mult": vol_mult, "drawdown": round(dd, 4), "drawdown_mult": round(dd_mult, 3),
                  "risk_per_contract_cents": round(risk_pc, 2), "equity": round(eq, 2)}
        if qty < 1 and not why:
            why.append(f"position size rounds to 0 (binding cap {min(caps, key=caps.get)})")
        return RiskDecision(approved=not why, qty=max(qty, 0) if not why else 0, reasons=why, sizing=sizing)

    def status(self, unrealized_dollars: float = 0.0, now: float | None = None) -> dict:
        now = now or time.time()
        eq = self.equity(unrealized_dollars)
        return {"kill_switch": self.kill_switch, "kill_reason": self.kill_reason, "halted": self.halted,
                "halt_reason": self.halt_reason, "daily_pnl": round(self.daily_pnl(unrealized_dollars, now), 2),
                "daily_loss_hit": self.daily_loss_hit, "max_daily_loss": self.cfg.max_daily_loss,
                "equity": round(eq, 2), "peak_equity": round(self.peak_equity, 2),
                "drawdown": round(self.drawdown(eq), 4), "entries_last_hour": len(self.entries),
                "strategy_today": {k: round(v, 2) for k, v in self.strategy_today.items()}}
