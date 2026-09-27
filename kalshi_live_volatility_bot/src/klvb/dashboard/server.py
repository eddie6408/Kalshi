"""Dedicated dashboard for this bot (FastAPI, same process, read-only).

Bound to 127.0.0.1 by default - open it through an SSH tunnel:
    ssh -L 8765:127.0.0.1:8765 user@your-vps   then browse http://localhost:8765
If KLVB_DASHBOARD_TOKEN is set, every request must carry ?token=... or an
X-Dashboard-Token header. The API key is never exposed: only "CONFIGURED" /
"NOT CONFIGURED".
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from ..backtest.metrics import evidence_note, full_report

STATIC = Path(__file__).parent / "static"


def create_app(bot) -> FastAPI:
    app = FastAPI(title="Kalshi Live Volatility Trader", docs_url=None, redoc_url=None)
    token = os.environ.get("KLVB_DASHBOARD_TOKEN", "")

    @app.middleware("http")
    async def auth(request: Request, call_next):
        if token and request.query_params.get("token") != token and request.headers.get("x-dashboard-token") != token:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status")
    async def status():
        return bot.status()

    @app.get("/api/scanner")
    async def scanner():
        return bot.trader.scanner_rows()

    @app.get("/api/positions")
    async def positions():
        return bot.trader.position_rows(time.time())

    @app.get("/api/performance")
    async def performance(env: str = "PAPER", days: float | None = None):
        env = env.upper()
        if env not in ("PAPER", "SHADOW", "LIVE"):
            raise HTTPException(400, "env must be PAPER, SHADOW or LIVE")
        since = time.time() - days * 86400 if days else None
        rep = full_report(bot.db, env, since)
        rep["evidence"] = evidence_note(rep["summary"])
        return rep

    @app.get("/api/signals")
    async def signals(limit: int = 50):
        return bot.db.query("SELECT ts, ticker, strategy, strategy_version, side, action, score, entry_ref, target, "
                            "stop, expected_net, reject_reasons, explanation FROM signals WHERE env=? "
                            "ORDER BY ts DESC LIMIT ?", (bot.env, min(limit, 500)))

    @app.get("/api/trades")
    async def trades(limit: int = 50):
        return bot.db.query("SELECT * FROM trades WHERE env=? ORDER BY closed_ts DESC LIMIT ?",
                            (bot.env, min(limit, 500)))

    @app.get("/api/alerts")
    async def alerts(limit: int = 50):
        return bot.db.query("SELECT ts, level, kind, message FROM alerts ORDER BY ts DESC LIMIT ?", (min(limit, 500),))

    @app.get("/api/health")
    async def health():
        rows = bot.db.query("SELECT * FROM system_health ORDER BY ts DESC LIMIT 1")
        return rows[0] if rows else {}

    return app


async def serve_dashboard(bot) -> None:
    import uvicorn

    cfg = bot.cfg.dashboard
    server = uvicorn.Server(uvicorn.Config(create_app(bot), host=cfg.host, port=int(cfg.port), log_level="warning",
                                           access_log=False))
    server.install_signal_handlers = lambda: None  # the application owns signals
    await server.serve()
