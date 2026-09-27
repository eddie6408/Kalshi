"""Readiness validations.

`run_selftest`  (offline): risk-control scenarios and simulated restart recovery.
`run_demo_execution_validation` (Kalshi DEMO exchange, fake money): order placement
   and cancel, duplicate client_order_id protection, partial-fill handling,
   reconciliation and restart recovery against the real exchange API.

IMPORTANT: the demo validation must use a DEMO ACCOUNT DEDICATED TO THIS BOT.
Placing orders on a demo account another bot uses would show up in that bot's
reconciliation - never do that.
"""
from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .engine.fees import FeeModel
from .engine.volatility import Features
from .execution.paper import PaperExecution
from .models import Direction, MarketMeta, MarketSnapshot, Order, OrderStatus, Position, Signal
from .portfolio.positions import PositionManager
from .portfolio.reconcile import reconcile_live, reconcile_simulated
from .risk.engine import RiskEngine
from .storage.db import Database


def _sig(ticker="T1", score=80.0, entry=50.0, stop=45.0, strategy="DIP") -> Signal:
    return Signal(ts=time.time(), ticker=ticker, strategy=strategy, strategy_version="TEST", direction=Direction.UP,
                  action="BUY", entry_ref=entry, target=entry + 6, stop=stop, score=score, expected_gross=6,
                  expected_costs=2, expected_net=4)


def _feat(ticker="T1", spread=1.0, depth=500.0) -> Features:
    return Features(ticker=ticker, ts=time.time(), samples=50, price=49.5, bid=49.0, ask=50.0, spread=spread,
                    bid_depth=depth, ask_depth=depth, data_age=0.5)


def run_selftest(cfg, record_db: Database | None = None) -> dict[str, Any]:
    results: dict[str, bool] = {}
    now = time.time()
    meta = MarketMeta(ticker="T1", event_ticker="E1")
    r = RiskEngine(cfg.risk, cfg.app.timezone)
    d = r.check_entry(_sig(), meta, _feat(), [], 0.0, now)
    results["approves_clean_entry"] = d.approved and d.qty >= 1
    results["size_within_max_contracts"] = d.qty <= cfg.risk.max_position_contracts
    results["risk_per_trade_capped"] = d.qty * (50 - 45 + 2) / 100 <= cfg.risk.max_risk_per_trade + 1e-9
    r.set_kill_switch(True, "test")
    results["kill_switch_blocks"] = not r.check_entry(_sig(), meta, _feat(), [], 0.0, now).approved
    r.set_kill_switch(False)
    r.realized_today = -cfg.risk.max_daily_loss - 1
    results["daily_loss_blocks"] = not r.check_entry(_sig(), meta, _feat(), [], 0.0, now).approved
    r2 = RiskEngine(cfg.risk, cfg.app.timezone)
    results["wide_spread_blocks"] = not r2.check_entry(_sig(), meta, _feat(spread=10), [], 0.0, now).approved
    results["thin_book_blocks"] = not r2.check_entry(_sig(), meta, _feat(depth=5), [], 0.0, now).approved
    held = Position(env="PAPER", ticker="T1", event_ticker="E1", side="yes", direction=Direction.UP, strategy="DIP",
                    strategy_version="TEST", signal_id=None, target=56, stop=45, max_hold_seconds=600, qty=5, avg_entry=50)
    results["duplicate_market_blocks"] = not r2.check_entry(_sig(), meta, _feat(), [held], 0.0, now).approved
    results["pending_order_blocks"] = not r2.check_entry(_sig(), meta, _feat(), [], 0.0, now, {"T1"}).approved
    other = MarketMeta(ticker="T2", event_ticker="E1")
    results["correlated_event_blocks"] = not r2.check_entry(_sig("T2"), other, _feat("T2"), [held], 0.0, now).approved
    r3 = RiskEngine(cfg.risk, cfg.app.timezone)
    for i in range(cfg.risk.max_trades_per_hour):
        r3.on_entry(f"X{i}", now)
    results["trade_frequency_blocks"] = not r3.check_entry(_sig(), meta, _feat(), [], 0.0, now).approved
    r4 = RiskEngine(cfg.risk, cfg.app.timezone)
    r4.halt("unknown order")
    results["halt_blocks"] = not r4.check_entry(_sig(), meta, _feat(), [], 0.0, now).approved

    # restart recovery (simulated ledger)
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "t.sqlite3")
        risk = RiskEngine(cfg.risk, cfg.app.timezone)
        pm = PositionManager(db, "PAPER", risk, "selftest")
        ex = PaperExecution(cfg.paper, FeeModel.from_config(cfg.fees), seed=1)
        ex.cfg = SimpleNamespace(**{**cfg.paper.as_dict(), "reject_probability": 0.0})
        pos, order = pm.open_from_signal(_sig(), meta, 5, "NORMAL_VOLATILITY", _feat(), 3, now)
        snap = MarketSnapshot(ticker="T1", ts=now + 5, yes_bid=49, yes_ask=50, yes_bid_size=500, yes_ask_size=500)

        async def go():
            for ev in await ex.submit(order, meta, now):
                pm.on_event(ev, now)
            for ev in await ex.on_market("T1", snap, None, [], now + 5, meta):
                pm.on_event(ev, now + 5)
        asyncio.run(go())
        risk2 = RiskEngine(cfg.risk, cfg.app.timezone)
        pm2 = PositionManager(db, "PAPER", risk2, "selftest2")
        loaded = pm2.load_open()
        rec = reconcile_simulated(db, "PAPER", loaded, restart=True)
        results["restart_recovers_open_position"] = len(loaded) == 1 and loaded[0].qty == 5 and rec.ok
        db.close()

    risk_ok = all(v for k, v in results.items() if k != "restart_recovers_open_position")
    if record_db is not None:
        record_db.record_validation("RISK_SELFTEST", risk_ok, {k: v for k, v in results.items()})
        record_db.record_validation("RESTART_RECOVERY_TEST", results["restart_recovers_open_position"],
                                    {"scope": "simulated ledger"})
    return {"passed": all(results.values()), "results": results}


