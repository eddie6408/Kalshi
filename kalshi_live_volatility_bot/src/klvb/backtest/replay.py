"""Deterministic historical replay.

Recorded live market data (snapshots, depth books, trade prints) is streamed in
timestamp order through the SAME Trader, strategies, risk engine and paper fill
model used live. The simulated clock only advances forward:

    for each recorded event (ts ascending):
        run Trader.step(t) for every loop tick t < event.ts   (decisions use data <= t)
        deliver the event                                    (fills happen only on data AFTER the order)

so no decision can see a future price, and a paper order can only fill against
an observation that arrives after its modeled latency. With the same inputs,
config and seed, the output is identical.
"""
from __future__ import annotations

import heapq
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..config import Config, _deep_merge
from ..config import Config as _Cfg
from ..engine.fees import FeeModel
from ..execution.paper import PaperExecution
from ..models import BookLevel, MarketMeta, MarketSnapshot, OrderBook, TradePrint
from ..portfolio.positions import PositionManager
from ..risk.engine import RiskEngine
from ..storage.db import Database
from ..strategies import build_strategies
from ..trader import Trader
from .metrics import full_report, summarize


def book_to_json(book: OrderBook | None, levels: int = 5) -> dict | None:
    if book is None:
        return None
    return {"ts": book.ts, "yes": [[lv.price, lv.size] for lv in book.yes_bids[:levels]],
            "no": [[lv.price, lv.size] for lv in book.no_bids[:levels]]}


def book_from_json(d: Any, default_ts: float) -> OrderBook | None:
    if not d:
        return None
    if isinstance(d, str):
        d = json.loads(d)
    return OrderBook(ts=d.get("ts", default_ts), yes_bids=[BookLevel(p, s) for p, s in d.get("yes", [])],
                     no_bids=[BookLevel(p, s) for p, s in d.get("no", [])])


@dataclass(order=True)
class ReplayEvent:
    ts: float
    seq: int
    kind: str = field(compare=False)            # snap | trades
    payload: Any = field(compare=False)


