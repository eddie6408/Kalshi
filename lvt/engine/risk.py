"""Centralised risk engine for THIS bot only (sections 35-38).

Every entry passes through check_entry(). Exits are never blocked by risk
limits - reducing risk is always allowed - but they are blocked by the
fail-closed flags that make prices or order state untrustworthy.

Sizing (section 36):
    risk budget  = equity * risk_per_trade_pct
    qty          = budget / (per-contract risk: entry -> stop + fees + slippage)
    then scaled  x min(1, target_vol / realised_vol)     volatility
                 x score / 100                           signal quality
                 x 0.5 if equity is off its peak by >= drawdown_halving_pct
    then capped  by contracts, cost, account exposure, event exposure
                 (correlation) and a share of the displayed liquidity.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from lvt.db import Database
from lvt.engine.positions import PositionBook
from lvt.engine.volatility import Metrics
from lvt.models import MarketInfo, Signal, Snapshot


@dataclass
class RiskDecision:
    ok: bool
    qty: int = 0
    limit_price: float = 0.0
    reason: str = ""
    checks: list[tuple[str, bool, str]] = field(default_factory=list)


class RiskEngine:
    def __init__(self, cfg, db: Database, env: str, book: PositionBook, tz: str = "UTC"):
        self.cfg = cfg                         # full config
        self.r = cfg.risk
        self.db = db
        self.env = env
        self.book = book
        self.tz = ZoneInfo(tz)
        self.entry_times: list[float] = []
        self.peak_equity = self.r.paper_starting_equity
        self.fail_closed: dict[str, str] = {}   # reason code -> detail
        self.kill_switch = False
        self.entries_halted_reason = ""

    # -- account state -------------------------------------------------------
    def day_start(self, now: float) -> float:
        d = datetime.fromtimestamp(now, self.tz)
        return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def realized_total(self) -> float:
        return float(self.db.scalar(
            "SELECT SUM(realized_pnl_usd) FROM positions WHERE env=? AND status='CLOSED'",
            (self.env,), 0.0))

    def realized_today(self, now: float, strategy: str | None = None) -> float:
        sql = "SELECT SUM(realized_pnl_usd) FROM positions WHERE env=? AND status='CLOSED' AND closed_ts>=?"
        args: list = [self.env, self.day_start(now)]
        if strategy:
            sql += " AND strategy=?"
            args.append(strategy)
        return float(self.db.scalar(sql, args, 0.0))

    def equity(self, live_balance_usd: float | None = None) -> float:
        if live_balance_usd is not None:
            return live_balance_usd + self.book.unrealized_usd()
        return self.r.paper_starting_equity + self.realized_total() + self.book.unrealized_usd()

    def daily_pnl(self, now: float, strategy: str | None = None) -> float:
        unreal = sum(p.unrealized_pnl_usd for p in self.book.open_positions()
                     if strategy is None or p.strategy == strategy)
        return self.realized_today(now, strategy) + unreal

    def daily_loss_hit(self, now: float) -> bool:
        return self.daily_pnl(now) <= -self.r.max_daily_loss_usd

    def note_entry(self, now: float) -> None:
        self.entry_times.append(now)

    def trades_last_hour(self, now: float) -> int:
        self.entry_times = [t for t in self.entry_times if now - t < 3600]
        return len(self.entry_times)

    def restore(self, now: float) -> None:
        rows = self.db.query("SELECT opened_ts FROM positions WHERE env=? AND opened_ts>=?",
                             (self.env, now - 3600))
        self.entry_times = [r["opened_ts"] for r in rows]
        peak = self.db.scalar("SELECT MAX(v) FROM (SELECT CAST(json_extract(v,'$.equity') AS REAL) v "
                              "FROM status WHERE k='equity_peak_" + self.env + "')", (), None)
        if peak:
            self.peak_equity = float(peak)

    # -- entry check ---------------------------------------------------------
    def check_entry(self, sig: Signal, snap: Snapshot, m: Metrics, info: MarketInfo, now: float,
                    has_open_order: bool, live_balance_usd: float | None = None) -> RiskDecision:
        r = self.r
        checks: list[tuple[str, bool, str]] = []

        def chk(name, ok, detail):
            checks.append((name, bool(ok), detail))
            return ok

        equity = self.equity(live_balance_usd)
        self.peak_equity = max(self.peak_equity, equity)
        chk("kill_switch", not self.kill_switch, "kill switch is ON" if self.kill_switch else "off")
        chk("fail_closed", not self.fail_closed,
            "; ".join(f"{k}: {v}" for k, v in self.fail_closed.items()) or "all critical components healthy")
        chk("entries_halted", not self.entries_halted_reason, self.entries_halted_reason or "no halt")
        dp = self.daily_pnl(now)
        chk("max_daily_loss", dp > -r.max_daily_loss_usd, f"daily P&L ${dp:.2f} vs limit -${r.max_daily_loss_usd}")
        sp = self.daily_pnl(now, sig.strategy)
        chk("max_strategy_loss", sp > -r.max_strategy_daily_loss_usd,
            f"{sig.strategy} daily P&L ${sp:.2f} vs limit -${r.max_strategy_daily_loss_usd}")
        n = self.trades_last_hour(now)
        chk("max_trade_frequency", n < r.max_trades_per_hour, f"{n} entries in the last hour (max {r.max_trades_per_hour})")
        opn = self.book.open_positions()
        chk("max_concurrent_positions", len(opn) < r.max_concurrent_positions,
            f"{len(opn)} open (max {r.max_concurrent_positions})")
        chk("one_position_per_market", self.book.by_ticker(sig.ticker) is None and not has_open_order,
            "market already has a position or a working order" if (self.book.by_ticker(sig.ticker) or has_open_order)
            else "market is flat")
        spread = snap.spread()
        chk("max_spread", spread is not None and spread <= r.max_spread_cents, f"spread {spread}c (max {r.max_spread_cents}c)")
        liq = snap.liquidity_usd()
        chk("min_liquidity", liq >= r.min_liquidity_usd, f"${liq:.0f} at the touch (min ${r.min_liquidity_usd})")
        age = now - snap.ts
        chk("data_fresh", age <= self.cfg.eligibility.max_data_age_s, f"quote {age:.1f}s old")
        ttc = (info.close_ts - now) if info.close_ts else 1e9
        chk("time_to_close", ttc >= self.cfg.discovery.min_minutes_to_close * 60 + self.cfg.exits.settlement_buffer_s,
            f"{ttc / 60:.0f} min to close")
        chk("max_holding_period", self.cfg.exits.max_hold_s > 0, f"max hold {self.cfg.exits.max_hold_s}s enforced by exit engine")

        if not all(c[1] for c in checks):
            failed = [c for c in checks if not c[1]]
            return RiskDecision(False, reason=f"{failed[0][0]}: {failed[0][2]}", checks=checks)

        # -- sizing ----------------------------------------------------------
        ask = sig.ask or 0.0
        limit = math.floor(min(ask + r.max_entry_slippage_cents, 99.0))
        if limit < ask:
            limit = math.ceil(ask)
        risk_c = float(sig.explanation.get("risk_cents") or 0.0) or max(ask - (sig.stop or 0.0), 1.0)
        budget_usd = equity * r.risk_per_trade_pct
        qty = budget_usd * 100.0 / max(risk_c, 0.5)
        vol = max(m.realized_vol, 1e-6)
        vol_scale = min(1.0, r.target_volatility / vol)
        qty *= vol_scale
        qty *= max(min(sig.score / 100.0, 1.0), 0.0)
        dd = (self.peak_equity - equity) / self.peak_equity if self.peak_equity > 0 else 0.0
        if dd >= r.drawdown_halving_pct:
            qty *= 0.5
        caps = {
            "max_position_contracts": r.max_position_contracts,
            "max_position_cost": r.max_position_cost_usd * 100.0 / limit,
            "max_total_exposure": (r.max_total_exposure_usd - self.book.exposure_usd()) * 100.0 / limit,
            "max_event_exposure": (r.max_event_exposure_usd - self.book.event_exposure_usd(sig.event_ticker)) * 100.0 / limit,
            "depth_participation": r.max_depth_participation * sum(
                s for p, s in snap.ask_levels(sig.side, self.cfg.discovery.depth_max_age_s) if p <= limit),
        }
        binding = min(caps, key=caps.get)
        final = int(max(0.0, min(qty, caps[binding])))
        detail = (f"budget ${budget_usd:.2f} / risk {risk_c:.1f}c -> vol x{vol_scale:.2f}, score x{sig.score / 100:.2f}, "
                  f"drawdown {dd:.1%} -> {qty:.1f}; caps {', '.join(f'{k}={v:.0f}' for k, v in caps.items())}")
        if chk("position_size", final >= 1, f"{final} contracts ({detail})"):
            return RiskDecision(True, qty=final, limit_price=float(limit), reason="ok", checks=checks)
        return RiskDecision(False, reason=f"size rounds to zero ({'binding ' + binding if caps[binding] < qty else 'budget'})",
                            checks=checks)

    def record_equity(self, now: float) -> None:
        self.db.insert("status", {"k": "equity_peak_" + self.env, "ts": now,
                                  "v": {"equity": round(self.peak_equity, 2)}}, replace=True)
