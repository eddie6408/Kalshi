"""Live application: wires data -> trader -> execution, runs the loops, the dashboard,
monitoring, reconciliation and the kill switch.

Initial deployment state:  RUNNING / PAPER / live market data / simulated execution /
production orders DISABLED / production key NOT REQUIRED.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import time
import uuid
from typing import Any

from . import __version__
from .backtest.replay import book_to_json, meta_row, snapshot_row
from .config import Config, TradingMode
from .data.base import DataUnavailable, FailoverMarketDataProvider, NullSportsProvider, market_raw
from .data.kalshi_rest import KalshiPublicRestProvider
from .data.sports import KalshiMilestoneSportsProvider
from .engine.fees import FeeModel
from .engine.scanner import build_candidates, select_watchlist
from .execution.factory import LiveModeUnavailable, build_execution
from .execution.kalshi_auth import credentials_present
from .models import MarketSnapshot
from .monitoring.alerts import Alerter
from .monitoring.health import HealthMonitor
from .monitoring.logs import event, setup_logging
from .portfolio.positions import PositionManager
from .portfolio.reconcile import reconcile_live, reconcile_simulated
from .risk.engine import RiskEngine
from .storage.db import Database
from .strategies import build_strategies
from .trader import Trader

log = logging.getLogger("klvb.app")


class Application:
    def __init__(self, cfg: Config, env: dict[str, str] | None = None):
        self.cfg = cfg
        self.env_vars = dict(os.environ) if env is None else env
        self.mode: TradingMode = cfg.mode
        self.env = self.mode.value
        self.run_id = f"run_{uuid.uuid4().hex[:12]}"
        self.started_ts = time.time()
        self.stopping = asyncio.Event()
        self.data_connected = False
        self.last_data_ok = 0.0
        self.last_snapshot_store: dict[str, tuple[float, tuple]] = {}
        self.trade_cursor: dict[str, float] = {}
        self.candidates: list = []
        self.discovery_stats: dict[str, Any] = {}
        self.loop_lag_ms = 0.0
        self.reconciled = False
        self.last_reconcile: dict[str, Any] = {}
        self.tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------------ setup
    async def setup(self) -> None:
        cfg = self.cfg
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        setup_logging(cfg.log_dir)
        self.db = Database(cfg.db_path)
        self.alerter = Alerter(self.db, cfg.alerts, self.env_vars)
        self.fees = FeeModel.from_config(cfg.fees)
        rest = KalshiPublicRestProvider(cfg)
        providers = []
        if cfg.data.use_websocket and credentials_present("data", self.env_vars):
            from .data.kalshi_ws import KalshiWebsocketProvider
            from .execution.kalshi_auth import KalshiSigner
            providers.append(KalshiWebsocketProvider(cfg, KalshiSigner.from_env("data", self.env_vars), rest))
        providers.append(rest)
        self.rest = rest
        self.data = FailoverMarketDataProvider(providers)
        self.sports = (KalshiMilestoneSportsProvider(rest.client) if cfg.data.sports_context_enabled
                       else NullSportsProvider())
        if self.mode is not TradingMode.LIVE and credentials_present("prod", self.env_vars):
            self.alerter.alert("PRODUCTION_CREDENTIALS_DETECTED",
                               f"production credentials are configured but mode is {self.env}; they are NOT loaded",
                               "WARNING")
        try:
            self.execution = build_execution(cfg, self.fees, self.data, self.db, self.env_vars)
        except LiveModeUnavailable as e:
            self.alerter.alert("LIVE_MODE_ACTIVATED", "LIVE requested but unavailable - refusing to start", "CRITICAL")
            log.critical(str(e))
            raise
        if self.mode is TradingMode.LIVE:
            self.alerter.alert("LIVE_MODE_ACTIVATED", "LIVE trading with real money is ACTIVE", "CRITICAL")
        self.risk = RiskEngine(cfg.risk, cfg.app.timezone)
        self.pm = PositionManager(self.db, self.env, self.risk, self.run_id, cfg.app.timezone)
        self.trader = Trader(cfg, self.db, self.env, self.run_id, self.execution, build_strategies(cfg), self.risk,
                             self.pm, self.fees, self.alerter)
        self.health = HealthMonitor(cfg.health, self.db, cfg.data_dir)
        self.db.insert("runs", {"id": self.run_id, "started_ts": self.started_ts, "mode": self.env,
                                "config_hash": cfg.fingerprint(),
                                "strategy_versions": {s.name: s.version for s in self.trader.strategies},
                                "app_version": __version__, "host": socket.gethostname(),
                                "notes": ",".join(cfg.source_files)})
        await self.data.start()
        await self.recover()

    async def recover(self) -> None:
        """Restart recovery: rebuild state, reconcile, and only then allow trading."""
        now = time.time()
        positions = self.pm.load_open()
        self.pm.restore_risk(now)
        if self.execution.live:
            self.risk.halt("awaiting startup reconciliation")
            res = await reconcile_live(self.execution, positions, self.db, self.env)
        else:
            res = reconcile_simulated(self.db, self.env, positions, restart=True)
            for p in positions:
                self.pm.persist_position(p, now)
        self.last_reconcile = {"ts": now, "ok": res.ok, "issues": res.issues, "info": res.info}
        event(log, "startup reconciliation", ok=res.ok, issues=res.issues, info=res.info,
              open_positions=len(positions))
        if res.ok:
            self.risk.clear_halt()
            self.reconciled = True
        else:
            self.risk.halt("reconciliation failed: " + "; ".join(res.issues[:3]))
            self.alerter.alert("RECONCILIATION_FAILURE", "; ".join(res.issues[:5]), "CRITICAL")

    # ------------------------------------------------------------------ loops
    async def _loop(self, name: str, interval: float, fn) -> None:
        while not self.stopping.is_set():
            t0 = time.monotonic()
            try:
                await fn()
            except DataUnavailable as e:
                self.data_connected = False
                self.alerter.alert("DATA_DISCONNECTED", f"{name}: {e}", dedupe_key=f"data:{name}")
            except Exception as e:  # noqa: BLE001 - a loop must never die silently
                log.exception("loop %s failed", name)
                self.alerter.alert("API_ERRORS", f"{name}: {type(e).__name__}: {e}", dedupe_key=f"err:{name}")
            el = time.monotonic() - t0
            try:
                await asyncio.wait_for(self.stopping.wait(), timeout=max(0.05, interval - el))
            except asyncio.TimeoutError:
                pass

    async def discovery(self) -> None:
        markets = await self.data.discover_markets()
        now = time.time()
        sports_live = set(self.trader.sports)
        cands, rejected = build_candidates(markets, now, self.cfg.scanner, self.cfg.eligibility, sports_live)
        self.candidates = cands
        pinned = {p.ticker for p in self.pm.open_positions()} | {o.ticker for o in self.pm.working_orders()}
        watch = select_watchlist(cands, pinned, self.cfg.data.max_watchlist)
        by_t = {m.ticker: m for m, _ in markets}
        for c in cands[: self.cfg.data.max_watchlist * 2]:
            self.trader.set_meta(c.meta)
            self.db.upsert("markets", meta_row(c.meta, now), key="ticker")
        for t in pinned:
            if t in by_t:
                self.trader.set_meta(by_t[t])
        dropped = set(self.trader.watch) - set(watch)
        self.trader.set_watchlist(watch)
        for t in dropped:
            if t not in pinned:
                self.trader.vol.drop(t)
        self.discovery_stats = {"ts": now, "markets": len(markets), "candidates": len(cands),
                                "watch": len(self.trader.watch), "rejected_by": rejected}
        event(log, "discovery", **{k: v for k, v in self.discovery_stats.items() if k != "ts"})

    def _store_snapshot(self, s: MarketSnapshot) -> None:
        key = (s.yes_bid, s.yes_ask, s.last_price, s.volume, s.yes_bid_size, s.yes_ask_size, s.status)
        prev = self.last_snapshot_store.get(s.ticker)
        if prev and prev[1] == key and s.ts - prev[0] < self.cfg.data.snapshot_heartbeat_seconds:
            return
        self.last_snapshot_store[s.ticker] = (s.ts, key)
        row = snapshot_row(s)
        series = self.trader.vol.get(s.ticker)
        if series.book is not None and s.ts - series.book.ts < 10:
            row["book"] = book_to_json(series.book)
        self.db.insert("snapshots", row)

    async def poll_watchlist(self) -> None:
        tickers = self.trader.watch
        if not tickers:
            return
        snaps = await self.data.snapshots(tickers)
        now = time.time()
        self.data_connected, self.last_data_ok = True, now
        for s in snaps:
            await self.trader.on_snapshot(s, now)
            self._store_snapshot(s)
        await self._check_settlements(snaps, now)

    def _hot_tickers(self) -> list[str]:
        hot = [p.ticker for p in self.pm.open_positions()] + [o.ticker for o in self.pm.working_orders()]
        ranked = sorted(self.trader.scores.items(), key=lambda kv: kv[1].get("movement") or 0, reverse=True)
        hot += [t for t, sc in ranked if sc.get("eligible")][:6]
        hot += [t for t, _ in ranked][:4]
        return list(dict.fromkeys(t for t in hot if t in self.trader.watch or t in {p.ticker for p in self.pm.open_positions()}))

    async def poll_books(self) -> None:
        for t in self._hot_tickers()[:10]:
            book = await self.data.orderbook(t)
            if book is not None:
                self.trader.on_book(t, book)

    async def poll_trades(self) -> None:
        tickers = self.trader.watch
        if not tickers:
            return
        # round-robin a slice each cycle to stay inside the request budget
        start = int(time.time() / self.cfg.data.trades_poll_seconds) * 8 % max(len(tickers), 1)
        chunk = (tickers + tickers)[start:start + 8]
        hot = self._hot_tickers()[:4]
        for t in dict.fromkeys(hot + chunk):
            since = self.trade_cursor.get(t, time.time() - 120)
            trades = await self.data.trades(t, since)
            if trades:
                self.trade_cursor[t] = max(tp.ts for tp in trades) + 1e-3
                self.trader.on_trades(t, trades)
                self.db.insert_many("trade_prints", [{"trade_id": tp.trade_id or f"{t}-{tp.ts}-{tp.count}",
                                                      "ticker": t, "ts": tp.ts, "yes_price": tp.yes_price,
                                                      "count": tp.count, "taker_side": tp.taker_side}
                                                     for tp in trades], ignore=True)
            else:
                self.trade_cursor.setdefault(t, time.time() - 5)

    async def poll_sports(self) -> None:
        ctx = await self.sports.contexts()
        self.trader.on_sports(ctx)
        now = time.time()
        rows = [{"ts": now, "event_ticker": k, "sport": v.sport, "status": v.status,
                 "data": {"score": v.score, "period": v.period, "clock": v.clock, "competitors": v.competitors,
                          "extra": v.extra}, "source": v.source}
                for k, v in ctx.items() if k in {m.event_ticker for m in self.trader.metas.values()}]
        self.db.insert_many("sports_context", rows)

    async def _check_settlements(self, snaps: list[MarketSnapshot], now: float) -> None:
        closed = {s.ticker for s in snaps if s.status in ("closed", "settled", "determined", "finalized")}
        for pos in self.pm.open_positions():
            if pos.ticker not in closed or pos.qty <= 0:
                continue
            m = await market_raw(self.data, pos.ticker)
            result = str(m.get("result", "")).lower()
            if result in ("yes", "no"):
                rec = self.pm.settle(pos, 100.0 if result == pos.side else 0.0, now)
                self.alerter.alert("UNEXPECTED_POSITION", f"{pos.ticker} settled while held ({result}); "
                                                          f"net {rec['net_pnl'] / 100:.2f}")

    async def decide(self) -> None:
        t0 = time.monotonic()
        ks_file = self.cfg.kill_switch_file.exists()
        ks = self.cfg.kill_switch_env or ks_file
        if ks and not self.risk.kill_switch:
            reason = self.cfg.kill_switch_file.read_text().strip() if ks_file else "KILL_SWITCH=true"
            self.risk.set_kill_switch(True, reason or "manual")
            self.alerter.alert("KILL_SWITCH", f"kill switch ACTIVATED: {reason}", "CRITICAL")
            if self.cfg.live.cancel_orders_on_kill:
                for o in list(self.execution.working_orders()):
                    if o.purpose == "ENTRY":
                        ev = await self.execution.cancel(o, time.time())
                        self.pm.on_event(ev, time.time())
        elif not ks and self.risk.kill_switch:
            self.risk.set_kill_switch(False)
            self.alerter.alert("KILL_SWITCH", "kill switch cleared", "INFO")
        if time.time() - self.last_data_ok > self.cfg.data.emergency_data_age_seconds and self.last_data_ok:
            self.alerter.alert("STALE_DATA", "no market data for "
                               f"{time.time() - self.last_data_ok:.0f}s - no new trades", dedupe_key="global_stale")
        await self.trader.step(time.time())
        if self.risk.entries and len(self.risk.entries) >= self.cfg.alerts.max_trades_per_hour_alert:
            self.alerter.alert("EXCESSIVE_TRADE_FREQUENCY", f"{len(self.risk.entries)} entries in the last hour",
                               dedupe_key="freq")
        self.loop_lag_ms = (time.monotonic() - t0) * 1000

    async def reconcile_periodic(self) -> None:
        if not self.execution.live:
            return
        res = await reconcile_live(self.execution, self.pm.open_positions(), self.db, self.env)
        self.last_reconcile = {"ts": time.time(), "ok": res.ok, "issues": res.issues, "info": res.info}
        if res.ok:
            if self.risk.halted:
                self.risk.clear_halt()
        else:
            self.risk.halt("reconciliation failed: " + "; ".join(res.issues[:3]))
            self.alerter.alert("RECONCILIATION_FAILURE", "; ".join(res.issues[:5]), "CRITICAL")

    async def health_check(self) -> None:
        st = self.data.status()
        rest = self.rest.status()
        row = self.health.sample({"req_per_sec": rest.get("req_per_sec"), "rate_limited": rest.get("rate_limited"),
                                  "ws_connected": any(p.get("connected") for p in st.get("providers", [])),
                                  "data_connected": self.data_connected, "loop_lag_ms": round(self.loop_lag_ms, 1),
                                  "open_positions": len(self.pm.open_positions()), "provider": st,
                                  "counters": self.trader.counters})
        why = self.health.pressure(row)
        if why:
            self.alerter.alert("RESOURCE_PRESSURE", "; ".join(why), dedupe_key="pressure")
        if (rest.get("rate_limited") or 0) > 0:
            self.alerter.alert("API_ERRORS", f"rate limited {rest['rate_limited']} times", dedupe_key="ratelimit")

    async def prune(self) -> None:
        cutoff = time.time() - self.cfg.data.retention_days * 86400
        n = self.db.prune_snapshots(cutoff)
        if n:
            event(log, "pruned snapshots", rows=n)

    # ------------------------------------------------------------------ status views
    def status(self) -> dict[str, Any]:
        now = time.time()
        unreal = self.pm.unrealized_dollars(self.trader.features)
        return {
            "app": "kalshi_live_volatility_bot", "version": __version__, "run_id": self.run_id,
            "uptime_seconds": round(now - self.started_ts),
            "mode": self.env, "data": "CONNECTED" if self.data_connected else "DISCONNECTED",
            "execution": "LIVE" if self.execution.live else "SIMULATED (" + self.env + ")",
            "kalshi": "CONNECTED" if self.data_connected else "NOT CONNECTED",
            "production_key": "CONFIGURED" if credentials_present("prod", self.env_vars) else "NOT CONFIGURED",
            "production_orders": "ENABLED" if self.execution.live else "DISABLED",
            "reconciled": self.reconciled, "last_reconcile": self.last_reconcile,
            "risk": self.risk.status(unreal, now), "discovery": self.discovery_stats,
            "data_provider": self.data.status(), "counters": self.trader.counters,
            "loop_lag_ms": round(self.loop_lag_ms, 1),
        }

    # ------------------------------------------------------------------ run
    async def run(self) -> None:
        await self.setup()
        c = self.cfg
        self.alerter.alert("BOT_STARTED", f"started in {self.env} mode (run {self.run_id})", "INFO")
        loops = [
            ("discovery", c.data.market_refresh_seconds, self.discovery),
            ("watchlist", c.data.watchlist_poll_seconds, self.poll_watchlist),
            ("orderbooks", c.data.orderbook_poll_seconds, self.poll_books),
            ("trades", c.data.trades_poll_seconds, self.poll_trades),
            ("decide", c.app.loop_interval_seconds, self.decide),
            ("health", c.health.interval_seconds, self.health_check),
            ("prune", 3600, self.prune),
        ]
        if c.data.sports_context_enabled:
            loops.append(("sports", c.data.sports_context_poll_seconds, self.poll_sports))
        if self.execution.live:
            loops.append(("reconcile", c.live.reconcile_interval_seconds, self.reconcile_periodic))
        await self.discovery()
        for name, interval, fn in loops:
            self.tasks.append(asyncio.create_task(self._loop(name, float(interval), fn), name=name))
        if c.dashboard.enabled:
            from .dashboard.server import serve_dashboard
            self.tasks.append(asyncio.create_task(serve_dashboard(self), name="dashboard"))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.stopping.set)
            except NotImplementedError:  # pragma: no cover
                pass
        await self.stopping.wait()
        await self.shutdown()

    async def shutdown(self) -> None:
        log.info("shutting down")
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        # working simulated orders are canceled; LIVE resting orders are left to the exchange
        # (and reconciled at next start) unless the kill switch asked for cancellation
        if not self.execution.live:
            for o in list(self.execution.working_orders()):
                ev = await self.execution.cancel(o, time.time())
                self.pm.on_event(ev, time.time())
        self.db.execute("UPDATE runs SET ended_ts=? WHERE id=?", (time.time(), self.run_id))
        self.alerter.alert("BOT_STOPPED", f"stopped ({self.env})", "WARNING")
        await self.data.stop()
        await self.execution.close()
