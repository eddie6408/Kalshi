"""Live runner: fetches real Kalshi market data and drives the engine.

Threads:
  main       hot-tier quotes + depth every quote_interval_s, engine cycle,
             reconciliation, health, status for the dashboard.
  discovery  series catalog, hourly universe sweep, probe-tier quotes and
             the sports-context index. Slow, paced calls live here so they
             never stall position management.
  dashboard  read-only HTTP server on 127.0.0.1 (its own DB connection).

All HTTP shares one token bucket.
"""
from __future__ import annotations

import json
import logging
import signal
import socket
import sqlite3
import threading
import time
import traceback
import uuid

from lvt import __version__
from lvt.alerts import Alerter
from lvt.clock import RealClock
from lvt.config import Config, git_commit
from lvt.data.context import KalshiLiveDataContext, MlbStatsContext
from lvt.data.http import PublicHttp
from lvt.data.kalshi_public import KalshiPublicData
from lvt.data.provider import CompositeContext, FallbackMarketData
from lvt.data.events import EventDetector
from lvt.data.schedule import PollState, companion_tickers, plan_context_polls
from lvt.db import open_state_db, open_tape_db
from lvt.engine.scanner import Scanner
from lvt.engine.trader import Engine
from lvt.execution.paper import PaperVenue
from lvt.execution.reconcile import reconcile
from lvt.health import ResourceMonitor
from lvt.monitor import Monitor
from lvt.labeler import label_signals
from lvt.live_gate import EXIT_LIVE_REFUSED, build_live_venue, credentials_present, format_report, live_allowed
from lvt.models import Snapshot
from lvt.tape import TapeRecorder

log = logging.getLogger("lvt.runner")


