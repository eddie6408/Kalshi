"""Deterministic risk management.

This module has veto power. The estimator can be as excited as it likes; if
RiskManager says no, nothing happens. Every rule here is a hard number read
from config.yaml, never a judgement call, and every rejection is logged with
a reason so you can audit why the bot did or didn't trade.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .edge import contracts_from_kelly, ev_usd
from .fees import FeeModel
from .models import Market, Orderbook, Position, SizedOrder, TradeIntent

log = logging.getLogger(__name__)


@dataclass
class RiskState:
    """Everything the risk engine needs to know about right now."""
    balance_usd: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    open_exposure_usd: float = 0.0
    realized_pnl_today_usd: float = 0.0
    unrealized_pnl_usd: float = 0.0
    consecutive_losses: int = 0
    order_timestamps: list[datetime] = field(default_factory=list)
    last_order_by_ticker: dict[str, datetime] = field(default_factory=dict)

    def exposure_in(self, ticker: str) -> float:
        p = self.positions.get(ticker)
        return (p.total_cost_cents / 100.0) if p else 0.0

    @property
    def day_pnl_usd(self) -> float:
        return self.realized_pnl_today_usd + self.unrealized_pnl_usd


@dataclass
class RiskDecision:
    approved: bool
    order: SizedOrder | None
    reasons: list[str] = field(default_factory=list)

    @classmethod
    def reject(cls, reason: str) -> "RiskDecision":
        return cls(False, None, [reason])


class HaltReason(Exception):
    """Raised when a hard safety limit is breached. The loop shuts down."""


class RiskManager:
    def __init__(self, cfg: dict, fees: FeeModel):
        self.r = cfg.get("risk", {}) or {}
        self.e = cfg.get("edge", {}) or {}
        self.s = cfg.get("safety", {}) or {}
        self.fees = fees

    # ---------------- global circuit breakers ----------------
    def check_halt_conditions(self, state: RiskState) -> None:
        min_bal = float(self.s.get("min_account_balance_usd", 0))
        if state.balance_usd < min_bal:
            raise HaltReason(
                f"Account balance ${state.balance_usd:.2f} is below the configured "
                f"minimum of ${min_bal:.2f}. Stopping."
            )
        loss_limit = float(self.s.get("daily_loss_limit_usd", 1e9))
        if state.day_pnl_usd <= -abs(loss_limit):
            raise HaltReason(
                f"Daily loss limit hit: {state.day_pnl_usd:+.2f} USD vs limit "
                f"-{abs(loss_limit):.2f}. Stopping for the day."
            )
        max_losses = int(self.s.get("max_consecutive_losses", 10**6))
        if state.consecutive_losses >= max_losses:
            raise HaltReason(
                f"{state.consecutive_losses} losing trades in a row (limit {max_losses}). "
                "Stopping so you can review the model."
            )

    # ---------------- per-market gates ----------------
    def market_is_tradable(self, market: Market, book: Orderbook | None,
                           now: datetime | None = None) -> tuple[bool, str]:
        now = now or datetime.now(timezone.utc)
        if market.status and market.status.lower() not in ("open", "active"):
            return False, f"market status is {market.status!r}"

        mins = market.minutes_to_close(now)
        if mins < float(self.e.get("min_minutes_to_close", 0)):
            return False, f"closes in {mins:.0f} min (min {self.e.get('min_minutes_to_close')})"
        if mins > float(self.e.get("max_hours_to_close", 1e9)) * 60:
            return False, f"closes in {mins/60:.1f} h (max {self.e.get('max_hours_to_close')} h)"

        if not market.yes_ask or not market.yes_bid:
            return False, "one-sided or empty market"
        lo = int(self.e.get("min_price_cents", 1))
        hi = int(self.e.get("max_price_cents", 99))
        if not (lo <= market.yes_ask <= hi):
            return False, f"yes ask {market.yes_ask}c outside [{lo},{hi}]"
        if market.spread_cents > int(self.e.get("max_spread_cents", 99)):
            return False, f"spread {market.spread_cents}c too wide"
        return True, "ok"

    def liquidity_ok(self, book: Orderbook | None, side: str, price_cents: int,
                     wanted: int) -> tuple[bool, str]:
        need = int(self.e.get("min_resting_contracts_at_our_price", 0))
        if book is None:
            return (need == 0), "no orderbook available"
        depth = book.depth_for(side, "buy", price_cents)
        if depth < need:
            return False, f"only {depth} contracts offered at {price_cents}c (need {need})"
        if depth < wanted:
            return True, f"depth {depth} < wanted {wanted}; will size down"
        return True, "ok"

    # ---------------- sizing ----------------
    def size_order(self, intent: TradeIntent, state: RiskState,
                   book: Orderbook | None = None) -> RiskDecision:
        reasons: list[str] = []

        # --- entry-quality gates -----------------------------------
        if intent.edge_after_fees < float(self.e.get("min_edge_after_fees", 0.05)):
            return RiskDecision.reject(
                f"edge {intent.edge_after_fees:+.3f} below minimum "
                f"{self.e.get('min_edge_after_fees')}")
        if intent.confidence < float(self.e.get("min_confidence", 0.0)):
            return RiskDecision.reject(
                f"confidence {intent.confidence:.2f} below minimum {self.e.get('min_confidence')}")

        # --- rate limiting on our own activity ---------------------
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        recent = [t for t in state.order_timestamps if t > cutoff]
        if len(recent) >= int(self.r.get("max_orders_per_hour", 10**6)):
            return RiskDecision.reject(
                f"{len(recent)} orders in the last hour (limit "
                f"{self.r.get('max_orders_per_hour')})")

        # --- cooldown: don't re-buy the same market every single scan ---
        cooldown = float(self.e.get("min_minutes_between_entries_same_market", 0))
        last = state.last_order_by_ticker.get(intent.ticker)
        if cooldown and last:
            waited = (datetime.now(timezone.utc) - last).total_seconds() / 60.0
            if waited < cooldown:
                return RiskDecision.reject(
                    f"bought this market {waited:.0f} min ago "
                    f"(cooldown {cooldown:.0f} min)")

        # --- position count ----------------------------------------
        held = {t for t, p in state.positions.items() if p.contracts}
        if intent.ticker not in held and len(held) >= int(self.r.get("max_open_positions", 10**6)):
            return RiskDecision.reject(
                f"already holding {len(held)} positions (limit "
                f"{self.r.get('max_open_positions')})")

        # --- averaging down ----------------------------------------
        existing = state.positions.get(intent.ticker)
        if existing and existing.contracts:
            same_side = (existing.side == intent.side)
            if same_side and not self.r.get("allow_averaging_down", False):
                cur_price = intent.limit_price_cents
                if cur_price < existing.avg_cost_cents:
                    return RiskDecision.reject(
                        "would average down into a losing position "
                        "(risk.allow_averaging_down is false)")
            if not same_side:
                return RiskDecision.reject("already holding the opposite side of this market")

        # --- Kelly-based size, then clamp by every dollar limit -----
        win_prob = (intent.fair_probability if intent.side == "yes"
                    else 1.0 - intent.fair_probability)
        bankroll = float(self.r.get("bankroll_usd", 0))
        n = contracts_from_kelly(
            win_prob=win_prob,
            price_cents=intent.limit_price_cents,
            bankroll_usd=bankroll,
            kelly_multiplier=float(self.r.get("kelly_fraction", 0.25)),
            confidence=intent.confidence,
        )
        if n <= 0:
            return RiskDecision.reject("Kelly size rounds to zero contracts")
        reasons.append(f"kelly suggests {n}")

        price_usd = intent.limit_price_cents / 100.0

        def clamp(limit_usd: float, label: str, already: float = 0.0) -> int:
            room = max(0.0, limit_usd - already)
            allowed = int(math.floor(room / price_usd + 1e-9))
            if allowed < n:
                reasons.append(f"{label} caps to {allowed}")
            return allowed

        n = min(n, clamp(float(self.r.get("max_cost_per_trade_usd", 1e9)), "per-trade cap"))
        n = min(n, clamp(float(self.r.get("max_exposure_per_market_usd", 1e9)),
                         "per-market cap", state.exposure_in(intent.ticker)))
        n = min(n, clamp(float(self.r.get("max_total_open_exposure_usd", 1e9)),
                         "total exposure cap", state.open_exposure_usd))
        n = min(n, clamp(bankroll * float(self.r.get("max_fraction_of_bankroll_at_risk", 1.0)),
                         "bankroll-fraction cap"))
        n = min(n, clamp(max(0.0, state.balance_usd - float(self.s.get("min_account_balance_usd", 0))),
                         "available cash"))

        if n <= 0:
            return RiskDecision(False, None, reasons + ["all sizing caps reduced this to 0 contracts"])

        # --- liquidity -------------------------------------------------
        ok, why = self.liquidity_ok(book, intent.side, intent.limit_price_cents, n)
        if not ok:
            return RiskDecision(False, None, reasons + [why])
        if book is not None:
            depth = book.depth_for(intent.side, "buy", intent.limit_price_cents)
            if depth < n:
                n = depth
                reasons.append(f"sized down to available depth {n}")
        if n <= 0:
            return RiskDecision(False, None, reasons + ["no depth at our limit price"])

        cost = n * price_usd
        fee = self.fees.entry_fee_usd(intent.limit_price_cents, n)
        ev = ev_usd(win_prob, intent.limit_price_cents, n, self.fees)
        if ev <= 0:
            return RiskDecision(False, None, reasons + [f"EV after fees is {ev:+.2f} USD"])

        return RiskDecision(
            True,
            SizedOrder(intent=intent, contracts=n, est_cost_usd=cost,
                       est_fee_usd=fee, est_ev_usd=ev, notes=reasons),
            reasons,
        )
