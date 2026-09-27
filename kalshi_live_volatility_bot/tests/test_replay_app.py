"""Deterministic replay, no look-ahead, walk-forward plumbing, and a full application run
against a mock Kalshi public API (including the dashboard endpoints)."""
import asyncio
import json
import math
import random
import time

import httpx

from conftest import T0

from klvb.backtest.replay import ReplayEngine, load_recorded, meta_row, snapshot_row
from klvb.backtest.synthetic import PATTERNS, generate
from klvb.backtest.walkforward import walk_forward
from klvb.storage.db import Database


def _synthetic_db(path):
    db = Database(path)
    for i, pat in enumerate(PATTERNS):
        meta, snaps = generate(f"SYN-{pat}", pat, T0, seed=i, event_ticker=f"EV{i}")
        db.upsert("markets", meta_row(meta, T0), key="ticker")
        db.insert_many("snapshots", [snapshot_row(s) for s in snaps])
    return db


async def test_replay_is_deterministic(cfg, tmp_path):
    p = tmp_path / "rec.sqlite3"
    _synthetic_db(p)
    runs = []
    for _ in range(2):
        metas, events = load_recorded(p, allow_synthetic=True)
        r = await ReplayEngine(cfg).run(metas, events)
        runs.append([(t["ticker"], t["strategy"], round(t["net_pnl"], 6), t["qty"]) for t in r.trades])
    assert runs[0] == runs[1] and runs[0]


async def test_synthetic_data_excluded_by_default(cfg, tmp_path):
    p = tmp_path / "rec.sqlite3"
    _synthetic_db(p)
    metas, events = load_recorded(p)
    r = await ReplayEngine(cfg).run(metas, events)
    assert r.summary["total_trades"] == 0


async def test_replay_decisions_do_not_see_the_future(cfg, tmp_path):
    """Changing prices AFTER time X must not change any decision made before X."""
    base = tmp_path / "a.sqlite3"
    alt = tmp_path / "b.sqlite3"
    _synthetic_db(base)
    db2 = _synthetic_db(alt)
    cutoff = T0 + 500
    db2.execute("UPDATE snapshots SET yes_bid = 5, yes_ask = 6, book = NULL WHERE ts > ?", (cutoff,))
    sigs = []
    for p in (base, alt):
        out = Database(":memory:")
        metas, events = load_recorded(p, allow_synthetic=True)
        await ReplayEngine(cfg, out).run(metas, events)
        sigs.append(out.query("SELECT ticker, strategy, ts, entry_ref, score FROM signals WHERE ts <= ? ORDER BY ts, "
                              "ticker, strategy", (cutoff,)))
    assert sigs[0] == sigs[1]


async def test_walkforward_runs_and_does_not_record_synthetic(cfg, tmp_path, db):
    p = tmp_path / "rec.sqlite3"
    _synthetic_db(p)
    res = await walk_forward(cfg, str(p), grid={"costs.min_net_edge_cents": [1.0, 2.0]}, min_trades=1,
                             record_db=db, allow_synthetic=True)
    assert res["ok"] and res["grid_size"] == 2 and "oos_summary" in res
    assert db.latest_validation("OUT_OF_SAMPLE") is None