async def run_demo_execution_validation(cfg, record_db: Database, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Exercises the LIVE execution path against Kalshi's DEMO exchange with 1-contract orders."""
    from .data.base import RateLimiter
    from .data.kalshi_rest import KalshiRestClient
    from .data.parsing import parse_market_snapshot
    from .execution.kalshi_auth import KalshiSigner
    from .execution.live import KalshiLiveExecution

    signer = KalshiSigner.from_env("demo", env)
    fees = FeeModel.from_config(cfg.fees)
    base = cfg.kalshi.demo_rest_base_url
    ex = KalshiLiveExecution(cfg.live, fees, signer, base, env_name="DEMO")
    pub = KalshiRestClient(base, RateLimiter(3))
    out: dict[str, Any] = {}
    try:
        bal = await ex.balance()
        out["balance_ok"] = "balance" in bal
        data = await pub.get("/markets", {"status": "open", "limit": 200, "mve_filter": "exclude"})
        ts = time.time()
        snaps = [parse_market_snapshot(m, ts) for m in data.get("markets") or []]
        snaps = [s for s in snaps if s.yes_bid and s.yes_ask and 10 <= s.yes_ask <= 90]
        if not snaps:
            raise RuntimeError("no quoted demo market found")
        s = snaps[0]
        meta = MarketMeta(ticker=s.ticker)
        # 1) resting post-only far from the market, then cancel
        o1 = Order(env="DEMO", ticker=s.ticker, side="yes", action="buy", count=1, limit_price=1.0,
                   purpose="ENTRY", style="maker", expires_ts=time.time() + 120)
        await ex.submit(o1, meta, time.time())
        placed = o1.status in (OrderStatus.RESTING, OrderStatus.SUBMITTED) and o1.exchange_order_id
        await ex.cancel(o1, time.time())
        out["EXECUTION_TEST"] = bool(placed) and o1.status in (OrderStatus.CANCELED, OrderStatus.MISSED)
        # 2) duplicate client_order_id is refused and nothing doubles
        o2 = Order(env="DEMO", ticker=s.ticker, side="yes", action="buy", count=1, limit_price=1.0,
                   purpose="ENTRY", style="maker", expires_ts=time.time() + 120)
        await ex.submit(o2, meta, time.time())
        dup = Order(env="DEMO", ticker=s.ticker, side="yes", action="buy", count=1, limit_price=1.0,
                    purpose="ENTRY", style="maker", client_order_id=o2.client_order_id)
        await ex.submit(dup, meta, time.time())
        matches = [o for o in await ex.orders(ticker=s.ticker, min_ts=time.time() - 300)
                   if o.get("client_order_id") == o2.client_order_id]
        out["DUPLICATE_ORDER_TEST"] = dup.status in (OrderStatus.REJECTED, OrderStatus.UNKNOWN) and len(matches) == 1
        await ex.cancel(o2, time.time())
        # 3) partial fill: IOC for more than the displayed best-ask size, at the best ask
        book = await pub.get(f"/markets/{s.ticker}/orderbook", {"depth": 3})
        from .data.parsing import parse_orderbook
        ob = parse_orderbook(book, time.time())
        ask = ob.best_yes_ask()
        partial_ok = None
        if ask and ask.size <= 20:
            o3 = Order(env="DEMO", ticker=s.ticker, side="yes", action="buy", count=int(ask.size) + 3,
                       limit_price=ask.price, purpose="ENTRY", style="taker")
            await ex.submit(o3, meta, time.time())
            partial_ok = 0 < o3.filled < o3.count and o3.status is OrderStatus.CANCELED
            if o3.filled:
                bid = ob.best_yes_bid()
                o4 = Order(env="DEMO", ticker=s.ticker, side="yes", action="sell", count=o3.filled,
                           limit_price=max(1.0, (bid.price if bid else 1.0) - 5), purpose="EXIT", style="taker")
                await ex.submit(o4, meta, time.time())
        out["PARTIAL_FILL_TEST"] = partial_ok
        # 4) reconciliation + restart recovery: a fresh client must see a consistent account
        ex2 = KalshiLiveExecution(cfg.live, fees, signer, base, env_name="DEMO")
        res = await reconcile_live(ex2, [], record_db, "DEMO")
        # local ledger is empty, so every non-zero exchange position must be flagged as a mismatch
        mism = [i for i in res.issues if i.startswith("POSITION MISMATCH")]
        out["RECONCILIATION_TEST"] = ("exchange_positions" in res.info
                                      and len(mism) == res.info.get("exchange_positions"))
        out["RESTART_RECOVERY_TEST"] = "exchange unreachable" not in " ".join(res.issues)
        out["reconcile_issues"] = res.issues
        await ex2.close()
    finally:
        await ex.close()
        await pub.close()
    for k in ("EXECUTION_TEST", "DUPLICATE_ORDER_TEST", "PARTIAL_FILL_TEST", "RECONCILIATION_TEST",
              "RESTART_RECOVERY_TEST"):
        if out.get(k) is not None:
            record_db.record_validation(k, bool(out[k]), {"exchange": "DEMO", **{kk: str(v) for kk, v in out.items()}})
    return out