class LiveRunner:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.mode = cfg.mode
        self.env = self.mode
        self.clock = RealClock()
        self.run_id = f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.db = open_state_db(cfg.path(cfg.app.state_db))
        self.tape_db = open_tape_db(cfg.path(cfg.app.tape_db))
        self.tape = TapeRecorder(self.tape_db)
        self.alerter = Alerter(self.db, self.run_id, cfg.alerts.dedupe_window_s, cfg.env.get("ALERT_WEBHOOK_URL", ""))
        k = cfg.kalshi
        self.http = PublicHttp(k.base_url, k.max_requests_per_second, k.burst, k.request_timeout_s, k.backoff_on_429_s)
        # discovery thread gets its own session but shares the rate budget
        self.http_bg = PublicHttp(k.base_url, k.max_requests_per_second, k.burst, k.request_timeout_s, k.backoff_on_429_s)
        self.http_bg.bucket = self.http.bucket
        self.data = FallbackMarketData([KalshiPublicData(self.http, cfg.discovery.quote_batch_size)])
        self.data_bg = KalshiPublicData(self.http_bg, cfg.discovery.quote_batch_size)
        ctx_providers = [KalshiLiveDataContext(self.http_bg)]
        if cfg.context.mlb_statsapi_enabled:
            ctx_providers.append(MlbStatsContext(self.http_bg))
        self.context = CompositeContext(ctx_providers)
        self.ctx_fetch = CompositeContext(
            [KalshiLiveDataContext(self.http)] + ([MlbStatsContext(self.http)] if cfg.context.mlb_statsapi_enabled else []))
        self.scanner = Scanner(cfg)
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.monitor = ResourceMonitor()
        self.guard = Monitor(cfg, self.db, self.alerter, cfg.path(cfg.app.data_dir))
        self.venue = None
        self.engine: Engine | None = None
        self.edge_engine: Engine | None = None
        self.venue2 = None
        self.timers: dict[str, float] = {}
        self.depth_rr = 0
        self.ctx_last: dict[str, float] = {}
        self.consecutive_failures = 0
        self.last_quote_ok = time.time()
        self.cycle_ms = 0.0
        self.trades_since = 0.0
        self.ctx_state: dict[str, PollState] = {}
        self.event_detector = EventDetector()
        self.sport_events_seen = 0
        self.companions: list[str] = []
        self.companion_info: dict = {}                 # ticker -> MarketInfo (recorded, never traded)
        self.event_markets: dict[str, tuple[float, list[str]]] = {}   # event -> (fetched ts, tickers)
        self.seen_trades: dict[str, float] = {}
        self.data_trades = KalshiPublicData(self.http, cfg.discovery.quote_batch_size)

    # ------------------------------------------------------------ startup
    def build_venue(self):
        markets = self.scanner.markets
        if self.mode == "LIVE":
            ok, items = live_allowed(self.cfg, self.db)
            if not ok:
                msg = "LIVE requested but the live gate refused:\n" + format_report(items)
                log.critical(msg)
                self.alerter.fire("LIVE_GATE_REFUSED", "TRADING_MODE=LIVE refused - see logs", "CRITICAL")
                raise SystemExit(EXIT_LIVE_REFUSED)
            try:
                venue = build_live_venue(self.cfg, markets)
                venue.balance_usd()                            # proves the key works
            except Exception as exc:                          # noqa: BLE001
                log.critical("LIVE refused: credentials/exchange check failed: %s", type(exc).__name__)
                self.alerter.fire("LIVE_GATE_REFUSED", f"live venue unavailable: {type(exc).__name__}", "CRITICAL")
                raise SystemExit(EXIT_LIVE_REFUSED)
            self.alerter.fire("LIVE_MODE_ACTIVATION", "LIVE trading activated", "CRITICAL")
            return venue
        if credentials_present(self.cfg)[0]:
            self.alerter.fire("PRODUCTION_CREDENTIALS_DETECTED",
                              f"production credentials present in the environment; ignored in {self.mode} mode", "WARNING")
        v = PaperVenue(self.cfg, self.env, markets, shadow=(self.mode == "SHADOW"))
        v.restore(self.db)
        return v

    def startup(self) -> None:
        cfg = self.cfg
        log.info("=" * 70)
        log.info("KALSHI LIVE VOLATILITY TRADER %s  mode=%s  run=%s", __version__, self.mode, self.run_id)
        log.info("market data: LIVE (Kalshi public REST)  execution: %s  production orders: %s",
                 "KALSHI PRODUCTION" if self.mode == "LIVE" else "SIMULATED",
                 "ENABLED" if self.mode == "LIVE" else "DISABLED")
        log.info("=" * 70)
        self.venue = self.build_venue()
        self.engine = Engine(cfg, self.env, self.run_id, self.db, self.venue, self.scanner.markets,
                             self.alerter, self.clock)
        self.engine.risk.fail_closed["STARTUP"] = "reconciliation has not completed"
        self.edge_engine = self._build_edge_engine()
        if self.mode in ("SHADOW", "LIVE"):
            from lvt.live_gate import limits_reviewed
            if limits_reviewed(cfg, self.db) is None:
                log.warning("RISK LIMITS NOT REVIEWED for this config - they were loosened for paper testing. "
                            "Revisit [risk] in config/default.toml, then run `lvt review-limits --by NAME`.")
                self.alerter.fire("RISK_LIMITS_REVIEW_NEEDED",
                                  f"{self.mode} started with [risk] limits not reviewed since paper testing", "CRITICAL")
        else:
            log.info("risk limits: %s", "ENFORCED" if self.engine.risk.limits_enforced
                     else "account limits OFF (paper research mode) - revisit before SHADOW/LIVE")
        self.db.insert("runs", {"run_id": self.run_id, "env": self.env, "kind": "live-loop", "started_ts": time.time(),
                                "git_commit": git_commit(cfg.root), "config_hash": cfg.config_hash(),
                                "strategy_versions": {s.name: s.version for s in self.engine.strategies},
                                "host": socket.gethostname()})
        self.alerter.fire("BOT_STARTED", f"started in {self.mode} mode ({self.run_id})", "INFO")
        now = time.time()
        self.engine.restore(now)
        if self.edge_engine is not None:
            self.edge_engine.restore(now)
        # discovery: catalog + first sweep synchronously so we start with a universe
        self._refresh_catalog()
        self._sweep()
        self._probe(force=True)
        self._startup_reconcile()
        self.last_quote_ok = time.time()          # the first sweep is not a data outage

    def _build_edge_engine(self):
        """EDGE_ENGINE_V1 as a challenger in its own paper account (PAPER only)."""
        ecfg = self.cfg.data.get("edge", {})
        if self.mode != "PAPER" or not ecfg.get("enabled", False):
            return None
        from collections import ChainMap

        from lvt.edge.engine import EdgeEngine
        env2 = ecfg.get("env", "PAPER_EDGE")
        markets = ChainMap(self.scanner.markets, self.companion_info)
        venue2 = PaperVenue(self.cfg, env2, markets)
        venue2.restore(self.db)
        edge = EdgeEngine(self.cfg, self.db, env2, self.run_id, self.engine.series,
                          lambda t: self.scanner.markets.get(t) or self.companion_info.get(t),
                          lambda ev: self.ctx_fetch.providers[0].meta.get(ev))
        eng = Engine(self.cfg, env2, self.run_id, self.db, venue2, markets, self.alerter, self.clock,
                     strategies=[], edge=edge, series=self.engine.series, metrics=self.engine.metrics,
                     contexts=self.engine.contexts, owns_metrics=False)
        eng.risk.fail_closed["STARTUP"] = "reconciliation has not completed"
        self.venue2 = venue2
        log.info("A/B challenger %s running in paper account %s", edge.version, env2)
        return eng

    def _engines(self):
        return [(self.engine, self.venue, "reconcile")] + (
            [(self.edge_engine, self.venue2, "reconcile_edge")] if self.edge_engine is not None else [])

    def _startup_reconcile(self) -> None:
        for e, venue, key in self._engines():
            still = e.om.resolve_unknown(time.time())
            res = reconcile(e.book, venue, still)
            self._record_reconcile(res, startup=True, engine=e, key=key)
            if res.ok:
                e.risk.fail_closed.pop("STARTUP", None)
                log.info("startup reconciliation OK (%s): %s", e.env, res.summary())

    def _record_reconcile(self, res, startup=False, engine=None, key="reconcile") -> None:
        e = engine or self.engine
        self.db.insert("status", {"k": key, "ts": time.time(),
                                  "v": {"ok": res.ok, "summary": res.summary(), "mismatches": res.mismatches,
                                        "startup": startup}}, replace=True)
        if res.ok:
            e.risk.fail_closed.pop("RECONCILIATION", None)
        else:
            e.risk.fail_closed["RECONCILIATION"] = res.summary()
            self.alerter.fire("RECONCILIATION_FAILURE", res.summary(), "CRITICAL", {"mismatches": res.mismatches})
            if any(m["kind"] == "UNEXPECTED_POSITION" for m in res.mismatches):
                self.alerter.fire("UNEXPECTED_POSITION", "venue reports a position the bot did not open", "CRITICAL")

    # --------------------------------------------------------- discovery
    def _refresh_catalog(self) -> None:
        try:
            cat = self.data_bg.series_catalog()
            with self.lock:
                self.scanner.catalog = cat
                self.scanner.last_catalog = time.time()
            log.info("series catalog: %d series", len(cat))
        except Exception as exc:                              # noqa: BLE001
            log.warning("series catalog failed: %s", exc)

    def _sweep(self) -> None:
        t0 = time.time()
        try:
            found = self.data_bg.list_open_markets(pause_s=self.cfg.discovery.sweep_page_pause_s)
        except Exception as exc:                              # noqa: BLE001
            log.warning("universe sweep failed: %s", exc)
            self.alerter.fire("API_ERRORS", f"universe sweep failed: {exc}", key="sweep")
            return
        with self.lock:
            keep = {p.ticker for p in self.engine.book.open_positions()} if self.engine else set()
            kept = self.scanner.load_universe(found, time.time(), keep)
        self.tape.record_markets(kept, time.time())
        log.info("sweep: %d open markets -> %d in universe (%.0fs)", len(found), len(kept), time.time() - t0)

    def _probe(self, force: bool = False) -> None:
        with self.lock:
            due = self.scanner.probe_due(time.time()) if not force else list(self.scanner.probe)
        if not due:
            return
        try:
            quotes = self.data_bg.quotes(due)
        except Exception as exc:                              # noqa: BLE001
            log.warning("probe failed: %s", exc)
            return
        with self.lock:
            self.scanner.note_probe(quotes)
        self.tape.record_probe(quotes)

    def discovery_loop(self) -> None:
        d = self.cfg.discovery
        while not self.stop.is_set():
            try:
                now = time.time()
                if now - self.scanner.last_catalog >= d.series_refresh_interval_s:
                    self._refresh_catalog()
                if now - self.scanner.last_sweep >= d.full_sweep_interval_s:
                    self._sweep()
                if self.cfg.context.enabled and now - self.timers.get("ctx_index", 0) >= self.cfg.context.milestone_refresh_interval_s:
                    self.timers["ctx_index"] = now
                    self.context.refresh_index()
                    self.ctx_fetch.providers[0].index = self.context.providers[0].index
                    self.ctx_fetch.providers[0].groups = self.context.providers[0].groups
                    self.ctx_fetch.providers[0].meta = self.context.providers[0].meta
                    if len(self.ctx_fetch.providers) > 1:
                        self.ctx_fetch.providers[1].games = self.context.providers[1].games
                self._probe()
            except Exception:                                 # noqa: BLE001
                log.error("discovery loop error:\n%s", traceback.format_exc())
            self.stop.wait(1.0)

    # ------------------------------------------------------------ main loop
    def cycle(self) -> None:
        cfg, e = self.cfg, self.engine
        now = time.time()
        self._check_kill_switch(now)
        with self.lock:
            pinned = set()
            for eng, _, _ in self._engines():
                pinned |= {p.ticker for p in eng.book.open_positions()} | {r.ticker for r in eng.om.working.values()}
            hot = self.scanner.select_hot(now, pinned)
        snaps: dict[str, Snapshot] = {}
        self.companions = self._companions(hot) if hot else []
        if hot:
            try:
                quotes = self.data.quotes(hot + self.companions)
                quotes = self.guard.on_quotes(quotes, [x for x, _, _ in self._engines()])
                self.last_quote_ok = time.time()
                if "MARKET_DATA" in e.risk.fail_closed:
                    e.risk.fail_closed.pop("MARKET_DATA")
                    self.alerter.fire("MARKET_DATA_RESTORED", "market data restored", "INFO")
            except Exception as exc:                          # noqa: BLE001
                quotes = {}
                e.risk.fail_closed["MARKET_DATA"] = f"quote fetch failed: {exc}"[:200]
                self.alerter.fire("MARKET_DATA_DISCONNECTED", f"quote fetch failed: {exc}", "CRITICAL")
            self.tape.record_quotes(quotes, cfg.discovery.quote_interval_s * 3)
            if now - self.timers.get("hot_probe", 0) >= cfg.discovery.probe_interval_s:
                self.timers["hot_probe"] = now
                with self.lock:
                    self.scanner.note_probe(quotes)
            if self.companions:
                with self.lock:
                    self.scanner.note_probe({t: q for t, q in quotes.items() if t in self.companion_info})
            depths = self._fetch_depth(hot, pinned)
            for t, q in quotes.items():
                d = depths.get(t)
                if d is None:
                    prev = e.series.get(t)
                    d = prev.last_snapshot.depth if (prev and prev.last_snapshot) else None
                snaps[t] = Snapshot(t, q.ts, q, d)
                mi = self.scanner.markets.get(t)
                if mi is not None:
                    mi.status = q.status or mi.status
                    mi.result = q.result
        self._fetch_context(hot, pinned)
        trades, covers = self._fetch_trades(set(hot) | pinned)
        self._clock_check()
        e.on_snapshots(snaps, time.time(), tradeable=set(hot), trades=trades, trade_feed_covers=covers)
        if self.edge_engine is not None:
            # same data, separate paper account; it may also trade the match-group companions
            self.edge_engine.on_snapshots(snaps, time.time(), tradeable=set(hot) | set(self.companions),
                                          trades=trades, trade_feed_covers=covers)
        self._periodic(time.time())

    def _fetch_depth(self, hot: list[str], pinned: set[str]) -> dict:
        n = self.cfg.discovery.depth_requests_per_cycle
        e = self.engine
        prio = list(pinned)
        ranked = sorted((t for t in hot if t not in pinned),
                        key=lambda t: -((e.scanner_rows.get(t) or {}).get("movement_score") or 0))
        rest = ranked[:max(0, n * 2)]
        choose = prio[:n]
        if len(choose) < n and rest:
            for i in range(n - len(choose)):
                choose.append(rest[(self.depth_rr + i) % len(rest)])
            self.depth_rr += 1
        out = {}
        for t in dict.fromkeys(choose):
            try:
                d = self.data.depth(t, self.cfg.discovery.depth_levels)
                out[t] = d
                self.tape.record_depth(d)
            except Exception as exc:                          # noqa: BLE001
                log.debug("depth %s failed: %s", t, exc)
        return out

    def _fetch_trades(self, watched: set[str]):
        """Exchange-wide public trade prints since the last cycle, filtered to
        the markets we watch. They drive the paper maker queue model and are
        recorded to the tape so replays can use the same evidence."""
        mk = self.cfg.data.get("maker", {})
        if not mk.get("trades_feed", True):
            return None, None
        since = self.trades_since or (time.time() - 5)
        try:
            got, complete = self.data_trades.trades_since(since - 2, int(mk.get("trades_max_pages", 3)))
        except Exception as exc:                          # noqa: BLE001
            log.debug("trade feed failed: %s", exc)
            return [], set()
        fresh = []
        for t in got:
            if t.trade_id in self.seen_trades:
                continue
            self.seen_trades[t.trade_id] = t.ts
            if t.ticker in watched:
                fresh.append(t)
        if got:
            self.trades_since = max(t.ts for t in got)
        if len(self.seen_trades) > 50000:
            cutoff = self.trades_since - 120
            self.seen_trades = {k: v for k, v in self.seen_trades.items() if v >= cutoff}
        self.tape.record_trades(fresh)
        return fresh, (set(watched) if complete else set())

    def _fetch_context(self, hot: list[str], pinned: set[str]) -> None:
        """Live scores for watched events, on the schedule in data/schedule.py
        (live tennis point-by-point, other live events slower, idle rarely)."""
        ccfg = self.cfg.context
        if not ccfg.enabled:
            return
        e = self.engine
        events: list[str] = []
        sport_of: dict[str, str] = {}
        for t in list(pinned) + hot:
            mi = self.scanner.markets.get(t) or self.companion_info.get(t)
            if mi and mi.sport != "NON_SPORT":
                events.append(mi.event_ticker)
                sport_of[mi.event_ticker] = mi.sport
        now = time.time()
        for ev in plan_context_polls(events, self.ctx_state, now, ccfg, self.ctx_fetch.covers):
            st = self.ctx_state.setdefault(ev, PollState(sport=sport_of.get(ev, "")))
            st.last_ts = now
            meta = self.ctx_fetch.providers[0].meta.get(ev)
            if meta:
                self.tape.record_event_meta(ev, meta, now)
            try:
                c = self.ctx_fetch.context(ev)
            except Exception as exc:                          # noqa: BLE001
                log.debug("context %s failed: %s", ev, exc)
                c = None
            if c is None:
                err = self.ctx_fetch.last_error.pop(ev, None)
                st.failed_until = now + (3600 if err == 404 else 60)
                continue
            st.status = c.status
            st.sport = c.sport if c.sport != "OTHER" else st.sport
            e.contexts[ev] = c
            self.tape.record_context(c)
            found = self.event_detector.update(c)
            if self.edge_engine is not None:
                self.edge_engine.edge.on_context(c, found, now)
            if found:
                self.sport_events_seen += len(found)
                self.tape.record_sport_events(found)

    def _companions(self, hot: list[str]) -> list[str]:
        """Related markets of live tennis matches on the hot tier: recorded
        every cycle for research, never traded."""
        ccfg = self.cfg.context
        if not ccfg.get("companions_enabled", False):
            return []
        sports = set(ccfg.get("companion_sports", ["TENNIS"]))
        hot_events = []
        for t in hot:
            mi = self.scanner.markets.get(t)
            if mi and mi.sport in sports:
                hot_events.append(mi.event_ticker)
        live = {ev: st.status for ev, st in self.ctx_state.items()}
        groups = self.ctx_fetch.providers[0].groups
        needed = [rel for ev in dict.fromkeys(hot_events) if live.get(ev) == "live"
                  for rel in groups.get(ev, (ev,))]
        self._load_event_markets(needed)
        by_event = {ev: tk for ev, (_, tk) in self.event_markets.items()}
        return companion_tickers(hot_events, groups, live, by_event, set(hot), int(ccfg.get("max_companions", 60)))

    def _load_event_markets(self, events: list[str]) -> None:
        """Open markets per related event: from the universe when present,
        otherwise one GET /markets?event_ticker per cycle, cached 15 min."""
        now = time.time()
        fetched = 0
        for ev in dict.fromkeys(events):
            hit = self.event_markets.get(ev)
            if hit and now - hit[0] < 900:
                continue
            known = [t for t, mi in self.scanner.markets.items() if mi.event_ticker == ev]
            if known:
                self.event_markets[ev] = (now, known)
                continue
            if fetched >= 1:
                continue
            fetched += 1
            try:
                found = self.data_trades.list_open_markets(event_ticker=ev)
            except Exception as exc:                          # noqa: BLE001
                log.debug("markets for %s failed: %s", ev, exc)
                self.event_markets[ev] = (now, [])
                continue
            for mi in found:
                with self.lock:
                    self.scanner.enrich(mi)
                self.companion_info[mi.ticker] = mi
            self.tape.record_markets(found, now)
            self.event_markets[ev] = (now, [mi.ticker for mi in found])

    def _clock_check(self) -> None:
        skew = self.http.stats.clock_skew_s
        e = self.engine
        if abs(skew) > self.cfg.kalshi.max_clock_skew_s:
            e.risk.fail_closed["CLOCK"] = f"local clock off by ~{skew:+.1f}s vs exchange"
            self.alerter.fire("CLOCK_SKEW", e.risk.fail_closed["CLOCK"], "CRITICAL")
        else:
            e.risk.fail_closed.pop("CLOCK", None)

    def _check_kill_switch(self, now: float) -> None:
        f = self.cfg.path(self.cfg.kill_switch.file)
        on = f.exists() or self.cfg.kill_switch_env
        reason = ""
        if on:
            reason = "KILL_SWITCH env" if self.cfg.kill_switch_env else f"file {f.name}: " + (
                f.read_text().strip()[:200] if f.exists() else "")
        for eng, _, _ in self._engines():
            eng.set_kill_switch(on, reason, now)

    def _due(self, name: str, every: float, now: float) -> bool:
        if now - self.timers.get(name, 0.0) >= every:
            self.timers[name] = now
            return True
        return False

    def _periodic(self, now: float) -> None:
        cfg = self.cfg
        if self._due("status", 5, now):
            self._write_status(now)
        if self._due("reconcile", cfg.execution.reconcile_interval_s, now):
            for eng, venue, key in self._engines():
                if eng.om.working:
                    continue
                still = eng.om.resolve_unknown(now)
                res = reconcile(eng.book, venue, still)
                self._record_reconcile(res, engine=eng, key=key)
                if res.ok:
                    eng.risk.fail_closed.pop("STARTUP", None)
        if self._due("health", cfg.health.interval_s, now):
            self._health(now)
            try:
                self.guard.periodic(now, self.http.stats, [x for x, _, _ in self._engines()],
                                    self.edge_engine.edge if self.edge_engine is not None else None)
            except Exception as exc:                          # noqa: BLE001
                log.warning("monitor failed: %s", exc)
        if self._due("labeler", cfg.labeler.interval_s, now):
            try:
                n = label_signals(self.db, self.tape_db, cfg, now)
                if n:
                    log.info("labeled %d signal outcomes", n)
                from lvt.edge.diagnostics import run_all
                done = run_all(self.db, self.tape_db, now)
                if any(done.values()):
                    log.info("diagnostics: %s", done)
            except Exception as exc:                          # noqa: BLE001
                log.warning("labeler failed: %s", exc)
        if self._due("results", 600, now):
            self._collect_results(now)
        if self._due("prune", 86400, now):
            n = self.tape.prune(cfg.health.tape_retention_days)
            log.info("tape prune removed %d rows", n)
            self._prune_state(now)
        if self._due("bookkeeping", 600, now):
            self._prune_bookkeeping(now)

    def _collect_results(self, now: float, limit: int = 500) -> None:
        """Outcomes of markets we recorded and that are no longer quoted
        (V2 brief section 8: calibration needs actual results)."""
        try:
            rows = self.tape_db.query(
                "SELECT m.ticker FROM markets m WHERE m.sport = 'TENNIS' AND NOT EXISTS "
                "(SELECT 1 FROM market_results r WHERE r.ticker=m.ticker) AND m.last_seen < ? "
                "AND m.ticker NOT IN (SELECT DISTINCT ticker FROM quotes WHERE ts > ?) LIMIT ?",
                (now - 600, now - 600, limit))
            tickers = [r["ticker"] for r in rows]
            if not tickers:
                return
            quotes = self.data_trades.quotes(tickers)
            n = self.tape.record_results(list(quotes.values()), now)
            if n:
                log.info("recorded %d settled market results", n)
        except Exception as exc:                              # noqa: BLE001
            log.warning("result collection failed: %s", exc)

    def _prune_bookkeeping(self, now: float) -> None:
        """Runner-side per-event/per-market dictionaries (bounded memory)."""
        self.ctx_state = {k: v for k, v in self.ctx_state.items() if now - v.last_ts < 6 * 3600}
        self.ctx_last = {k: v for k, v in self.ctx_last.items() if now - v < 6 * 3600}
        self.event_markets = {k: v for k, v in self.event_markets.items() if now - v[0] < 3600}
        live = {t for _, tk in self.event_markets.values() for t in tk} | set(self.engine.series)
        self.companion_info = {k: v for k, v in self.companion_info.items() if k in live}
        self.event_detector.last = {k: v for k, v in self.event_detector.last.items()
                                    if now - v[1].fetched_ts < 6 * 3600}
        ph = self.scanner.probe_hist
        for t in [t for t, h in ph.items() if not h or now - h[-1][0] > 3600]:
            ph.pop(t, None)

    def _prune_state(self, now: float) -> None:
        """State-DB retention: high-volume research rows age out; trades,
        orders, fills and positions are kept forever."""
        r = self.cfg.data.get("health", {})
        short, long_ = float(r.get("state_retention_days", 7)), float(r.get("signal_retention_days", 30))
        n = 0
        for table, col, days, where in (("market_states", "ts", short, ""), ("eligibility_log", "ts", short, ""),
                                         ("health", "ts", long_, ""), ("decisions", "ts", long_, "AND decision='HOLD'"),
                                         ("signals", "ts", long_, "AND decision IN ('WATCH','NO_EDGE','REJECTED')")):
            n += self.db.execute(f"DELETE FROM {table} WHERE {col} < ? {where}", (now - days * 86400,)).rowcount
        n += self.db.execute("DELETE FROM signal_outcomes WHERE signal_id NOT IN (SELECT id FROM signals)").rowcount
        log.info("state retention removed %d rows", n)

    def _write_status(self, now: float) -> None:
        e = self.engine
        st = self.http.stats
        eq = e.risk.equity(self.venue.balance_usd() if self.venue.is_real else None)
        e.risk.peak_equity = max(e.risk.peak_equity, eq)
        e.risk.record_equity(now)
        status = {
            "mode": self.mode, "run_id": self.run_id, "version": __version__,
            "data": "CONNECTED" if now - self.last_quote_ok < 30 else "DISCONNECTED",
            "execution": "LIVE" if self.venue.is_real else ("SHADOW (simulated)" if self.mode == "SHADOW" else "PAPER (simulated)"),
            "kalshi": "CONNECTED" if now - st.last_ok_ts < 30 else "NOT CONNECTED",
            "production_key": "CONFIGURED" if credentials_present(self.cfg)[0] else "NOT CONFIGURED",
            "production_orders": "ENABLED" if self.venue.is_real else "DISABLED",
            "universe": len(self.scanner.markets), "probe": len(self.scanner.probe), "hot": len(self.scanner.hot),
            "equity": round(eq, 2), "daily_pnl": round(e.risk.daily_pnl(now), 2),
            "req_per_s": round(st.req_per_s(), 2), "http_429": st.http_429, "request_errors": st.errors,
            "clock_skew_s": round(st.clock_skew_s, 2), "cycle_ms": round(self.cycle_ms, 1),
            "tape_rows": self.tape.rows_written,
            "live_score_events": sum(1 for st in self.ctx_state.values() if st.status == "live"),
            "companions": len(self.companions), "sport_events": self.sport_events_seen,
            "health_state": self.guard.status(), **e.status(),
        }
        if self.edge_engine is not None:
            e2 = self.edge_engine
            status["edge_engine"] = {"env": e2.env, "version": e2.edge.version,
                                     "equity": round(e2.risk.equity(None), 2),
                                     "daily_pnl": round(e2.risk.daily_pnl(now), 2),
                                     "modelled_markets": len(e2.edge.refs.market_match),
                                     "live_matches": sum(1 for m in e2.edge.refs.matches.values() if m.state is not None),
                                     **e2.status()}
        self.db.insert("status", {"k": "engine", "ts": now, "v": status}, replace=True)
        rows = e.scanner_json()
        if self.edge_engine is not None:
            for r in rows:
                r.update(self.edge_engine.edge.row_extras(r["ticker"], now, None if r.get("bid") is None or r.get("ask") is None
                                                          else (r["bid"] + r["ask"]) / 2))
        hot = set(self.scanner.hot)
        self.db.executemany("INSERT OR REPLACE INTO scanner_state VALUES (?,?,?)",
                            [(r["ticker"], now, json.dumps(r, default=str)) for r in rows if r["ticker"] in hot])
        self.db.execute("DELETE FROM scanner_state WHERE ts < ?", (now - 60,))

    def _health(self, now: float) -> None:
        cfg, e = self.cfg, self.engine
        s = self.monitor.sample(cfg.path(cfg.app.data_dir), cfg.path(cfg.app.state_db), cfg.path(cfg.app.tape_db))
        st = self.http.stats
        ages = [now - ser.last_snapshot.ts for t, ser in e.series.items() if t in self.scanner.hot and ser.last_snapshot]
        row = {"ts": now, "run_id": self.run_id, **s, "requests": st.requests, "request_errors": st.errors,
               "http_429": st.http_429, "req_per_s": round(st.req_per_s(), 2), "websockets": 0,
               "max_data_age_s": round(max(ages), 1) if ages else None,
               "open_positions": sum(len(x.book.open_positions()) for x, _, _ in self._engines()),
               "open_orders": sum(len(x.om.working) for x, _, _ in self._engines()),
               "fail_closed": e.risk.fail_closed, "loop_ms": self.cycle_ms}
        self.db.insert("health", row)
        if s["disk_free_gb"] < cfg.health.min_free_disk_gb:
            self.tape.enabled = False
            self.alerter.fire("RESOURCE", f"disk free {s['disk_free_gb']}GB - tape recording paused", "CRITICAL")
        else:
            self.tape.enabled = True
        if s["rss_mb"] > cfg.health.max_rss_mb:
            self.alerter.fire("RESOURCE", f"RSS {s['rss_mb']}MB > {cfg.health.max_rss_mb}MB")
        if now - self.last_quote_ok > cfg.health.stale_data_alert_s:
            self.alerter.fire("STALE_DATA", f"no quotes for {now - self.last_quote_ok:.0f}s", "CRITICAL")
        if st.http_429 and self._due("429alert", 3600, now):
            self.alerter.fire("API_ERRORS", f"{st.http_429} HTTP 429 responses so far", key="429")

    # ------------------------------------------------------------ run
    def run(self) -> int:
        signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
        signal.signal(signal.SIGINT, lambda *_: self.stop.set())
        self.startup()
        threading.Thread(target=self.discovery_loop, name="discovery", daemon=True).start()
        if self.cfg.dashboard.enabled:
            from lvt.dashboard import serve
            threading.Thread(target=serve, args=(self.cfg, self.stop), name="dashboard", daemon=True).start()
        interval = self.cfg.discovery.quote_interval_s
        exit_code = 0
        while not self.stop.is_set():
            t0 = time.time()
            try:
                self.cycle()
                self.consecutive_failures = 0
                self.engine.risk.fail_closed.pop("DATABASE", None)
                self.engine.risk.fail_closed.pop("ENGINE_ERROR", None)
            except SystemExit:
                raise
            except sqlite3.Error as exc:
                self.consecutive_failures += 1
                self.engine.risk.fail_closed["DATABASE"] = str(exc)[:200]
                self.alerter.fire("DATABASE_FAILURE", f"database error: {exc}", "CRITICAL")
            except Exception:                                 # noqa: BLE001
                self.consecutive_failures += 1
                self.engine.risk.fail_closed["ENGINE_ERROR"] = "cycle raised; see log"
                log.error("cycle error:\n%s", traceback.format_exc())
            if self.consecutive_failures >= 30:
                log.critical("30 consecutive cycle failures - exiting for a clean restart")
                exit_code = 1
                break
            self.cycle_ms = (time.time() - t0) * 1000.0
            self.guard.on_cycle(self.cycle_ms)
            self.stop.wait(max(0.0, interval - (time.time() - t0)))
        self.shutdown()
        return exit_code

    def shutdown(self) -> None:
        self.stop.set()
        try:
            self.db.execute("UPDATE runs SET ended_ts=? WHERE run_id=?", (time.time(), self.run_id))
            self.alerter.fire("BOT_STOPPED", f"stopped ({self.run_id})", "WARNING")
        except Exception:                                     # noqa: BLE001
            pass
        log.info("shutdown complete")
