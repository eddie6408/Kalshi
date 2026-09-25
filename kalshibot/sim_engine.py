"""The SIMULATOR's engine. Not a trading path.

RETIRED 2026-09-17 from the live side. This was the original single-estimator
loop - scan, estimate, size, execute, manage - and for a while it was the bot.
It is not any more. Everything it did now lives on the live path:

    scan_once / _consider   ->  strategies/forecast.py, run by arb_engine.py
    manage_positions/_close ->  position_manager.py, on its own 30s clock
    run_forever             ->  arb.py

Those replacements are strictly better: the forecast strategy has the tail
guards and the coherence check this loop never had, and the position worker
runs CONCURRENTLY with the scan instead of taking turns with it, and has six
exit rules where this has four.

What is left here is kept for one reason only: scripts/simulate.py drives it
against a synthetic exchange so you can watch the machinery work with no
account and no key (DIAGNOSTICS option 7). Retiring the file outright would
have deleted a working feature to tidy up an import.

Two rules keep this from ever becoming a second live path again:

  * Nothing outside scripts/simulate.py and its smoke test may import it.
  * It refuses to construct with a broker that can reach an exchange. See
    the guard in __init__ - it raises rather than warns.

If you find yourself wanting to add a feature here, it belongs on the live
path instead. See docs: claude/engine-retirement-log.md
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .client import KalshiClient, KalshiError
from .edge import evaluate
from .estimators.base import Estimate
from .execution import Broker, Fill
from .fees import FeeModel
from .journal import Journal
from .models import Market, Orderbook, Position, TradeIntent
from .risk import HaltReason, RiskManager, RiskState

log = logging.getLogger(__name__)


class RetiredEngineError(RuntimeError):
    """Raised when the retired engine is pointed at anything that can trade."""


def _refuse_live_broker(broker) -> None:
    """The retired engine may simulate. It may not reach an exchange.

    This is belt-and-braces over the production lockout, and it is a raise
    rather than a warning on purpose: a warning in a loop is a warning nobody
    reads. The live path has the tail guards, the coherence check and the
    position worker; this one does not, and must never be the thing that
    places an order.
    """
    name = type(broker).__name__
    if name != "PaperBroker" and not name.lower().startswith(("sim", "fake", "paper")):
        raise RetiredEngineError(
            f"{name} can reach an exchange. This engine is retired and runs "
            f"only against a simulated or paper broker - use arb.py to trade.")


@dataclass
class PaperPortfolio:
    """In-memory positions for paper mode, shaped like the API's."""
    positions: dict[str, Position] = field(default_factory=dict)
    trade_ids: dict[str, list[int]] = field(default_factory=dict)
    cash_spent_usd: float = 0.0

    def apply(self, fill: Fill, trade_id: int | None = None) -> None:
        p = self.positions.setdefault(fill.ticker, Position(ticker=fill.ticker))
        signed = fill.contracts if fill.side == "yes" else -fill.contracts
        if fill.action == "sell":
            signed = -signed
        p.contracts += signed
        p.total_cost_cents += fill.contracts * fill.price_cents
        self.cash_spent_usd += fill.contracts * fill.price_cents / 100.0 + fill.fee_usd
        if trade_id:
            self.trade_ids.setdefault(fill.ticker, []).append(trade_id)
        if p.contracts == 0:
            self.positions.pop(fill.ticker, None)
            self.trade_ids.pop(fill.ticker, None)


