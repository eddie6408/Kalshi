"""Market-data tape: everything the engine saw, so it can be replayed.

Quotes and depth are written only when they change, which keeps the tape
small without losing information (an unchanged quote carries none). The
replay reader reconstructs the same Snapshot objects in timestamp order.
"""
from __future__ import annotations

import json
import time
from typing import Iterator

from lvt.data.provider import EventContext
from lvt.db import Database
from lvt.models import Depth, MarketInfo, Quote, Snapshot


class TapeRecorder:
    def __init__(self, db: Database):
        self.db = db
        self.last_quote: dict[str, tuple] = {}
        self.last_depth: dict[str, tuple] = {}
        self.last_ctx: dict[str, str] = {}
        self.last_probe: dict[str, tuple] = {}
        self.rows_written = 0
        self.enabled = True

    def record_quotes(self, quotes: dict[str, Quote], keepalive_s: float = 6.0) -> None:
        """Write changed quotes, and unchanged ones every `keepalive_s` so
        replay knows the market was still being watched."""
        if not self.enabled:
            return
        rows = []
        for t, q in quotes.items():
            k = q.key()
            prev = self.last_quote.get(t)
            if prev is not None and prev[0] == k and q.ts - prev[1] < keepalive_s:
                continue
            self.last_quote[t] = (k, q.ts)
            rows.append((q.ts, t, q.yes_bid, q.yes_ask, q.yes_bid_size, q.yes_ask_size, q.last, q.volume,
                         q.open_interest, q.status))
        if rows:
            self.db.executemany("INSERT INTO quotes VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
            self.rows_written += len(rows)

    def record_probe(self, quotes: dict[str, Quote]) -> None:
        if not self.enabled:
            return
        rows = []
        for t, q in quotes.items():
            k = q.key()
            if self.last_probe.get(t) == k:
                continue
            self.last_probe[t] = k
            rows.append((q.ts, t, q.yes_bid, q.yes_ask, q.yes_bid_size, q.yes_ask_size, q.last, q.volume,
                         q.open_interest, q.status))
        if rows:
            self.db.executemany("INSERT INTO probe_quotes VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
            self.rows_written += len(rows)

    def record_depth(self, d: Depth) -> None:
        if not self.enabled:
            return
        k = (tuple(d.yes), tuple(d.no))
        if self.last_depth.get(d.ticker) == k:
            return
        self.last_depth[d.ticker] = k
        self.db.execute("INSERT INTO depth VALUES (?,?,?,?)", (d.ts, d.ticker, json.dumps(d.yes), json.dumps(d.no)))
        self.rows_written += 1

    def record_context(self, c: EventContext) -> None:
        blob = json.dumps(c.summary(), sort_keys=True, default=str)
        if self.last_ctx.get(c.event_ticker) == blob:
            return
        self.last_ctx[c.event_ticker] = blob
        self.db.execute("INSERT INTO context VALUES (?,?,?,?,?,?,?)",
                        (c.fetched_ts, c.event_ticker, c.provider, c.sport, c.status, c.event_ts, blob))

    def record_markets(self, markets: list[MarketInfo], now: float) -> None:
        rows = [(m.ticker, m.event_ticker, m.series_ticker, m.category, m.sport, m.title, m.sub_title, m.close_ts,
                 m.occurrence_ts, m.fee_type, m.fee_multiplier, int(m.can_close_early), m.volume_24h, m.status,
                 m.result, now, now) for m in markets]
        self.db.executemany(
            "INSERT INTO markets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET "
            "close_ts=excluded.close_ts, volume_24h=excluded.volume_24h, status=excluded.status, "
            "result=excluded.result, category=excluded.category, sport=excluded.sport, "
            "fee_type=excluded.fee_type, fee_multiplier=excluded.fee_multiplier, last_seen=excluded.last_seen", rows)

    def prune(self, retention_days: float) -> int:
        cutoff = time.time() - retention_days * 86400
        n = 0
        for table in ("quotes", "probe_quotes", "depth", "context"):
            n += self.db.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,)).rowcount
        return n


def load_markets(tape: Database) -> dict[str, MarketInfo]:
    out = {}
    for r in tape.query("SELECT * FROM markets"):
        out[r["ticker"]] = MarketInfo(
            ticker=r["ticker"], event_ticker=r["event_ticker"] or "", series_ticker=r["series_ticker"] or "",
            category=r["category"] or "", sport=r["sport"] or "", title=r["title"] or "",
            sub_title=r["sub_title"] or "", close_ts=r["close_ts"] or 0.0, occurrence_ts=r["occurrence_ts"] or 0.0,
            status="active", volume_24h=r["volume_24h"] or 0.0, fee_type=r["fee_type"] or "quadratic",
            fee_multiplier=r["fee_multiplier"] or 1.0)
    return out


def iter_snapshots(tape: Database, start: float, end: float, tickers: set[str] | None = None,
                   step_s: float = 2.0, keepalive_s: float = 6.0) -> Iterator[tuple[float, dict[str, Snapshot]]]:
    """Yield (t, {ticker: Snapshot}) every `step_s` of recorded time.

    The recorder writes a quote when it changes and at least every
    `keepalive_s` while a market is being polled, so a market whose last
    row is within 1.5 x keepalive was still being watched: its last quote is
    carried forward, observed at t. Older markets drop out and go stale
    exactly as they would have live. Only rows with ts <= t are ever used.
    """
    q_rows = tape.conn.execute("SELECT ts, ticker, yes_bid, yes_ask, bid_size, ask_size, last, volume, oi, status "
                               "FROM quotes WHERE ts>=? AND ts<? ORDER BY ts", (start, end))
    d_rows = tape.conn.execute("SELECT ts, ticker, yes, no FROM depth WHERE ts>=? AND ts<? ORDER BY ts",
                               (start - 30, end))
    last_q: dict[str, tuple] = {}
    last_d: dict[str, Depth] = {}
    pending_d = next(d_rows, None)
    row = next(q_rows, None)
    if row is None:
        return
    t = row[0]
    horizon = 1.5 * keepalive_s
    while True:
        t += step_s
        while row is not None and row[0] <= t:
            if tickers is None or row[1] in tickers:
                last_q[row[1]] = tuple(row)
            row = next(q_rows, None)
        while pending_d is not None and pending_d[0] <= t:
            dts, dt, y, n = pending_d
            last_d[dt] = Depth(dt, dts, [tuple(x) for x in json.loads(y)], [tuple(x) for x in json.loads(n)])
            pending_d = next(d_rows, None)
        out: dict[str, Snapshot] = {}
        for tk, r in list(last_q.items()):
            if t - r[0] > horizon:
                del last_q[tk]
                continue
            q = Quote(tk, t, r[2], r[3], r[4] or 0.0, r[5] or 0.0, r[6], r[7] or 0.0, r[8] or 0.0, r[9] or "active")
            d = last_d.get(tk)
            out[tk] = Snapshot(tk, t, q, d if d is not None and t - d.ts <= 30 else None)
        if out:
            yield t, out
        if row is None and not last_q:
            return
        if not last_q and row is not None and row[0] > t + step_s:
            t = row[0] - step_s          # skip idle stretches