def load_recorded(db_path: str | Path, start: float | None = None, end: float | None = None,
                  tickers: list[str] | None = None, allow_synthetic: bool = False
                  ) -> tuple[dict[str, MarketMeta], Iterator[ReplayEvent]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    metas = {}
    for r in conn.execute("SELECT * FROM markets"):
        metas[r["ticker"]] = MarketMeta(ticker=r["ticker"], event_ticker=r["event_ticker"] or "",
                                        series_ticker=r["series_ticker"] or "", title=r["title"] or "",
                                        category=r["category"] or "", sport=r["sport"] or "OTHER",
                                        close_ts=r["close_ts"], fee_type=r["fee_type"],
                                        fee_multiplier=r["fee_multiplier"])
    where, params = ["1=1"], []
    if start is not None:
        where.append("ts >= ?")
        params.append(start)
    if end is not None:
        where.append("ts < ?")
        params.append(end)
    if tickers:
        where.append(f"ticker IN ({','.join('?' for _ in tickers)})")
        params += tickers
    snap_where = list(where) + ([] if allow_synthetic else ["COALESCE(source,'') != 'synthetic'"])

    def snaps() -> Iterator[ReplayEvent]:
        cur = conn.execute(f"SELECT * FROM snapshots WHERE {' AND '.join(snap_where)} ORDER BY ts, id", params)
        for i, r in enumerate(cur):
            s = MarketSnapshot(ticker=r["ticker"], ts=r["ts"], yes_bid=r["yes_bid"], yes_ask=r["yes_ask"],
                               last_price=r["last_price"], volume=r["volume"], volume_24h=r["volume_24h"],
                               open_interest=r["open_interest"], yes_bid_size=r["yes_bid_size"],
                               yes_ask_size=r["yes_ask_size"], status=r["status"] or "active",
                               source=r["source"] or "", book=book_from_json(r["book"], r["ts"]))
            yield ReplayEvent(r["ts"], 2 * i + 1, "snap", s)

    def trades() -> Iterator[ReplayEvent]:
        cur = conn.execute(f"SELECT * FROM trade_prints WHERE {' AND '.join(where)} ORDER BY ts", params)
        for i, r in enumerate(cur):
            yield ReplayEvent(r["ts"], 2 * i, "trades",
                              TradePrint(r["ticker"], r["ts"], r["yes_price"], r["count"], r["taker_side"] or "",
                                         r["trade_id"]))

    return metas, heapq.merge(trades(), snaps())


@dataclass
class ReplayResult:
    env: str
    summary: dict[str, Any]
    report: dict[str, Any]
    counters: dict[str, int]
    config_hash: str
    trades: list[dict[str, Any]]


def cfg_with(cfg: Config, overrides: dict[str, Any] | None) -> Config:
    if not overrides:
        return cfg
    nested: dict[str, Any] = {}
    for dotted, v in overrides.items():
        cur = nested
        parts = dotted.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = v
    return _Cfg(raw=_deep_merge(cfg.raw, nested), source_files=cfg.source_files + ["<overrides>"])


class ReplayEngine:
    def __init__(self, cfg: Config, out_db: Database | None = None, env: str = "BACKTEST",
                 record_market_states: bool = False):
        self.cfg = cfg
        self.db = out_db or Database(":memory:")
        self.env = env
        self.record_states = record_market_states

    async def run(self, metas: dict[str, MarketMeta], events: Iterable[ReplayEvent]) -> ReplayResult:
        cfg = self.cfg
        run_id = f"bt_{uuid.uuid4().hex[:10]}"
        fees = FeeModel.from_config(cfg.fees)
        execution = PaperExecution(cfg.paper, fees, seed=cfg.app.random_seed)
        execution.env = self.env
        risk = RiskEngine(cfg.risk, cfg.app.timezone)
        pm = PositionManager(self.db, self.env, risk, run_id, cfg.app.timezone)
        trader = Trader(cfg, self.db, self.env, run_id, execution, build_strategies(cfg), risk, pm, fees,
                        alerter=None, record_market_states=self.record_states)
        for m in metas.values():
            trader.set_meta(m)
        interval = float(cfg.app.loop_interval_seconds)
        next_step: float | None = None
        seen: set[str] = set()
        last_ts = None
        for ev in events:
            if next_step is None:
                next_step = ev.ts
            while next_step < ev.ts:
                await trader.step(next_step)
                next_step += interval
            if ev.kind == "snap":
                s: MarketSnapshot = ev.payload
                if s.ticker not in seen:
                    seen.add(s.ticker)
                    if s.ticker not in trader.metas:
                        trader.set_meta(MarketMeta(ticker=s.ticker))
                    trader.set_watchlist(sorted(seen))
                if s.book is not None:
                    trader.on_book(s.ticker, s.book)
                await trader.on_snapshot(s, ev.ts)
            else:
                tp: TradePrint = ev.payload
                trader.on_trades(tp.ticker, [tp])
            last_ts = ev.ts
        if last_ts is not None:
            # close anything still open at the last observed price (marked as END_OF_DATA)
            trader.flatten_reason = "END_OF_DATA"
            await trader.step(last_ts + interval)
            for t in list(seen):
                s = trader.vol.get(t).last_snapshot
                if s is not None:
                    s2 = MarketSnapshot(**{**s.__dict__, "ts": last_ts + 2 * interval})
                    await trader.on_snapshot(s2, last_ts + 2 * interval)
        trades = self.db.query("SELECT * FROM trades WHERE env=? AND run_id=?", (self.env, run_id))
        return ReplayResult(env=self.env, summary=summarize(trades, cfg.risk.starting_paper_equity),
                            report=full_report(self.db, self.env), counters=dict(trader.counters),
                            config_hash=cfg.fingerprint(), trades=trades)


def snapshot_row(s: MarketSnapshot) -> dict[str, Any]:
    return {"ts": s.ts, "ticker": s.ticker, "yes_bid": s.yes_bid, "yes_ask": s.yes_ask, "last_price": s.last_price,
            "volume": s.volume, "volume_24h": s.volume_24h, "open_interest": s.open_interest,
            "yes_bid_size": s.yes_bid_size, "yes_ask_size": s.yes_ask_size, "status": s.status,
            "source": s.source, "book": book_to_json(s.book)}


def meta_row(m: MarketMeta, now: float) -> dict[str, Any]:
    return {"ticker": m.ticker, "event_ticker": m.event_ticker, "series_ticker": m.series_ticker, "title": m.title,
            "category": m.category, "sport": m.sport, "close_ts": m.effective_end_ts(), "fee_type": m.fee_type,
            "fee_multiplier": m.fee_multiplier, "is_multivariate": m.is_multivariate, "updated_ts": now}