class FakeKalshiPublic:
    """Serves /markets, /markets/{t}/orderbook, /markets/trades, /series, /milestones from a random walk."""

    def __init__(self):
        self.t0 = time.time()
        self.rng = random.Random(3)
        self.tickers = [f"KXNBAGAME-26SEP27-T{i}" for i in range(4)]

    def mid(self, i):
        el = time.time() - self.t0
        return 50 + 8 * math.sin(el / 3 + i)

    def market(self, i):
        m = self.mid(i)
        bid = int(m)
        return {"ticker": self.tickers[i], "event_ticker": f"KXNBAGAME-26SEP27-G{i}", "status": "active",
                "yes_bid_dollars": f"{bid / 100:.4f}", "yes_ask_dollars": f"{(bid + 1) / 100:.4f}",
                "last_price_dollars": f"{bid / 100:.4f}", "volume_fp": f"{1000 + int((time.time() - self.t0) * 50)}.00",
                "volume_24h_fp": "90000.00", "open_interest_fp": "20000.00", "yes_bid_size_fp": "800.00",
                "yes_ask_size_fp": "800.00",
                "close_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 7200))}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/trade-api/v2", "")
        if path == "/markets":
            t = request.url.params.get("tickers")
            idx = [self.tickers.index(x) for x in t.split(",")] if t else range(len(self.tickers))
            return httpx.Response(200, json={"markets": [self.market(i) for i in idx], "cursor": ""})
        if path.endswith("/orderbook"):
            i = self.tickers.index(path.split("/")[2])
            bid = int(self.mid(i))
            return httpx.Response(200, json={"orderbook_fp": {
                "yes_dollars": [[f"{(bid - k) / 100:.4f}", "800.00"] for k in range(3)][::-1],
                "no_dollars": [[f"{(99 - bid - k) / 100:.4f}", "800.00"] for k in range(3)][::-1]}})
        if path == "/markets/trades":
            return httpx.Response(200, json={"trades": [], "cursor": ""})
        if path == "/series":
            cat = request.url.params.get("category")
            return httpx.Response(200, json={"series": [{"ticker": "KXNBAGAME", "category": "Sports",
                                                         "title": "Pro Basketball Game", "tags": ["Basketball"],
                                                         "fee_type": "quadratic", "fee_multiplier": 1}]
                                             if cat == "Sports" else []})
        if path == "/milestones":
            return httpx.Response(200, json={"milestones": []})
        return httpx.Response(404, json={})


async def test_application_runs_paper_end_to_end(tmp_path, monkeypatch):
    from klvb import app as app_mod
    from klvb.config import load_config
    from klvb.dashboard.server import create_app
    from klvb.data.base import RateLimiter
    from klvb.data.kalshi_rest import KalshiPublicRestProvider, KalshiRestClient

    fake = FakeKalshiPublic()

    class TestProvider(KalshiPublicRestProvider):
        def __init__(self, cfg):
            super().__init__(cfg, KalshiRestClient(cfg.kalshi.rest_base_url, RateLimiter(1000),
                                                   transport=httpx.MockTransport(fake.handler)))

    monkeypatch.setattr(app_mod, "KalshiPublicRestProvider", TestProvider)
    cfg = load_config(env={"KLVB_DATA_DIR": str(tmp_path / "d"), "KLVB_LOG_DIR": str(tmp_path / "l")},
                      overrides={"data": {"watchlist_poll_seconds": 0.2, "orderbook_poll_seconds": 0.3,
                                          "trades_poll_seconds": 0.5, "market_refresh_seconds": 1.0},
                                 "app": {"loop_interval_seconds": 0.2}, "dashboard": {"enabled": False},
                                 "health": {"interval_seconds": 0.5}})
    bot = app_mod.Application(cfg, env={})
    task = asyncio.create_task(bot.run())
    await asyncio.sleep(4)
    st = bot.status()
    assert st["mode"] == "PAPER" and st["production_orders"] == "DISABLED"
    assert st["production_key"] == "NOT CONFIGURED" and st["data"] == "CONNECTED"
    assert st["discovery"]["candidates"] == 4
    rows = bot.trader.scanner_rows()
    assert len(rows) == 4 and rows[0]["sport"] == "BASKETBALL"
    # dashboard API (in-process)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url="http://t")
    assert (await client.get("/api/status")).json()["mode"] == "PAPER"
    assert len((await client.get("/api/scanner")).json()) == 4
    perf = (await client.get("/api/performance?env=LIVE")).json()
    assert perf["environment"] == "LIVE" and "REAL MONEY" in perf["label"]
    assert (await client.get("/api/performance?env=BACKTEST")).status_code == 400
    await client.aclose()
    # kill switch file is picked up
    cfg.kill_switch_file.write_text("test")
    await asyncio.sleep(0.6)
    assert bot.risk.kill_switch
    bot.stopping.set()
    await asyncio.wait_for(task, 10)
    db = Database(cfg.db_path)
    assert db.scalar("SELECT COUNT(*) FROM snapshots") > 10
    assert db.scalar("SELECT COUNT(*) FROM market_states") >= 4
    assert db.scalar("SELECT COUNT(*) FROM system_health") >= 1
    kinds = {r["kind"] for r in db.query("SELECT kind FROM alerts")}
    assert {"BOT_STARTED", "KILL_SWITCH", "BOT_STOPPED"} <= kinds
    assert db.scalar("SELECT ended_ts FROM runs") is not None
