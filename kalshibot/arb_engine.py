"""The structural arbitrage loop.

    discover -> price books -> scan -> gate -> risk -> execute -> reconcile

Three modes, set by TRADING_MODE:

    SCAN_ONLY   find and report. Never sends anything. The default.
    PAPER       simulate fills against the live demo book. No orders.
    DEMO_LIVE   actually submit orders to Kalshi demo. Play money.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from .depth import DepthBook
from .discovery import DiscoveryStats, MarketDiscovery
from .execution_manager import ExecutionManager, TradeOutcome, TradeResult
from .gate import ProfitabilityGate
from .liquidity import LiquidityProfile
from .opportunity import ArbitrageOpportunity
from .strategies.arbitrage import StructuralArbitrageScanner
from .calibration_gate import CalibrationConfig, CalibrationGate
from .settlement import settle_forecasts
from .strategies.forecast import ForecastConfig, ForecastStrategy

from .data_gates import GateConfig
from .alerts import ALERTS, today_key
from .sources import MONITOR

log = logging.getLogger(__name__)


class TradingMode(str, Enum):
    SCAN_ONLY = "SCAN_ONLY"
    PAPER = "PAPER"
    DEMO_LIVE = "DEMO_LIVE"


@dataclass
class ScanReport:
    started: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    seconds: float = 0.0
    discovery: DiscoveryStats = field(default_factory=DiscoveryStats)
    books_fetched: int = 0
    markets_with_depth: int = 0
    book_formats: dict = field(default_factory=dict)
    liquid_series: list = field(default_factory=list)
    events_inspected: int = 0
    relationships: int = 0
    opportunities: list[ArbitrageOpportunity] = field(default_factory=list)
    accepted: list[ArbitrageOpportunity] = field(default_factory=list)
    rejections: dict[str, int] = field(default_factory=dict)
    #: structures that were real but not crossed, tightest gap first
    near_misses: list = field(default_factory=list)
    near_miss_count: int = 0
    forecast_errors: int = 0
    forecast_skips: list = field(default_factory=list)
    forecast_considered: int = 0
    suspended: dict = field(default_factory=dict)
    trades: list[TradeResult] = field(default_factory=list)

    @property
    def qualifying(self) -> int:
        return len(self.accepted)


@dataclass
class RiskLimits:
    max_total_exposure_usd: float = 100.0
    max_event_exposure_usd: float = 25.0
    max_market_exposure_usd: float = 10.0
    max_contracts_per_trade: int = 1
    max_legs_per_trade: int = 14
    max_trades_per_scan: int = 1
    max_trades_per_hour: int = 10
    #: Adding to a position we already hold, on the same side. False means one
    #: bite per market per side - the "no doubling down" rule.
    allow_averaging_in: bool = False
    #: Minutes before a market we have just left may be entered again. The
    #: forecast that said "buy" will still say "buy" the moment the position
    #: is gone, so without this the bot re-buys what it just sold. On
    #: 19 September it bought KXHIGHCHI-26SEP19-T72 back four times, paying
    #: the 69-cent spread each way, every time.
    reentry_cooldown_minutes: float = 120.0

    @classmethod
    def from_dict(cls, d: dict | None) -> "RiskLimits":
        d = d or {}
        return cls(
            max_total_exposure_usd=float(d.get("max_total_exposure_usd", 100.0)),
            max_event_exposure_usd=float(d.get("max_event_exposure_usd", 25.0)),
            max_market_exposure_usd=float(d.get("max_market_exposure_usd", 10.0)),
            max_contracts_per_trade=int(d.get("max_contracts_per_trade", 1)),
            max_legs_per_trade=int(d.get("max_legs_per_trade", 14)),
            max_trades_per_scan=int(d.get("max_trades_per_scan", 1)),
            max_trades_per_hour=int(d.get("max_trades_per_hour", 10)),
            allow_averaging_in=bool(d.get("allow_averaging_in", False)),
            reentry_cooldown_minutes=float(
                d.get("reentry_cooldown_minutes", 120.0)),
        )


class ArbitrageEngine:
    #: Circuit-breaker state, as CLASS defaults.
    #:
    #: Several tests build an engine with __new__ and set only the attributes
    #: the code under test needs, which drifts from __init__ every time a new
    #: one appears. For state that is immutable and has an obvious safe
    #: default, a class attribute means a half-built engine behaves like a
    #: healthy one rather than raising AttributeError deep inside a risk
    #: check. Mutable state stays in __init__, where it cannot be shared.
    _halted = False
    _halt_reason = ""

    def __init__(self, settings, market_client, portfolio_client,
                 execution_client, fees, journal, mode: TradingMode):
        self.s = settings
        self.market = market_client
        self.portfolio = portfolio_client
        self.exec_client = execution_client
        self.fees = fees
        self.journal = journal
        self.mode = mode

        self.discovery = MarketDiscovery(market_client, settings.section("discovery"))
        self.book_budget = int(settings.get("discovery.max_orderbook_fetches", 1500))
        self.book_workers = int(settings.get("discovery.orderbook_workers", 8))
        #: never let one giant event eat the whole order book budget
        self.max_books_per_event = int(
            settings.get("discovery.max_books_per_event", 12))
        #: and no single SERIES may dominate either
        self.max_books_per_series = int(
            settings.get("discovery.max_books_per_series", 60))
        #: fraction of the budget held back for series we have never sampled,
        #: so the scan keeps discovering instead of only re-checking winners
        self.explore_fraction = float(
            settings.get("discovery.explore_fraction", 0.25))
        self.liquidity = LiquidityProfile(
            settings.path("discovery.liquidity_profile", "data/liquid_series.json")
            if hasattr(settings, "path") else "data/liquid_series.json")
        self.scanner = StructuralArbitrageScanner(fees, settings.section("arbitrage"))
        #: Forecast strategies run in the SAME loop, through the same gate,
        #: the same risk limits and the same execution manager. They are what
        #: makes this a trading bot rather than an arbitrage scanner.
        self.forecasts: list[ForecastStrategy] = self._build_forecasts(
            fees, settings, journal)
        self.bankroll_usd = 0.0
        self.gate = ProfitabilityGate(settings.section("profitability"))
        self.limits = RiskLimits.from_dict(settings.section("limits"))
        self.exposure_usd = 0.0
        #: what we hold right now, re-read at the start of every pass
        self._positions: dict = {}
        self._positions_unknown = False
        #: cost committed during THIS pass but not yet visible in positions
        self._pending_cost: dict[tuple[str, str], float] = {}
        #: what our own fills say we hold, cached for the length of one pass
        self._fill_holdings: dict[tuple[str, str], tuple[int, float] | None] = {}
        #: set once a circuit breaker trips; only a restart clears it
        self._halted = False
        self._halt_reason = ""
        #: Guards the shared risk state above. Once each strategy has its own
        #: worker, two passes read and write `_positions` and `_pending_cost`
        #: at the same time - and those two are exactly what stops the bot
        #: buying the same market twice. A race there is not a glitch, it is
        #: the per-market limit quietly not applying.
        self._risk_lock = threading.RLock()
        self.trade_times: list[datetime] = []
        #: Shared with the position worker so the scan and the exit path never
        #: trade the same ticker at the same moment.
        self.lock = threading.Lock()
        self.position_worker = None
        #: Calibration decides whether a strategy may still OPEN positions.
        #: It never decides whether one may close them.
        self.calibration = CalibrationGate(
            journal, CalibrationConfig.from_dict(settings.section("calibration")))

        self.manager = None
        if mode is TradingMode.DEMO_LIVE and execution_client is not None:
            self.manager = ExecutionManager(
                execution_client, portfolio_client, market_client,
                self.gate, fees, journal, settings.section("execution"))

    @staticmethod
    def _build_forecasts(fees, settings, journal=None) -> list[ForecastStrategy]:
        """Instantiate the estimators named in config. An estimator that fails
        to construct is logged and skipped - a broken data source must not
        stop structural arbitrage from running."""
        section = settings.section("forecast")
        if not section.get("enabled", False):
            return []
        cfg = ForecastConfig.from_dict(section)
        out: list[ForecastStrategy] = []
        from .estimators.registry import ESTIMATORS
        for name in section.get("estimators", []) or []:
            factory = ESTIMATORS.get(name)
            if factory is None:
                log.error("FORECAST  unknown estimator %r; known: %s",
                          name, sorted(ESTIMATORS))
                continue
            try:
                out.append(ForecastStrategy(
                    factory(settings), fees, cfg, journal=journal,
                    gates=GateConfig.from_dict(settings.get("sources.gates")),
                    monitor=MONITOR))
                log.info("FORECAST  %s registered", name)
            except Exception as exc:  # noqa: BLE001
                log.error("FORECAST  %s failed to start: %s", name, exc)
        return out

    # ==============================================================
    def scan_once(self) -> ScanReport:
        t0 = time.time()
        report = ScanReport()

        # Which strategies have lost the right to open new positions? Asked
        # once per scan rather than per market: Brier scores move slowly and
        # this reads the database.
        suspended = self._refresh_suspensions(report)
        self._refresh_bankroll()
        self._refresh_positions()

        events, stats = self.discovery.discover()
        report.discovery = stats
        log.info("DISCOVERY  %s", stats.summary())
        if not events:
            report.seconds = time.time() - t0
            return report

        # Anything not currently listed on the exchange has no business
        # steering the budget - including test fixtures.
        live_series = {ev.series_ticker or ev.event_ticker.split("-")[0]
                       for ev in events}
        dropped = self.liquidity.prune_to(live_series)
        if dropped:
            log.info("LIQUIDITY  dropped %d series no longer on the exchange",
                     dropped)

        # Spend the order book budget where books were actually found before.
        # Priority, in order:
        #   1. series where books were actually found before
        #   2. exchange-confirmed mutually exclusive (basket candidates)
        #   3. ACTUAL TRADED ACTIVITY, not event size
        #
        # Sorting by event size was actively counterproductive: the biggest
        # events on this exchange are sprawling, never-traded blobs, and they
        # swallowed the entire budget - 1,500 order books across 4 events,
        # all empty.
        # A forecast strategy that never gets an order book is a strategy
        # that never trades. The series its estimators know how to price are
        # sorted to the very front, ahead of the liquidity ranking, so the
        # budget always reaches them.
        wanted: set[str] = set()
        for strategy in self.forecasts:
            wanted.update(strategy.candidate_series())
        if wanted:
            log.info("FORECAST  prioritising %d series: %s",
                     len(wanted), ", ".join(sorted(wanted)[:8]))

        def _series_of(e) -> str:
            return e.series_ticker or e.event_ticker.split("-")[0]

        def _forecast_rank(e) -> int:
            series = _series_of(e)
            return 0 if any(series.startswith(w) for w in wanted) else 1

        events.sort(key=lambda e: (
            _forecast_rank(e),
            -self.liquidity.score(_series_of(e)),
            not e.mutually_exclusive,
            -e.activity,
            e.minutes_to_close()))
        log.info("LIQUIDITY  %s", self.liquidity.summary())

        known = set(self.liquidity.known_liquid())
        dead = set(self.liquidity.known_dead())
        if known:
            log.info("LIQUIDITY  %d proven series, %d written off, exploring "
                     "%.0f%% of the budget", len(known), len(dead),
                     self.explore_fraction * 100)

        explore_budget = int(self.book_budget * self.explore_fraction)
        exploit_budget = self.book_budget - explore_budget
        budget = self.book_budget
        per_series_spent: dict[str, int] = {}
        for ev in events:
            if budget <= 0:
                log.info("order book budget of %d exhausted; %d events not "
                         "inspected this scan", self.book_budget,
                         len(events) - events.index(ev))
                break
            # Take the most active markets in this event, capped, so the
            # budget reaches many events instead of drowning in one.
            series = ev.series_ticker or ev.event_ticker.split("-")[0]
            # A series we have sampled repeatedly and never found a book in
            # does not deserve more of the budget.
            is_wanted = bool(wanted) and any(series.startswith(w) for w in wanted)
            if series in dead and not is_wanted:
                continue
            # A series an estimator can price is spent from the exploit
            # budget regardless of its book history - we are not sampling it
            # speculatively, we have a reason to look.
            is_known = is_wanted or series in known
            if is_known and exploit_budget <= 0:
                continue
            if not is_known and explore_budget <= 0:
                continue
            spent = per_series_spent.get(series, 0)
            room = max(0, self.max_books_per_series - spent)
            if room == 0:
                continue
            take = min(self.max_books_per_event, budget, ev.n_markets, room)
            if take <= 0:
                continue
            take = min(take, exploit_budget if is_known else explore_budget)
            if take <= 0:
                continue
            tickers = [m.ticker for m in ev.most_active(take)]
            per_series_spent[series] = spent + len(tickers)
            if is_known:
                exploit_budget -= len(tickers)
            else:
                explore_budget -= len(tickers)
            books = self.market.get_depths(tickers, workers=self.book_workers)
            budget -= len(tickers)
            report.books_fetched += len(books)
            report.events_inspected += 1
            for b in books.values():
                fmt = getattr(b, "source_format", "unknown")
                report.book_formats[fmt] = report.book_formats.get(fmt, 0) + 1
            live = {t: b for t, b in books.items()
                    if b.best_price("yes", "buy") or b.best_price("no", "buy")}
            report.markets_with_depth += len(live)
            self.liquidity.record(ev.series_ticker or ev.event_ticker.split("-")[0],
                                  len(books), len(live))
            if not live:
                continue
            report.relationships += self._count_relationships(ev, live)

            self.scanner.reset_near_misses()
            found = self.scanner.scan_event(ev.event_ticker, ev.markets, live,
                                            ev.mutually_exclusive)
            report.near_miss_count += self.scanner.near_miss_count()
            report.near_misses.extend(self.scanner.near_misses(5))

            for strategy in self.forecasts:
                if strategy.name in suspended:
                    continue
                try:
                    found += strategy.find(ev.markets, live, self.bankroll_usd)
                except Exception as exc:  # noqa: BLE001
                    # A data source that times out or changes shape must never
                    # take the whole scan down with it.
                    log.error("FORECAST  %s failed on %s: %s",
                              strategy.name, ev.event_ticker, exc)
                    report.forecast_errors += 1
            for op in found:
                report.opportunities.append(op)
                self._judge(op, report)

        for strategy in self.forecasts:
            report.forecast_considered += len(strategy.skips)
            report.forecast_skips.extend(strategy.skips[:5])
            strategy.skips = []
        report.near_misses.sort(key=lambda nm: nm.gap_cents)
        del report.near_misses[10:]
        report.accepted.sort(key=lambda o: o.net_edge, reverse=True)
        if self.mode is TradingMode.DEMO_LIVE and report.accepted:
            self._trade(report)
        elif self.mode is TradingMode.PAPER and report.accepted:
            self._paper(report)

        self.liquidity.save()
        report.liquid_series = self.liquidity.known_liquid()
        report.seconds = time.time() - t0
        self._log_scan(report)
        return report

    # ==============================================================
    def scan_focused(self, series: list[str] | None = None,
                     strategies=None) -> ScanReport:
        """Price only the series a forecast strategy can actually price.

        `scan_once` sweeps the whole exchange because structural arbitrage
        does not know in advance where a mispricing will be. A forecast
        strategy is the opposite: it can name its six series. Making it wait
        on the sweep meant the first live run spent twenty minutes paging
        eighty thousand markets to reach four hundred it knew the tickers of,
        and produced a scan that took 2.8 hours.

        Everything downstream is the SAME path - the same gate, the same risk
        limits, the same execution manager, the same lock. Only the way the
        markets are found is different. That matters: two ways to find a
        market is fine, two ways to approve a trade is how a limit gets
        bypassed.
        """
        t0 = time.time()
        report = ScanReport()
        # A worker is given ITS strategies, not all of them. Otherwise the
        # rates worker queues behind the weather worker and the two are
        # parallel in name only.
        mine = list(self.forecasts if strategies is None else strategies)
        wanted = list(series or sorted(
            {w for st in mine for w in st.candidate_series()}))
        if not wanted or not mine:
            report.seconds = time.time() - t0
            return report

        suspended = self._refresh_suspensions(report, mine)
        self._refresh_bankroll()
        self._refresh_positions()

        events, stats = self.discovery.discover_series(wanted)
        report.discovery = stats
        log.info("FOCUSED  %d series -> %d events / %d markets (%.1fs)",
                 len(wanted), len(events), stats.markets_kept, time.time() - t0)

        busy = self._busy_tickers()
        for ev in events:
            tickers = [m.ticker for m in ev.markets if m.ticker not in busy]
            if not tickers:
                continue
            books = self.market.get_depths(tickers, workers=self.book_workers)
            report.books_fetched += len(books)
            report.events_inspected += 1
            live = {t: b for t, b in books.items()
                    if b.best_price("yes", "buy") or b.best_price("no", "buy")}
            report.markets_with_depth += len(live)
            if not live:
                continue

            found = []
            for strategy in mine:
                if strategy.name in suspended:
                    continue
                try:
                    found += strategy.find(ev.markets, live, self.bankroll_usd)
                except Exception as exc:  # noqa: BLE001
                    # A data source that times out or changes shape must never
                    # take the whole pass down with it.
                    log.error("FORECAST  %s failed on %s: %s",
                              strategy.name, ev.event_ticker, exc)
                    report.forecast_errors += 1
            for op in found:
                report.opportunities.append(op)
                self._judge(op, report)

        for strategy in mine:
            report.forecast_considered += len(strategy.skips)
            report.forecast_skips.extend(strategy.skips[:5])
            strategy.skips = []

        report.accepted.sort(key=lambda o: o.net_edge, reverse=True)
        if self.mode is TradingMode.DEMO_LIVE and report.accepted:
            self._trade(report)
        elif self.mode is TradingMode.PAPER and report.accepted:
            self._paper(report)
        report.seconds = time.time() - t0
        self._log_scan(report)
        return report

    # --------------------------------------------------------------
    def _refresh_suspensions(self, report: ScanReport,
                            strategies=None) -> set[str]:
        """Which strategies have lost the right to open new positions?

        Returned rather than stored: with a worker per strategy, two passes
        overlap, and one pass must never overwrite the other's answer.
        """
        suspended: set[str] = set()
        for strategy in (self.forecasts if strategies is None else strategies):
            ok, why = self.calibration.may_enter(strategy.name)
            if not ok:
                suspended.add(strategy.name)
                log.warning("CALIBRATION  %s SUSPENDED from new entries: %s",
                            strategy.name, why)
                ALERTS.send(today_key("calibration", strategy.name),
                            f"{strategy.name} suspended",
                            f"Calibration gate stopped new entries: {why}",
                            priority="high", tags="pause_button")
                report.suspended[strategy.name] = why
        self._suspended = suspended          # kept for anything still reading it
        return suspended

    def strategy_named(self, name: str):
        """One strategy by name, for a worker that owns only that one."""
        return next((s for s in self.forecasts if s.name == name), None)

    def _refresh_bankroll(self) -> None:
        """Forecast sizing is a fraction of the bankroll, so it must be live.

        A stale balance sizes trades against money that is already committed.
        """
        if not (self.forecasts and self.portfolio is not None):
            return
        try:
            self.bankroll_usd = self.portfolio.balance_usd()
        except Exception as exc:  # noqa: BLE001
            log.error("FORECAST  could not read balance, forecasts sit this "
                      "pass out: %s", exc)
            self.bankroll_usd = 0.0

    # ==============================================================
    def _count_relationships(self, ev, books) -> int:
        """How many structural relationships exist here at all, priced or not."""
        n = len(books)
        rel = n                                      # each market is its own YES/NO pair
        rungs = sum(1 for m in ev.markets
                    if self.scanner.ladder.threshold_of(m) is not None
                    and m.ticker in books)
        rel += rungs * (rungs - 1) // 2
        if ev.mutually_exclusive and n >= 2:
            rel += 1
        return rel

    def _judge(self, op: ArbitrageOpportunity, report: ScanReport) -> None:
        decision = self.gate.evaluate(op)
        if self.journal:
            self.journal.log_opportunity(self.mode.value, op, decision)

        if decision.accepted:
            ok, why = self._risk_ok(op)
            if not ok:
                report.rejections[f"RISK:{why}"] = report.rejections.get(
                    f"RISK:{why}", 0) + 1
                log.info("ARBITRAGE REJECTED  %s | risk: %s", op.summary(), why)
                return
            report.accepted.append(op)
            log.info("ARBITRAGE FOUND\n%s", op.explain())
        else:
            key = decision.reason.value
            report.rejections[key] = report.rejections.get(key, 0) + 1
            log.info("ARBITRAGE REJECTED  %s | %s", op.summary(), decision)

    def _breakers_ok(self) -> tuple[bool, str]:
        """The account-level circuit breakers, checked before every entry.

        These were configured from the start - daily_loss_limit_usd,
        min_account_balance_usd, max_consecutive_losses - and the only code
        that read them was `sim_engine.check_halt_conditions`, the OFFLINE
        simulator. On the live path nothing called it, so all three were
        limit-shaped comments. That is the third set found in this codebase,
        after max_unhedged_exposure_usd and max_market_exposure_usd, and it
        is the one that matters most for a long unattended run: until now
        there was no circuit breaker of any kind.

        Tripping stops the bot OPENING positions. It deliberately does not
        stop it managing the ones it has, and it does not kill the process:
        a bot that exits leaving open risk unattended is worse than one that
        stops buying. Entries stay closed until it is restarted.
        """
        if self._halted:
            return False, f"circuit breaker: {self._halt_reason}"
        if self.journal is None:
            return True, "ok"

        s = self.s.section("safety") or {}
        try:
            min_balance = float(s.get("min_account_balance_usd", 0) or 0)
            if min_balance and self.portfolio is not None:
                balance = self.portfolio.balance_usd()
                if balance < min_balance:
                    return self._halt(
                        f"account balance ${balance:.2f} is below the "
                        f"${min_balance:.2f} minimum")

            loss_limit = float(s.get("daily_loss_limit_usd", 0) or 0)
            if loss_limit:
                midnight = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00")
                today = self.journal.realised_pnl(since=midnight)
                if today <= -abs(loss_limit):
                    return self._halt(
                        f"today's realised P/L is ${today:+.2f}, past the "
                        f"${-abs(loss_limit):.2f} daily loss limit")

            max_losses = int(s.get("max_consecutive_losses", 0) or 0)
            if max_losses:
                run = self.journal.consecutive_losses()
                if run >= max_losses:
                    return self._halt(
                        f"{run} losing positions in a row, limit {max_losses}")
        except Exception as exc:  # noqa: BLE001
            # A breaker we cannot evaluate must not silently pass. Say so and
            # keep trading - failing closed on a database hiccup would stop
            # the bot for the wrong reason - but never pretend it was checked.
            log.error("RISK  could not evaluate the circuit breakers: %s", exc)
        return True, "ok"

    def _halt(self, why: str) -> tuple[bool, str]:
        self._halted = True
        self._halt_reason = why
        log.error("RISK  CIRCUIT BREAKER  %s. No new positions will be opened "
                  "until the bot is restarted (a consecutive-loss trip also "
                  "needs scripts/reset_breaker.py first). Existing positions "
                  "are still being managed.", why)
        ALERTS.send(today_key("circuit_breaker"), "Circuit breaker tripped",
                    f"{why}. No new positions until you review it and restart "
                    f"(START BOT (VPS) offers the reset). Existing positions "
                    f"are still managed.",
                    priority="urgent", tags="rotating_light")
        return False, f"circuit breaker: {why}"

    def _risk_ok(self, op: ArbitrageOpportunity) -> tuple[bool, str]:
        ok, why = self._breakers_ok()
        if not ok:
            return False, why
        if op.quantity > self.limits.max_contracts_per_trade:
            return False, (f"{op.quantity} contracts > limit "
                           f"{self.limits.max_contracts_per_trade}")
        if op.n_legs > self.limits.max_legs_per_trade:
            return False, f"{op.n_legs} legs > limit {self.limits.max_legs_per_trade}"
        if op.gross_cost > self.limits.max_event_exposure_usd:
            return False, (f"${op.gross_cost:.2f} > per-event limit "
                           f"${self.limits.max_event_exposure_usd:.2f}")
        if self.exposure_usd + op.gross_cost > self.limits.max_total_exposure_usd:
            return False, (f"total exposure would reach "
                           f"${self.exposure_usd + op.gross_cost:.2f} > "
                           f"${self.limits.max_total_exposure_usd:.2f}")
        cutoff = datetime.now(timezone.utc).timestamp() - 3600
        recent = [t for t in self.trade_times if t.timestamp() > cutoff]
        if len(recent) >= self.limits.max_trades_per_hour:
            return False, f"{len(recent)} trades in the last hour"

        # One worker per strategy means two passes can ask this at the same
        # moment. Reading what we hold, deciding, and (after a fill)
        # remembering what we committed have to be one indivisible step -
        # otherwise both passes see the same empty allowance and both buy.
        with self._risk_lock:
            ok, why = self._market_exposure_ok(op)
            if not ok:
                return False, why
            ok, why = self._reentry_ok(op)
            if not ok:
                return False, why
            return True, "ok"

    def _reentry_ok(self, op: ArbitrageOpportunity) -> tuple[bool, str]:
        """Have we just been in this market?

        A forecast does not change because a position closed. The estimator
        that said "buy NO at 83c" says it again thirty seconds later, so the
        bot buys back what it just sold and pays the spread a second time.
        That is not a strategy, it is a fee generator: on 19 September one
        market was entered four times and lost $24, while every other trade
        that day was net positive.
        """
        if self.limits.reentry_cooldown_minutes <= 0 or self.journal is None:
            return True, "ok"
        now = datetime.now(timezone.utc)
        for ticker in op.market_tickers:
            try:
                raw = self.journal.last_exit_at(ticker)
            except Exception as exc:  # noqa: BLE001
                log.debug("RISK  no exit history for %s: %s", ticker, exc)
                continue
            if not raw:
                continue
            try:
                left = datetime.fromisoformat(raw)
            except ValueError:
                continue
            if left.tzinfo is None:
                left = left.replace(tzinfo=timezone.utc)
            mins = (now - left).total_seconds() / 60.0
            if mins < self.limits.reentry_cooldown_minutes:
                return False, (f"left {ticker} {mins:.0f} min ago; "
                               f"cooling off for "
                               f"{self.limits.reentry_cooldown_minutes:.0f} min")
        return True, "ok"

    def _market_exposure_ok(self, op: ArbitrageOpportunity) -> tuple[bool, str]:  # noqa: D401
        """What we ALREADY hold in this market, plus what this would add.

        Every other limit here is per-trade or per-event, so two separate
        trades in the same market each passed on their own. On 18 September the
        bot bought Denver T82 fourteen contracts at 63c, and then bought
        fourteen more at 63c twenty minutes later - 28 contracts in a market
        the spec caps, and nothing said a word.

        `max_market_exposure_usd` was declared in RiskLimits, parsed from
        config, and never read by anything. Dead config is not a limit; it is
        a limit-shaped comment. This is the second one of those found in this
        codebase, after max_unhedged_exposure_usd.
        """
        if self._positions_unknown:
            return False, ("cannot read current positions, so the per-market "
                           "limit cannot be enforced")
        for leg in op.legs:
            ticker = leg.ticker
            side = getattr(leg, "side", "")
            cost = getattr(leg, "cost_usd", 0.0) or (
                leg.contracts * leg.limit_price_cents / 100.0)

            held, held_cost = self._holding(ticker, side)
            pending = self._pending_cost.get((ticker, side), 0.0)

            # 1. No doubling down. Adding to a position we already hold, on
            #    the same side, is a separate decision from opening one - and
            #    it is the decision that turns a bad forecast into a big bad
            #    forecast.
            if held > 0 and not self.limits.allow_averaging_in:
                return False, (f"already hold {held} {side.upper()} in "
                               f"{ticker}; averaging in is off")
            if pending > 0 and not self.limits.allow_averaging_in:
                return False, (f"already buying {side.upper()} in {ticker} "
                               f"this pass; averaging in is off")

            # 2. And whatever the answer to that, the money in one market is
            #    capped.
            total = held_cost + pending + cost
            if total > self.limits.max_market_exposure_usd:
                return False, (f"{ticker} would reach ${total:.2f} "
                               f"(${held_cost:.2f} held + ${pending:.2f} "
                               f"pending + ${cost:.2f} new) > per-market limit "
                               f"${self.limits.max_market_exposure_usd:.2f}")
        return True, "ok"

    def _holding(self, ticker: str, side: str) -> tuple[int, float]:
        """Contracts and dollars we hold in one market on one side.

        Two sources, and we take the LARGER.

        The exchange is authoritative about what exists and stale about what
        just changed; our fill record is the reverse. The exit clamp takes the
        smaller of the two, because selling more than we hold opens a short.
        A spending limit is the mirror image: the danger is holding MORE than
        we think, so the conservative answer is the bigger number.

        Without the fill record this limit only worked inside a single pass.
        `_pending_cost` is wiped by `_refresh_positions` at the top of every
        pass, so a second pass arriving before the exchange caught up saw
        nothing held and nothing pending, and bought again. That is Denver on
        19 September - 25 contracts at 16:25, 25 more at 16:26, one minute and
        one pass apart - and it cost $10.00 when the line settled worthless.
        """
        exchange = (0, 0.0)
        pos = self._positions.get(ticker)
        if pos is not None and pos.contracts and (not side or pos.side == side):
            exchange = (abs(pos.contracts), abs(pos.total_cost_cents) / 100.0)

        ours = self._holding_from_fills(ticker, side)
        if ours is None:
            return exchange
        return exchange if exchange[0] >= ours[0] else ours

    def _holding_from_fills(self, ticker: str, side: str
                            ) -> tuple[int, float] | None:
        """What OUR OWN fills say is open here. None means no record.

        None rather than zero, for the same reason as the exit clamp: a
        journal that has never seen this market must not get a vote, or a
        position opened by hand would look like room to spend.
        """
        if self.journal is None or not hasattr(self.journal, "entry_for"):
            return None
        key = (ticker, side)
        if key in self._fill_holdings:
            return self._fill_holdings[key]
        answer: tuple[int, float] | None = None
        try:
            held, avg_cents = self.journal.entry_for(ticker, side)
            if held > 0:
                answer = (int(held), int(held) * float(avg_cents or 0) / 100.0)
            elif avg_cents is not None:
                answer = (0, 0.0)          # we have a record, and it is flat
        except Exception as exc:  # noqa: BLE001
            # A ledger we cannot read is not a ledger that says zero.
            log.debug("RISK  no fill history for %s %s: %s", ticker, side, exc)
        self._fill_holdings[key] = answer
        return answer

    def _refresh_positions(self) -> None:
        """Re-read what we hold. Once per pass, before anything is judged.

        A stale position list is how the per-market limit gets bypassed: the
        second buy looks like the first one.
        """
        with self._risk_lock:
            self._pending_cost = {}
            # Re-read the fill record too. It is the half of the answer that
            # is current, and caching it for one pass keeps a SQLite hit off
            # every leg of every opportunity.
            self._fill_holdings = {}
        if self.portfolio is None:
            return
        try:
            fresh = {p.ticker: p for p in self.portfolio.positions()
                     if p.contracts}
        except Exception as exc:  # noqa: BLE001
            # Fail CLOSED. Not knowing what we hold is not a reason to assume
            # we hold nothing - that is precisely the assumption that bought
            # Denver twice.
            log.error("RISK  could not read positions; treating this pass as "
                      "fully committed: %s", exc)
            with self._risk_lock:
                self._positions = {}
                self._positions_unknown = True
            return
        with self._risk_lock:
            self._positions = fresh
            self._positions_unknown = False

    # ==============================================================
    def settle(self) -> None:
        """Score forecasts whose markets have resolved. Cheap, and the only
        thing that ever turns an opinion into a measurement."""
        try:
            settle_forecasts(self.journal, self.market,
                             exchange_tickers=set(self._positions))
        except Exception as exc:  # noqa: BLE001
            log.error("SETTLE  pass failed: %s", exc)

    def _busy_tickers(self) -> set[str]:
        """Tickers the position worker is currently selling out of."""
        worker = self.position_worker
        if worker is None:
            return set()
        with worker.lock:
            return set(worker.exiting)

    def _trade(self, report: ScanReport) -> None:
        busy = self._busy_tickers()
        for op in report.accepted[: self.limits.max_trades_per_scan]:
            clash = busy.intersection(op.market_tickers)
            if clash:
                # Buying into a ticker the worker is exiting would have the bot
                # trading against itself and leave reconciliation unable to
                # explain the result.
                log.info("ARBITRAGE SKIPPED  %s: position worker is exiting %s",
                         op.summary(), ", ".join(sorted(clash)))
                continue
            result = self.manager.execute(op)
            result.mode = self.mode.value
            report.trades.append(result)
            self.trade_times.append(datetime.now(timezone.utc))
            if result.outcome in (TradeOutcome.COMPLETE,
                                  TradeOutcome.PARTIAL_EXPOSED):
                self.exposure_usd += result.total_cost_usd
                # The exchange will not show this position for a few seconds.
                # Remember it here so a second opportunity in the SAME pass
                # cannot spend the same per-market allowance twice.
                self._remember_pending(op)

    def _remember_pending(self, op: ArbitrageOpportunity) -> None:
        with self._risk_lock:
            for leg in op.legs:
                key = (leg.ticker, getattr(leg, "side", ""))
                cost = getattr(leg, "cost_usd", 0.0) or (
                    leg.contracts * leg.limit_price_cents / 100.0)
                self._pending_cost[key] = self._pending_cost.get(key, 0.0) + cost

    def _paper(self, report: ScanReport) -> None:
        for op in report.accepted[: self.limits.max_trades_per_scan]:
            log.info("PAPER FILL  %s -> simulated net $%+.2f (no order sent)",
                     op.summary(), op.net_edge)

    def _log_scan(self, report: ScanReport) -> None:
        if not self.journal:
            return
        counts = {
            "seen": report.discovery.markets_seen,
            "priced": report.books_fetched,
            "no_orderbook": report.discovery.markets_seen - report.books_fetched,
            "edge_too_small": sum(v for k, v in report.rejections.items()
                                  if "EDGE" in k or "FEE" in k),
            "risk_rejected": sum(v for k, v in report.rejections.items()
                                 if k.startswith("RISK")),
            "traded": len(report.trades),
        }
        try:
            self.journal.log_scan(self.mode.value, counts, report.seconds)
        except Exception as exc:  # noqa: BLE001
            log.debug("scan log failed: %s", exc)
