#!/usr/bin/env python3
"""Structural arbitrage on Kalshi DEMO.

    python arb.py                    # SCAN_ONLY  (default, sends nothing)
    python arb.py --mode PAPER       # simulate fills against the live demo book
    python arb.py --mode DEMO_LIVE   # actually submit orders to Kalshi demo
    python arb.py --loop             # keep scanning
    python arb.py --once --verbose   # one scan, full detail

Mode can also come from the environment:  TRADING_MODE=DEMO_LIVE python arb.py

This build is demo-only. Production hosts and any non-demo environment are
refused before a client is even constructed.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time

from kalshibot.arb_engine import ArbitrageEngine, ScanReport, TradingMode
from kalshibot.position_manager import PositionConfig, PositionWorker
from kalshibot.auth import Signer
from kalshibot.clients import DemoExecutionClient, MarketDataClient, PortfolioClient
from kalshibot.config import ConfigError, load_settings
from kalshibot.env_guard import (ProductionBlocked, assert_demo_environment,
                                 assert_live_books_are_paper_only,
                                 assert_demo_url, describe_guard)
from kalshibot.fees import FeeModel
from kalshibot.forecast_scoring import score_pending
from kalshibot.journal import Journal
from kalshibot.maker_shadow import MakerShadow, MakerShadowConfig
from kalshibot.market_state import MarketState
from kalshibot.prodwatch import ProductionWatch, WatchConfig
from kalshibot.recorder import Recorder, RecorderConfig
from kalshibot.versioning import versions
from kalshibot.sources import (MONITOR, OFF, DOWN, HEALTHY, SourceConfigError,
                               configure_from_settings, nws_user_agent)
from kalshibot.alerts import ALERTS, today_key
from kalshibot.alerts import configure_from_settings as configure_alerts
from kalshibot.wsfeed import FeedConfig, KalshiFeed
from kalshibot.workers import Supervisor, Worker, WorkerConfig
from kalshibot.logging_setup import setup_logging

log = logging.getLogger("arb")
B, D, G, R, Y, C, X = ("\033[1m", "\033[2m", "\033[32m", "\033[31m",
                       "\033[33m", "\033[36m", "\033[0m")


def check_settlement_sources(settings, market, journal) -> None:
    """Package 14A: read, from the exchange, what each weather series settles
    on; store it; and say so loudly if it changed or is not registered.

    Kalshi's rules name the resolving source. It was a comment in config.yaml
    until now - and a comment cannot notice the contract changing under it.
    Never blocks startup: a failed lookup is logged and the bot carries on.
    """
    registered = {s.publisher for s in MONITOR.registry if "settlement" in s.roles}
    for cfg in settings.get("weather.series") or []:
        series = cfg.get("ticker_prefix")
        if not series:
            continue
        try:
            found = market.get_series(series).get("settlement_sources") or []
        except Exception as exc:  # noqa: BLE001
            log.warning("SOURCES  could not read %s settlement source: %s", series, exc)
            continue
        names = sorted({str(x.get("name") or "") for x in found})
        added, dropped = journal.record_settlement_sources(series, found)
        if added or dropped:
            log.warning("SOURCES  %s settlement source CHANGED - now %s "
                        "(added %s, dropped %s). Check the contract rules.",
                        series, names, added, dropped)
            ALERTS.send(today_key("settlement", series),
                        f"{series} settlement source changed",
                        f"Kalshi now lists {names}. Check the contract rules "
                        f"before trusting the scoring.", priority="high",
                        tags="warning")
        stray = [n for n in names if n not in registered]
        if stray:
            log.warning("SOURCES  %s settles on %s, which is not a registered "
                        "settlement source in config.yaml", series, stray)
        log.info("SOURCES  %s settles on %s", series, ", ".join(names) or "?")


def exchange_is_open(market) -> tuple[bool, str]:
    """Is the exchange actually trading right now?

    The Kalshi demo exchange closes overnight and reopens around 05:00 CST.
    A scan against a closed board takes the same fifteen minutes, reads books
    nobody is quoting, and any order it produced would be refused - so the
    honest thing is to wait.

    Fails OPEN: if the status endpoint cannot be read, carry on scanning
    rather than sitting idle on a network blip. Anything that actually
    reaches the exchange has its own guards.
    """
    try:
        st = market.exchange_status() or {}
    except Exception as exc:  # noqa: BLE001
        log.debug("exchange status unavailable (%s); assuming open", exc)
        return True, "status unavailable"

    trading = st.get("trading_active")
    active = st.get("exchange_active")
    if trading is False:
        return False, "exchange reports trading_active=false"
    if active is False:
        return False, "exchange reports exchange_active=false"
    return True, "open"


def enable_forecasts(settings) -> None:
    """Turn on opinion-based trading for THIS RUN ONLY.

    All three switches have to move together. Setting forecast.enabled without
    clearing require_guaranteed would build the strategies and then have the
    gate refuse every opportunity they produced - the bot would look like it
    was forecasting while trading nothing.

    Nothing is written back to config.yaml.
    """
    settings.raw.setdefault("forecast", {})["enabled"] = True
    prof = settings.raw.setdefault("profitability", {})
    prof["allow_forecast"] = True
    prof["require_guaranteed"] = False


def banner(mode: TradingMode, settings, balance: float | None) -> None:
    bar = "=" * 56
    if mode is TradingMode.DEMO_LIVE:
        print(f"\n{R}{B}{bar}")
        print("   KALSHI DEMO LIVE TRADING")
        print("   SIMULATED FUNDS ONLY")
        print(f"{bar}{X}")
    else:
        print(f"\n{C}{B}{bar}")
        print(f"   KALSHI STRUCTURAL ARBITRAGE  -  {mode.value}")
        print(f"{bar}{X}")
    print(f"  environment : {settings.kalshi_env}")
    print(f"  endpoint    : {settings.rest_base}")
    if balance is not None:
        print(f"  balance     : ${balance:,.2f}")

    # Say plainly, every single run, whether opinions can lose money here.
    if settings.get("forecast.enabled", False):
        names = settings.get("forecast.estimators", []) or []
        print(f"\n{Y}{B}  FORECAST STRATEGIES ARE ON{X}")
        print(f"{Y}  estimators  : {', '.join(names) or 'none configured'}{X}")
        print(f"{Y}  These trades have NO guaranteed payout. They can lose.{X}")
        print(f"{D}  sizing      : {settings.get('forecast.kelly_fraction', 0.25)} "
              f"Kelly, max {settings.get('forecast.max_bankroll_fraction', 0.02):.0%}"
              f" of bankroll, {settings.get('forecast.max_contracts_per_trade', 25)}"
              f" contracts per trade{X}")
        print(f"{D}  needs       : "
              f"{settings.get('profitability.min_probability_edge', 0.05):.0%}"
              f" edge and {settings.get('profitability.min_confidence', 0.5):.0%}"
              f" confidence{X}")
    else:
        print(f"{D}  forecasts   : off (structural arbitrage only){X}")
    print(f"{D}{describe_guard()}{X}\n")


def summarise(report: ScanReport, verbose: bool, label: str = "SCAN") -> None:
    d = report.discovery
    print(f"\n{B}{label} COMPLETE{X} in {report.seconds:.1f}s")
    print(f"  markets scanned            : {d.markets_seen}")
    print(f"  markets quoted             : {d.markets_quoted}")
    print(f"  markets ever traded        : {d.markets_with_activity}")
    print(f"  events kept                : {d.events_kept} "
          f"({d.events_exclusive} mutually exclusive)")
    if d.dropped_no_activity:
        print(f"{D}  dropped, never traded      : {d.dropped_no_activity}{X}")
    print(f"  events inspected           : {report.events_inspected} "
          f"of {d.events_kept}")
    print(f"  order books fetched        : {report.books_fetched}")
    if report.book_formats:
        fmts = ", ".join(f"{k}={v}" for k, v in sorted(report.book_formats.items()))
        empty = report.book_formats.get("empty", 0)
        colour = R if empty == report.books_fetched and report.books_fetched else D
        print(f"  {colour}order book format          : {fmts}{X}")
    depth_colour = G if report.markets_with_depth else R
    print(f"  {depth_colour}markets with real depth    : "
          f"{report.markets_with_depth}{X}")
    print(f"  structural relationships   : {report.relationships}")
    n_forecast = sum(1 for o in report.opportunities if not o.guaranteed)
    n_structural = len(report.opportunities) - n_forecast
    print(f"  priced opportunities       : {len(report.opportunities)}"
          f"{f'  ({n_structural} structural, {n_forecast} forecast)' if n_forecast else ''}")
    print(f"  {B}qualifying opportunities   : {report.qualifying}{X}")

    if getattr(report, "liquid_series", None):
        names = report.liquid_series[:10]
        print(f"\n  {G}series with live order books{X} {D}(learned, "
              f"prioritised next scan){X}")
        print(f"    {D}{', '.join(names)}{X}")

    if report.rejections:
        print(f"\n  {D}why the rest were rejected:{X}")
        for reason, n in sorted(report.rejections.items(), key=lambda kv: -kv[1]):
            print(f"    {D}{reason:<28} {n}{X}")

    # "0 opportunities" on its own cannot tell you whether the board was
    # efficient or the scanner was blind. These are the structures that were
    # real and correctly priced but simply did not cross.
    skips = getattr(report, "forecast_skips", None)
    if skips or getattr(report, "forecast_considered", 0):
        considered = getattr(report, "forecast_considered", 0)
        print(f"\n  {D}forecast markets looked at but not traded "
              f"({considered}):{X}")
        for sk in skips[:5]:
            print(f"    {D}{sk.describe()}{X}")
    if getattr(report, "forecast_errors", 0):
        print(f"  {R}forecast data errors this scan: "
              f"{report.forecast_errors}{X}")

    near = getattr(report, "near_misses", None)
    if near:
        checked = getattr(report, "near_miss_count", len(near))
        print(f"\n  {D}closest structures that did NOT cross "
              f"({checked} priced, none profitable):{X}")
        for nm in near[:5]:
            print(f"    {D}{nm.gap_cents:>4}c short   {nm.describe()}{X}")
        print(f"    {D}a structure crosses when the gap reaches 0c, and clears "
              f"fees at roughly -4c or better.{X}")
    elif not report.opportunities and report.relationships:
        print(f"\n  {D}{report.relationships} structural relationships were "
              f"checked; none of them had two priced sides to compare.{X}")

    if verbose and report.opportunities:
        print(f"\n  {D}closest near-misses:{X}")
        for op in sorted(report.opportunities,
                         key=lambda o: -o.net_edge)[:5]:
            print(f"{D}{op.explain()}{X}\n")

    for op in report.accepted:
        print(f"\n{G}{B}ARBITRAGE FOUND{X}")
        print(op.explain())

    for t in report.trades:
        colour = G if t.outcome.value == "COMPLETE" else Y
        print(f"\n{colour}{B}TRADE {t.outcome.value}{X}")
        for lx in t.legs:
            print(f"    {lx.state.value:<18} {lx.leg.ticker:<26} "
                  f"{lx.filled}/{lx.requested} @ {lx.avg_price_cents:.0f}c")
        edge = ("n/a (not guaranteed)" if t.realised_edge_usd is None
                else f"${t.realised_edge_usd:+.2f}")
        print(f"    cost ${t.total_cost_usd:.2f}  fees ${t.total_fees_usd:.2f}  "
              f"guaranteed edge {edge}")
        for n in t.notes:
            print(f"    {D}{n}{X}")

    if d.truncated:
        print(f"\n{Y}  NOTE: the market list hit the {d.page_limit}-page cap, so "
              f"parts of the{X}")
        print(f"{Y}  exchange were never listed. Raise discovery.max_market_pages.{X}")
    if report.books_fetched and not report.markets_with_depth:
        print(f"\n{R}{B}  Every order book came back empty.{X}")
        if report.book_formats.get("empty") == report.books_fetched:
            print(f"{R}  That usually means a parsing problem, not a quiet market.{X}")
            print(f"{D}  Run DIAGNOSTICS -> 2 (market audit) to see the raw shape.{X}")
        else:
            print(f"{D}  Books parsed fine but nobody is quoting these contracts.{X}")

    if not report.accepted:
        print(f"\n{Y}  No qualifying arbitrage found.{X}")
        print(f"{D}  That is a successful scan, not a failure. Real structural"
              f" arbitrage is rare{X}")
        print(f"{D}  and fee rounding kills most of what looks like an edge.{X}")


def report_feed(feed, state, verbose: bool) -> None:
    """One line on where the prices came from.

    Worth printing every scan: a feed that has quietly stopped looks exactly
    like a feed that is working, right up until the moment a stale book costs
    money. Here it shows as a hit rate that has fallen to nothing.
    """
    h = feed.health()
    st = state.health()
    colour = G if h["connected"] else Y
    print(f"{D}  feed: {colour}{'connected' if h['connected'] else 'reconnecting'}{D}"
          f"  {h['snapshots']} snapshots, {h['deltas']} deltas, "
          f"{h['resnapshots']} resnapshots, following {h['following']}"
          f"  |  cache {st['hit_rate'] * 100:.0f}% of {st['books']} books"
          + (f", {st['untrusted']} untrusted" if st["untrusted"] else "") + X)
    if verbose and h["last_error"]:
        print(f"{D}    last feed error: {h['last_error']}{X}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=[m.value for m in TradingMode],
                    default=os.getenv("TRADING_MODE", "SCAN_ONLY"))
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--forecast", action="store_true",
                    help="also run forecast strategies (opinion-based trades "
                         "with NO guaranteed payout). Overrides config.yaml "
                         "for this run only; the file is never modified.")
    args = ap.parse_args()

    mode = TradingMode(args.mode.upper())

    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        print(f"\n{R}CONFIGURATION PROBLEM{X}\n  {exc}\n", file=sys.stderr)
        return 2

    # --forecast turns on opinion-based trading FOR THIS RUN ONLY. Nothing is
    # written back to config.yaml, so closing the window returns the bot to
    # structural-arbitrage-only. Both gate switches have to move together or
    # the gate will reject every forecast it is handed.
    if args.forecast:
        enable_forecasts(settings)

    setup_logging("DEBUG" if args.verbose else
                  settings.get("logging.level", "INFO"),
                  settings.path("logging.file", "logs/kalshibot.log"))

    # ---- hard safety checks before anything is constructed ----
    book_source = str(settings.get("execution.paper_book_source", "demo")
                      or "demo").strip().lower()
    try:
        assert_demo_environment(settings.kalshi_env)
        assert_demo_url(settings.rest_base, "configured REST endpoint")
        assert_live_books_are_paper_only(mode.value, book_source)
    except ProductionBlocked as exc:
        print(f"\n{R}{B}REFUSED TO START{X}\n  {exc}\n", file=sys.stderr)
        return 3

    if not settings.key_id or not settings.private_key_path.exists():
        print(f"\n{R}Missing demo credentials.{X}\n"
              "  Set these in .env:\n"
              "    KALSHI_ENV=demo\n"
              "    KALSHI_KEY_ID=<the UUID from demo.kalshi.co>\n"
              "    KALSHI_PRIVATE_KEY_PATH=<path to the .pem>\n", file=sys.stderr)
        return 4

    signer = Signer.from_settings(settings)
    market = MarketDataClient(settings.rest_base, signer)
    series_client = market
    # Paper mode may price against the REAL book. Public market data, GET
    # only, no credential - see kalshibot/livebooks.py. Everything else
    # (discovery, market details, settlement) stays on demo.
    live_books = None
    if book_source == "live":
        from kalshibot.livebooks import LiveBookMarketClient, LiveBookReader
        reader = LiveBookReader()
        if not reader.ping():
            print(f"\n{R}Live market data is unreachable.{X}\n"
                  "  paper_book_source is 'live' but production market data "
                  "did not answer.\n  Set execution.paper_book_source back to "
                  "'demo' to run against demo books.\n", file=sys.stderr)
            return 5
        live_books = LiveBookMarketClient(market, reader)
        market = live_books
        print(f"  {Y}book source: LIVE production books (read-only, no "
              f"credential). No orders are sent in paper mode.{X}")
        log.info("PAPER  pricing against LIVE production order books "
                 "(read-only, unauthenticated)")
    portfolio = PortfolioClient(settings.rest_base, signer)
    execution = None
    if mode is TradingMode.DEMO_LIVE:
        execution = DemoExecutionClient(settings.rest_base, signer,
                                        settings.kalshi_env)

    balance = None
    try:
        balance = portfolio.balance_usd()
    except Exception as exc:  # noqa: BLE001
        print(f"{R}Could not read the demo account: {exc}{X}", file=sys.stderr)
        return 5

    banner(mode, settings, balance)

    fees = FeeModel(settings.section("fees"))
    journal = Journal(settings.path("journal.database_path", "data/journal.db"))
    if balance is not None:
        journal.log_balance(balance)

    # ---- package 14A: approved sources, health, provenance -------------
    # The registry is validated here: a config that would let a tier-4
    # source reach a trade, or improvise a fallback, does not start.
    try:
        configure_from_settings(settings, journal)
    except SourceConfigError as exc:
        print(f"\n{R}{B}REFUSED TO START{X}\n  sources: {exc}\n", file=sys.stderr)
        return 6
    configure_alerts(settings)
    log.info("ALERTS  %s", "ntfy on" if ALERTS.active else
             "off (no NTFY_TOPIC in .env) - alerts go to the log only")
    check_settlement_sources(settings, series_client, journal)

    # ---- one shared view of the exchange -------------------------------
    # The scan and the position worker now read the SAME books instead of
    # each asking REST for prices that have not moved. The store forwards
    # anything it does not cache straight to the client, so it stands in for
    # it everywhere - and it refuses to serve a book that is stale or that
    # missed an update, which means the worst case is the REST call we were
    # making anyway.
    ms_cfg = settings.section("market_state") or {}
    state = MarketState(
        market,
        max_book_age=float(ms_cfg.get("max_book_age_seconds", 20.0)),
        max_market_age=float(ms_cfg.get("max_market_age_seconds", 300.0)),
        capacity=int(ms_cfg.get("capacity", 4000)))

    feed_cfg = FeedConfig.from_dict(settings.section("feed"))
    feed = KalshiFeed(state, signer, feed_cfg, env=settings.kalshi_env)

    # ---- package 3a: keep the market data, instead of throwing it away --
    # Append-only tick files outside the journal. Recording can stop (disk
    # low, write error); trading never waits on it.
    rec_cfg = RecorderConfig.from_dict(settings.section("recorder"))
    recorder = Recorder(
        settings.path("recorder.directory", "data/ticks"), rec_cfg,
        alert=lambda msg: ALERTS.send(today_key("recorder", "disk"),
                                      "Market-data recorder stopped", msg,
                                      priority="default", tags="floppy_disk"))
    if rec_cfg.enabled:
        recorder.start()
        feed.recorder = recorder
        print(f"{D}  recording market data to {recorder.root}{X}")

    def _feed_probe():
        if not feed_cfg.enabled:
            return OFF, "feed disabled; books read over REST"
        st = feed.stats
        return (HEALTHY if st.connected_since else DOWN), st.summary()
    MONITOR.add_probe("kalshi_ws", _feed_probe)
    stop_feed = threading.Event()
    feed_thread = None
    if feed_cfg.enabled:
        feed_thread = threading.Thread(
            target=feed.run_forever, args=(stop_feed,), name="feed", daemon=True)
        feed_thread.start()
        print(f"{D}  market feed: {feed_cfg.url}{X}")
        print(f"{D}  following up to {feed_cfg.max_subscriptions} markets; "
              f"anything it cannot keep current is read over REST{X}")
    else:
        print(f"{D}  market feed off; prices are read over REST "
              f"(shared cache on, {state.max_book_age:.0f}s){X}")

    engine = ArbitrageEngine(settings, state, portfolio, execution,
                             fees, journal, mode)

    # ---- package 8: the fee schedule Kalshi publishes ------------------
    # Read from demo (where the fees are charged) and cached. It refuses an
    # entry on a series whose fees are unknown or above what the model
    # assumes; it never makes a fee look cheaper than config.yaml's.
    sched_cfg = settings.section("fees").get("schedule") or {}
    if sched_cfg.get("enabled", True):
        from kalshibot.fee_schedule import TAKER_RATE, FeeSchedule
        engine.fee_schedule = FeeSchedule(
            series_client, assumed_multiplier=fees.taker_rate / TAKER_RATE,
            refresh_hours=float(sched_cfg.get("refresh_hours", 6)),
            enforce=bool(sched_cfg.get("enforce", True)),
            alert=lambda series, msg: ALERTS.send(
                today_key("fees", series), f"Fee change scheduled: {series}", msg,
                priority="high", tags="warning"))

    # ---- the position worker ------------------------------------------
    # It runs CONCURRENTLY with the scan, on a much faster clock, because a
    # position needs looking at far more often than the exchange needs
    # sweeping - and needs only the tickers we hold to do it. Same process,
    # so it shares one risk view, one set of clients and one reconciliation.
    stop_positions = threading.Event()
    position_thread = None
    if portfolio is not None:
        # In SCAN_ONLY there is no execution client, so the worker reviews and
        # reports what it WOULD do without sending anything. Watching the
        # reasoning before letting it act costs nothing.
        worker = PositionWorker(
            state, portfolio, execution, engine.forecasts, journal,
            PositionConfig.from_dict(settings.section("positions"),
                                     risk=settings.section("risk"),
                                     forecast=settings.section("forecast")),
            lock=engine.lock, dry_run=mode is not TradingMode.DEMO_LIVE)
        engine.position_worker = worker
        position_thread = threading.Thread(
            target=worker.run_forever, args=(stop_positions,),
            name="positions", daemon=True)
        position_thread.start()
        how = "reporting only" if worker.dry_run else "CAN SELL"
        print(f"{D}  position worker every "
              f"{worker.cfg.review_interval_seconds:.0f}s alongside the scan "
              f"({how}){X}")

    try:
        from kalshibot.estimators.weather import MODEL_VERSIONS as _weather_models
    except Exception:  # noqa: BLE001
        _weather_models = {}
    run_versions = versions()
    run_id = journal.start_run(
        mode.value, note=f"env={settings.kalshi_env}",
        code_version=run_versions["code_version"],
        config_version=run_versions["config_version"],
        model_versions={s.name: (_weather_models if s.name.startswith("weather") else {})
                        for s in engine.forecasts},
        fee_schedule_version=fees.version)
    log.info("RUN  %d: code %s, config %s", run_id, run_versions["code_version"],
             run_versions["config_version"])

    # ---- package 3a + C2 M0: the same markets, on production -----------
    # Public data, read only, on its own worker and request budget; the
    # forecast strategy only ever reads what it has already fetched.
    watch_cfg = WatchConfig.from_dict(settings.section("production_watch"))
    prod_watch = None
    maker_shadow = None
    if watch_cfg.enabled and engine.forecasts:
        from kalshibot.livebooks import LiveBookReader
        prod_watch = ProductionWatch(LiveBookReader(),
                                     recorder if rec_cfg.enabled else None, watch_cfg)
        for strategy in engine.forecasts:
            strategy.production_watch = prod_watch
            if rec_cfg.enabled:
                strategy.recorder = recorder
        ms_cfg = MakerShadowConfig.from_dict(settings.section("maker_shadow"))
        if ms_cfg.enabled:
            from kalshibot.fee_schedule import FeeSchedule
            from kalshibot.livebooks import LiveBookReader as _Reader
            maker_shadow = MakerShadow(journal, fees, ms_cfg, watch=prod_watch,
                                       versions=run_versions,
                                       fee_schedule=FeeSchedule(_Reader(), enforce=False))
            maker_shadow.load()
            prod_watch.listeners.append(maker_shadow)
        print(f"{D}  production watch: books every {watch_cfg.book_interval_seconds:.0f}s, "
              f"prints every {watch_cfg.trades_interval_seconds:.0f}s, "
              f"<= {watch_cfg.max_requests_per_second:g} req/s (read only)"
              f"{'; maker shadow on (no orders)' if maker_shadow else ''}{X}")
    interval = int(settings.get("arb_loop.scan_interval_seconds", 60))
    closed_interval = int(settings.get("arb_loop.closed_check_seconds", 900))

    # ---- strategy workers ----------------------------------------------
    # A forecast strategy can name its six series. Making it wait on a sweep
    # of the whole exchange meant one pass took 2.8 hours, twenty minutes of
    # which was paging eighty thousand markets to reach four hundred whose
    # tickers it already knew. Two clocks, one account.
    def _open_now():
        """The gate both workers share. Returns the REASON as well.

        Throwing the reason away is what made a waiting bot look like a dead
        one: the worker skipped silently and nothing downstream could say
        why.
        """
        return exchange_is_open(state)

    def make_forecast_pass(strategy):
        """One pass for ONE strategy.

        Each strategy gets its own closure, its own worker and its own clock,
        so a rates strategy that repriices every two minutes does not queue
        behind a weather strategy waiting on an ensemble API. They share the
        account, the gate and the execution lock - never the queue.
        """
        name = strategy.name

        def run() -> None:
            engine.settle()
            summarise(engine.scan_focused(strategies=[strategy]),
                      args.verbose, label=name.upper())
            _after_pass()
        return run

    def arbitrage_pass() -> None:
        summarise(engine.scan_once(), args.verbose, label="SWEEP")
        _after_pass()

    def _after_pass() -> None:
        # Follow whatever was actually looked at. The exchange has 80,000
        # markets; the working set is a few hundred.
        if feed_cfg.enabled:
            feed.follow(state.hot_tickers(feed_cfg.max_subscriptions))
            report_feed(feed, state, args.verbose)

    workers_cfg = settings.section("workers") or {}
    supervisor = Supervisor()
    has_forecasts = bool(engine.forecasts)
    # One worker per strategy. A new estimator in config.yaml becomes a new
    # parallel worker on the next start, with nothing else to wire up - and
    # it is still the same one command that starts them all.
    defaults = workers_cfg.get("forecast") or {}
    for strategy in engine.forecasts:
        cfg = {**defaults, **(workers_cfg.get(strategy.name) or {})}
        supervisor.add(Worker(
            WorkerConfig.from_dict(strategy.name, cfg, 120.0),
            make_forecast_pass(strategy), should_run=_open_now))
    supervisor.add(Worker(
        WorkerConfig.from_dict("arbitrage", workers_cfg.get("arbitrage"),
                               float(max(interval, 900))),
        arbitrage_pass, should_run=_open_now))
    if prod_watch is not None:
        supervisor.add(Worker(
            WorkerConfig.from_dict("production_watch",
                                   workers_cfg.get("production_watch"), 15.0),
            prod_watch.run_pass))

    # Execution quality: what would this order cost on a REAL book?
    #
    # The one question demo cannot answer, and the last unknown before any
    # conversation about real money. Reads the public production book - no
    # credential, GET only - and walks a realistic size down both books for
    # the same market at the same instant.
    #
    # Measurement only. Nothing it computes is returned to the engine and it
    # places no orders; the reader it uses has no method that could.
    probe_cfg = settings.section("execution_probe") or {}
    if probe_cfg.get("enabled") and engine.forecasts:
        from kalshibot.execution_probe import compare
        from kalshibot.livebooks import LiveBookReader
        probe_reader = LiveBookReader()
        probe_size = int(probe_cfg.get("size", 25))
        probe_n = int(probe_cfg.get("markets_per_pass", 6))

        def probe_pass():
            tickers = list(state.hot_tickers(probe_n)) if state else []
            if not tickers:
                log.info("PROBE  nothing to compare yet (no markets priced)")
                return
            done = 0
            # Why a pass recorded nothing. A probe that says nothing when it
            # finds nothing hid a parsing bug for days; it says why now.
            why = {"error": 0, "live empty": 0, "demo empty": 0, "one-sided": 0}
            for ticker in tickers[:probe_n]:
                try:
                    demo_book = market.get_depth(ticker)
                    live_book = probe_reader.orderbook(ticker)
                except Exception as exc:  # noqa: BLE001
                    log.debug("PROBE  %s: %s", ticker, exc)
                    why["error"] += 1
                    continue
                if live_book is None:
                    why["live empty"] += 1
                    continue
                if demo_book is None or not (demo_book.yes_bids or demo_book.no_bids):
                    why["demo empty"] += 1
                    continue
                before = done
                for side in ("yes", "no"):
                    row = compare(ticker, demo_book, live_book,
                                  size=probe_size, side=side, action="buy")
                    if row:
                        journal.log_book_quality(row)
                        done += 1
                if done == before:
                    why["one-sided"] += 1
            skipped = ", ".join(f"{n} {k}" for k, n in why.items() if n)
            log.info("PROBE  compared %d book side(s) demo vs live%s", done,
                     f"; skipped: {skipped}" if skipped else "")

        supervisor.add(Worker(
            WorkerConfig.from_dict("execution_probe",
                                   workers_cfg.get("execution_probe"), 900.0),
            probe_pass))

    # Scoring: what did the temperature actually do?
    #
    # Deliberately NOT gated on the exchange being open - the weather happens
    # whether or not Kalshi is trading, and an overnight close is the best
    # time to be filling in yesterday. This ran as a manual script nobody ever
    # ran, and a four-degree Miami bias hid behind the empty table for eight
    # days. On a clock it cannot be forgotten.
    if has_forecasts:
        tz_by_series = {x["ticker_prefix"]: x.get("timezone", "America/New_York")
                        for x in (settings.get("weather.series") or [])}

        cli_by_station = {x["station"]: x["climate_report"]
                          for x in (settings.get("weather.series") or [])
                          if x.get("station") and x.get("climate_report")}

        def scoring_pass():
            from kalshibot.weatherdata import WeatherData
            wd = WeatherData(cache_seconds=1,
                             user_agent=nws_user_agent(settings.section("sources")))
            # score_pending writes through the shared connection directly,
            # so it takes the journal's write lock like every other writer.
            with journal.lock:
                updated, looked_up = score_pending(
                    journal.conn, wd._session, tz_by_series,
                    cli_by_station=cli_by_station)
            # Package 2: how did every opportunity past its settlement turn out?
            try:
                from kalshibot.ledger import resolve_outcomes
                resolve_outcomes(
                    journal, series_client, fees,
                    retention_days=int(settings.get("journal.ledger_retention_days", 90)))
            except Exception as exc:  # noqa: BLE001
                log.error("LEDGER  outcome pass failed: %s", exc)
            # C2 M0: what did the simulated maker orders' contracts pay?
            if maker_shadow is not None:
                try:
                    maker_shadow.resolve(series_client)
                except Exception as exc:  # noqa: BLE001
                    log.error("MAKER SHADOW  settlement pass failed: %s", exc)
            if rec_cfg.enabled:
                recorder.prune()
            if updated:
                log.info("SCORING  filled %d forecast rows from %d "
                         "station-days", updated, looked_up)

        supervisor.add(Worker(
            WorkerConfig.from_dict("scoring", workers_cfg.get("scoring"),
                                   3600.0),
            scoring_pass))

    try:
        if args.once or not args.loop:
            # A single pass still runs both, in order, so --once behaves the
            # way it always has.
            open_now, why = exchange_is_open(state)
            if not open_now:
                print(f"\n{Y}  EXCHANGE CLOSED{X}  {D}{why}{X}")
                log.info("EXCHANGE CLOSED  %s", why)
            for strategy in engine.forecasts:
                make_forecast_pass(strategy)()
            arbitrage_pass()
        else:
            for w in supervisor.workers:
                print(f"{D}  {w.cfg.name} worker every "
                      f"{w.cfg.interval:.0f}s{X}")
            supervisor.start()
            # A heartbeat on ITS OWN clock, fast enough that a person watching
            # can tell the difference between waiting and dead. The old single
            # loop said "EXCHANGE CLOSED, checking again in 15 min" every time
            # it waited; the worker version said nothing at all for fifteen
            # minutes and got stopped by hand because it looked broken.
            beat = max(15, int(settings.get("arb_loop.heartbeat_seconds", 60)))
            last_logged = 0.0
            while True:
                if supervisor.stop.wait(beat):
                    break
                supervisor.check_threads()
                line = supervisor.summary()
                waiting = supervisor.waiting()
                colour = Y if waiting else D
                print(f"{colour}  {time.strftime('%H:%M:%S')}  {line}{X}")
                # The terminal gets a line a minute; the log gets one every
                # five, so the file stays readable but never goes quiet.
                if time.time() - last_logged >= 300:
                    last_logged = time.time()
                    log.info("HEARTBEAT  %s", line)
                    # Cache and feed counters, cumulative since start. The
                    # only way to tell whether the feed is earning its keep
                    # is to compare this line with it off and with it on.
                    log.info("MARKET STATE  %s | feed %s", state.stats.summary(),
                             feed.stats.summary() if feed_cfg.enabled else "off")
                    log.info("SOURCES  %s", MONITOR.summary())
                    if rec_cfg.enabled:
                        log.info("RECORDER  %s", recorder.summary())
                    if engine.fee_schedule is not None:
                        log.info("FEES  %s", engine.fee_schedule.summary())
                    if prod_watch is not None:
                        log.info("PRODWATCH  %s%s", prod_watch.summary(),
                                 f" | maker shadow {maker_shadow.summary()}"
                                 if maker_shadow else "")
                    # The balance, from the exchange, for the watch screen.
                    if portfolio is not None:
                        try:
                            journal.log_balance(portfolio.balance_usd())
                        except Exception as exc:  # noqa: BLE001
                            log.debug("could not record the balance: %s", exc)
                    snap = MONITOR.snapshot()
                    try:
                        journal.save_source_health(snap)
                    except Exception as exc:  # noqa: BLE001
                        log.debug("could not save source health: %s", exc)
                    for row in snap:
                        src = MONITOR.registry.get(row["source_id"])
                        if row["state"] == DOWN and src and src.required_by:
                            ALERTS.send(
                                today_key("down", row["source_id"]),
                                f"Source DOWN: {row['source_id']}",
                                f"{src.publisher}: {row['consecutive_failures']} "
                                f"failures in a row. Last error: "
                                f"{row['last_error'] or '-'}", priority="high",
                                tags="warning")
                if all(w.health.suspended for w in supervisor.workers):
                    print(f"\n{R}  every worker is suspended; nothing left to "
                          f"do.{X}\n  {supervisor.summary()}\n")
                    break
    except KeyboardInterrupt:
        print("\nstopped\n")
    finally:
        supervisor.shutdown(timeout=10)
        stop_positions.set()
        stop_feed.set()
        if position_thread is not None:
            position_thread.join(timeout=5)
        if feed_thread is not None:
            feed_thread.join(timeout=5)
        recorder.stop()
        journal.end_run(run_id)

    s = journal.arbitrage_summary()
    print(f"\n{D}  journal totals: {s['opportunities']} opportunities, "
          f"{s['accepted']} accepted, {s['executions']} executions, "
          f"guaranteed edge booked ${(s['net_pnl'] or 0):+.2f}{X}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