class Engine:
    def __init__(self, settings, client: KalshiClient | None, estimator, broker: Broker,
                 fees: FeeModel, risk: RiskManager, journal: Journal):
        self.s = settings
        self.client = client
        self.estimator = estimator
        self.broker = broker
        self.fees = fees
        self.risk = risk
        self.journal = journal
        self.mode = settings.mode
        self.paper = PaperPortfolio()
        _refuse_live_broker(broker)
        self.order_timestamps: list[datetime] = []
        self.last_order_by_ticker: dict[str, datetime] = {}
        self._book_cache: dict[str, tuple[float, Orderbook]] = {}
        self._forecast_logged: set[tuple] = set()
        self.counts: dict[str, int] = {}

    # ---------------------------------------------------------------
    def _orderbook(self, ticker: str, ttl: float = 20.0) -> Orderbook | None:
        if not self.client:
            return None
        hit = self._book_cache.get(ticker)
        if hit and (time.time() - hit[0]) < ttl:
            return hit[1]
        try:
            book = self.client.get_orderbook(ticker)
        except KalshiError as exc:
            log.warning("orderbook %s failed: %s", ticker, exc)
            return None
        self._book_cache[ticker] = (time.time(), book)
        return book

    def _state(self) -> RiskState:
        if self.mode == "paper":
            positions = dict(self.paper.positions)
            balance = float(self.risk.r.get("bankroll_usd", 0)) - self.paper.cash_spent_usd
        else:
            positions = {p.ticker: p for p in self.client.get_positions()}
            balance = self.client.get_balance_cents() / 100.0

        exposure = sum(p.total_cost_cents for p in positions.values()) / 100.0
        return RiskState(
            balance_usd=balance,
            positions=positions,
            open_exposure_usd=exposure,
            realized_pnl_today_usd=self.journal.realized_pnl_today_usd(),
            unrealized_pnl_usd=0.0,
            consecutive_losses=self.journal.consecutive_losses(),
            order_timestamps=list(self.order_timestamps),
            last_order_by_ticker=dict(self.last_order_by_ticker),
        )

    # ---------------------------------------------------------------
    def discover_markets(self) -> list[Market]:
        if not self.client:
            return []
        out: list[Market] = []
        for series in self.estimator.candidate_series():
            try:
                found = self.client.get_markets(series_ticker=series, status="open")
            except KalshiError as exc:
                log.warning("market lookup for %s failed: %s", series, exc)
                continue
            log.info("series %-12s -> %d open markets", series, len(found))
            out.extend(found)
        return out

    # ---------------------------------------------------------------
    def scan_once(self) -> int:
        started = time.time()
        state = self._state()
        self.risk.check_halt_conditions(state)

        markets = self.discover_markets()
        self.counts = {"seen": len(markets), "no_orderbook": 0, "wrong_window": 0,
                       "other_untradable": 0, "priced": 0, "edge_too_small": 0,
                       "risk_rejected": 0, "traded": 0}
        if not markets:
            log.info("no open markets found for the configured series")
            self.journal.log_scan(self.mode, self.counts, time.time() - started)
            return 0

        placed = 0
        for market in markets:
            try:
                placed += self._consider(market, state)
            except HaltReason:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("error while considering %s: %s", market.ticker, exc)

        c = self.counts
        self.journal.log_scan(self.mode, c, time.time() - started)
        log.info("scan complete in %.1fs: %d markets seen | %d no order book | "
                 "%d outside time window | %d other | %d priced -> "
                 "%d edge too small, %d risk rejected, %d TRADED",
                 time.time() - started, c["seen"], c["no_orderbook"],
                 c["wrong_window"], c["other_untradable"], c["priced"],
                 c["edge_too_small"], c["risk_rejected"], c["traded"])
        if c["seen"] and c["no_orderbook"] == c["seen"]:
            log.warning("Every market had an empty order book. Nobody is quoting "
                        "these contracts right now - there is literally nothing to "
                        "buy. This is normal on the demo exchange, which has very "
                        "few participants.")
        return placed

    def _record_forecast(self, market: Market, estimate: Estimate) -> None:
        """Save the raw forecast so scripts/settle.py can score it later."""
        ev = estimate.evidence or {}
        if "ensemble_mean_f" not in ev:
            return
        key = (market.series_ticker, ev.get("target_date"),
               datetime.now(timezone.utc).strftime("%Y%m%d%H"))
        if key in self._forecast_logged:
            return
        self._forecast_logged.add(key)
        self.journal.log_forecast(
            series=market.series_ticker, station=ev.get("station", ""),
            target_date=str(ev.get("target_date", "")),
            mean_f=float(ev.get("ensemble_mean_f", 0.0)),
            sigma_f=float(ev.get("sigma_used_f", 0.0)),
            members=int(ev.get("ensemble_members", 0)),
            lead_hours=float(ev.get("lead_hours", 0.0)),
        )

    def _consider(self, market: Market, state: RiskState) -> int:
        book = None
        ok, why = self.risk.market_is_tradable(market, book)
        if not ok:
            if "one-sided or empty" in why:
                self.counts["no_orderbook"] += 1
            elif "closes in" in why:
                self.counts["wrong_window"] += 1
            else:
                self.counts["other_untradable"] += 1
            log.debug("skip %s: %s", market.ticker, why)
            return 0
        self.counts["priced"] += 1

        estimate: Estimate = self.estimator.estimate(market)
        if not estimate.usable:
            log.debug("skip %s: %s", market.ticker, estimate.skip_reason)
            return 0

        self._record_forecast(market, estimate)
        edge = evaluate(estimate.probability_yes, market.yes_ask, market.no_ask, self.fees)
        implied = market.yes_ask / 100.0 if market.yes_ask else 0.0
        log.info(
            "%-28s ours=%.3f mkt(yes ask)=%.3f  best=%s@%dc edge=%+.3f conf=%.2f",
            market.ticker, estimate.probability_yes, implied,
            edge.side, edge.price_cents, edge.edge_after_fees, estimate.confidence,
        )

        if edge.edge_after_fees < float(self.risk.e.get("min_edge_after_fees", 0.05)):
            self.counts["edge_too_small"] += 1
            self.journal.log_decision(mode=self.mode, market=market, estimate=estimate,
                                      edge=edge, action="skipped",
                                      reason=f"edge {edge.edge_after_fees:+.3f} too small")
            return 0

        book = self._orderbook(market.ticker)
        intent = TradeIntent(
            ticker=market.ticker, side=edge.side, action="buy",
            limit_price_cents=edge.price_cents,
            fair_probability=estimate.probability_yes,
            confidence=estimate.confidence,
            edge_after_fees=edge.edge_after_fees,
            rationale=estimate.source,
            evidence=estimate.evidence,
        )
        decision = self.risk.size_order(intent, state, book)
        if not decision.approved or not decision.order:
            reason = "; ".join(decision.reasons) or "risk rejected"
            self.counts["risk_rejected"] += 1
            log.info("  -> no trade: %s", reason)
            self.journal.log_decision(mode=self.mode, market=market, estimate=estimate,
                                      edge=edge, action="rejected", reason=reason)
            return 0

        did = self.journal.log_decision(mode=self.mode, market=market, estimate=estimate,
                                        edge=edge, action="traded",
                                        reason="; ".join(decision.reasons),
                                        sized=decision.order)
        fill = self.broker.place(decision.order, book)
        if not fill.ok:
            log.warning("  -> order failed: %s", fill.error)
            return 0

        tid = self.journal.log_trade(
            decision_id=did, mode=self.mode, ticker=fill.ticker, side=fill.side,
            action=fill.action, contracts=fill.contracts, price_cents=fill.price_cents,
            fee_usd=fill.fee_usd, our_probability=estimate.probability_yes,
            order_id=fill.order_id, client_order_id=fill.client_order_id, status=fill.status,
        )
        if self.mode == "paper":
            self.paper.apply(fill, tid)

        now = datetime.now(timezone.utc)
        self.order_timestamps.append(now)
        self.last_order_by_ticker[fill.ticker] = now
        self.counts["traded"] += 1
        # refresh the live view so the next market in this scan sees the new exposure
        state.positions = self._state().positions
        state.open_exposure_usd = sum(p.total_cost_cents for p in state.positions.values()) / 100.0
        state.order_timestamps = list(self.order_timestamps)
        return 1

    # ---------------------------------------------------------------
    def manage_positions(self) -> None:
        """Re-ask the thesis question on everything we already hold."""
        state = self._state()
        if not state.positions:
            return
        ex = self.s.section("exit")
        for ticker, pos in list(state.positions.items()):
            if not pos.contracts:
                continue
            try:
                market = self.client.get_market(ticker) if self.client else None
            except (KalshiError, KeyError, StopIteration) as exc:
                # Market has settled or been delisted since we bought it.
                log.warning("could not refresh %s (%s); leaving it to settle",
                            ticker, exc)
                continue
            if market is None:
                continue

            estimate = self.estimator.estimate(market)
            mins = market.minutes_to_close()
            reason = None

            if mins <= float(ex.get("flatten_minutes_before_close", 0)):
                reason = f"settlement in {mins:.0f} min - flattening"
            elif estimate.usable:
                held_side = pos.side
                win_prob = (estimate.probability_yes if held_side == "yes"
                            else 1.0 - estimate.probability_yes)
                exit_px = market.yes_bid if held_side == "yes" else market.no_bid
                if exit_px:
                    remaining = win_prob - exit_px / 100.0
                    loss_frac = 1.0 - (exit_px / max(1.0, pos.avg_cost_cents))
                    if remaining < float(ex.get("exit_when_edge_below", -1)):
                        reason = f"thesis gone: remaining edge {remaining:+.3f}"
                    elif remaining <= float(ex.get("take_profit_edge_remaining", -1)) and exit_px > pos.avg_cost_cents:
                        reason = f"target reached: remaining edge {remaining:+.3f}"
                    elif loss_frac >= float(ex.get("stop_loss_fraction", 2)):
                        reason = f"stop loss: down {loss_frac*100:.0f}%"

            if reason:
                log.info("EXIT %s (%s)", ticker, reason)
                self._close(pos, market, reason)

    def _close(self, pos: Position, market: Market, reason: str) -> None:
        side = pos.side
        price = market.yes_bid if side == "yes" else market.no_bid
        if not price:
            log.warning("cannot exit %s: no bid", pos.ticker)
            return
        intent = TradeIntent(ticker=pos.ticker, side=side, action="sell",
                             limit_price_cents=price, fair_probability=0.0,
                             confidence=1.0, edge_after_fees=0.0, rationale=reason)
        from .models import SizedOrder
        order = SizedOrder(intent=intent, contracts=abs(pos.contracts),
                           est_cost_usd=0.0, est_fee_usd=0.0, est_ev_usd=0.0,
                           notes=[reason])
        fill = self.broker.place(order, self._orderbook(pos.ticker))
        if not fill.ok:
            log.warning("exit order failed for %s: %s", pos.ticker, fill.error)
            return
        yes_exit = market.yes_bid if side == "yes" else (100 - market.no_bid)
        no_exit = market.no_bid if side == "no" else (100 - market.yes_bid)
        n_closed, pnl = self.journal.close_open_trades(pos.ticker, yes_exit, no_exit)
        if self.mode == "paper":
            self.paper.apply(fill)
        log.info("closed %s (%d entr%s) for P/L $%+.2f (%s)",
                 pos.ticker, n_closed, "y" if n_closed == 1 else "ies", pnl, reason)

    # ---------------------------------------------------------------
    def run_forever(self) -> None:
        loop = self.s.section("loop")
        scan_every = int(loop.get("scan_interval_seconds", 300))
        manage_every = int(loop.get("manage_interval_seconds", 60))
        active_hours = loop.get("active_utc_hours") or []

        run_id = self.journal.start_run(self.mode, note=f"env={self.s.kalshi_env}")
        log.info("bot started | mode=%s | env=%s | strategy=%s",
                 self.mode, self.s.kalshi_env, self.estimator.name)
        last_scan = 0.0
        try:
            while True:
                now = datetime.now(timezone.utc)
                if active_hours and now.hour not in active_hours:
                    log.debug("outside active hours, sleeping")
                    time.sleep(manage_every)
                    continue
                try:
                    self.manage_positions()
                    if time.time() - last_scan >= scan_every:
                        self.scan_once()
                        last_scan = time.time()
                except HaltReason as halt:
                    log.error("HALT: %s", halt)
                    self.broker.cancel_all()
                    break
                except KalshiError as exc:
                    log.error("Kalshi API error, will retry: %s", exc)
                time.sleep(manage_every)
        except KeyboardInterrupt:
            log.info("stopped by user")
        finally:
            self.journal.end_run(run_id)
