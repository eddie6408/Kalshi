"""Dedicated SQLite database for this bot (never shared with any other application).

Every trading table carries an `env` column (PAPER / SHADOW / LIVE / BACKTEST) so
simulated and real results can never be mixed in a report. WAL mode lets the
dashboard read while the trader writes.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, started_ts REAL, ended_ts REAL, mode TEXT, config_hash TEXT,
  strategy_versions TEXT, app_version TEXT, host TEXT, notes TEXT);

CREATE TABLE IF NOT EXISTS markets (
  ticker TEXT PRIMARY KEY, event_ticker TEXT, series_ticker TEXT, title TEXT, category TEXT,
  sport TEXT, close_ts REAL, fee_type TEXT, fee_multiplier REAL, is_multivariate INTEGER,
  updated_ts REAL);

CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, ticker TEXT NOT NULL,
  yes_bid REAL, yes_ask REAL, last_price REAL, volume REAL, volume_24h REAL, open_interest REAL,
  yes_bid_size REAL, yes_ask_size REAL, status TEXT, source TEXT, book TEXT);
CREATE INDEX IF NOT EXISTS ix_snap_ticker_ts ON snapshots(ticker, ts);
CREATE INDEX IF NOT EXISTS ix_snap_ts ON snapshots(ts);

CREATE TABLE IF NOT EXISTS trade_prints (
  trade_id TEXT PRIMARY KEY, ticker TEXT, ts REAL, yes_price REAL, count REAL, taker_side TEXT);
CREATE INDEX IF NOT EXISTS ix_tp_ticker_ts ON trade_prints(ticker, ts);

CREATE TABLE IF NOT EXISTS sports_context (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, event_ticker TEXT, sport TEXT, status TEXT,
  data TEXT, source TEXT);
CREATE INDEX IF NOT EXISTS ix_sc_event_ts ON sports_context(event_ticker, ts);

CREATE TABLE IF NOT EXISTS market_states (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, run_id TEXT, ticker TEXT, sport TEXT,
  states TEXT, explanations TEXT, features TEXT, eligible INTEGER, ineligible_reasons TEXT,
  movement_score REAL, dip_score REAL, momentum_score REAL, regime TEXT);
CREATE INDEX IF NOT EXISTS ix_ms_ticker_ts ON market_states(ticker, ts);

CREATE TABLE IF NOT EXISTS signals (
  id TEXT PRIMARY KEY, ts REAL, run_id TEXT, env TEXT, ticker TEXT, event_ticker TEXT, sport TEXT,
  category TEXT, strategy TEXT, strategy_version TEXT, direction TEXT, side TEXT, action TEXT,
  entry_ref REAL, target REAL, stop REAL, score REAL, expected_gross REAL, expected_costs REAL,
  expected_net REAL, accepted INTEGER, reject_reasons TEXT, reasons TEXT, details TEXT,
  components TEXT, explanation TEXT, regime TEXT);
CREATE INDEX IF NOT EXISTS ix_sig_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS ix_sig_strat ON signals(strategy_version, env);

CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY, env TEXT, run_id TEXT, ticker TEXT, side TEXT, action TEXT, count INTEGER,
  limit_price REAL, purpose TEXT, style TEXT, reason TEXT, signal_id TEXT, position_id TEXT,
  client_order_id TEXT UNIQUE, exchange_order_id TEXT, status TEXT, filled INTEGER,
  avg_fill_price REAL, fees REAL, reject_reason TEXT, ref_price REAL, expires_ts REAL,
  event_ts REAL, data_ts REAL, signal_ts REAL, created_ts REAL, submitted_ts REAL, ack_ts REAL,
  first_fill_ts REAL, final_ts REAL, payload TEXT, strategy_version TEXT, updated_ts REAL);
CREATE INDEX IF NOT EXISTS ix_ord_status ON orders(env, status);

CREATE TABLE IF NOT EXISTS fills (
  id TEXT PRIMARY KEY, order_id TEXT, env TEXT, ticker TEXT, side TEXT, action TEXT, count INTEGER,
  price REAL, fee REAL, is_taker INTEGER, ts REAL, ref_price REAL, slippage REAL,
  exchange_fill_id TEXT UNIQUE);
CREATE INDEX IF NOT EXISTS ix_fill_order ON fills(order_id);

CREATE TABLE IF NOT EXISTS positions (
  id TEXT PRIMARY KEY, env TEXT, ticker TEXT, event_ticker TEXT, side TEXT, direction TEXT,
  strategy TEXT, strategy_version TEXT, signal_id TEXT, qty INTEGER, avg_entry REAL, bought INTEGER,
  sold INTEGER, entry_cost REAL, exit_proceeds REAL, fees REAL, slippage_cost REAL, target REAL,
  stop REAL, status TEXT, opened_ts REAL, closed_ts REAL, entry_reason TEXT, exit_reason TEXT,
  signal_score REAL, sport TEXT, category TEXT, regime TEXT, peak_mark REAL, trough_mark REAL,
  last_mark REAL, max_hold_seconds REAL, entry_features TEXT, updated_ts REAL);
CREATE INDEX IF NOT EXISTS ix_pos_status ON positions(env, status);

CREATE TABLE IF NOT EXISTS trades (
  id TEXT PRIMARY KEY, env TEXT, run_id TEXT, ticker TEXT, event_ticker TEXT, sport TEXT,
  category TEXT, strategy TEXT, strategy_version TEXT, side TEXT, qty INTEGER, avg_entry REAL,
  avg_exit REAL, gross_pnl REAL, fees REAL, slippage REAL, net_pnl REAL, opened_ts REAL,
  closed_ts REAL, hold_seconds REAL, entry_reason TEXT, exit_reason TEXT, signal_score REAL,
  regime TEXT, mfe REAL, mae REAL, hour_of_day INTEGER, signal_id TEXT, latency_ms REAL);
CREATE INDEX IF NOT EXISTS ix_tr_env_ts ON trades(env, closed_ts);

CREATE TABLE IF NOT EXISTS missed_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, env TEXT, signal_id TEXT, order_id TEXT,
  ticker TEXT, strategy TEXT, strategy_version TEXT, reason TEXT, ref_price REAL,
  price_after REAL, details TEXT);

CREATE TABLE IF NOT EXISTS risk_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, env TEXT, kind TEXT, ticker TEXT, detail TEXT);

CREATE TABLE IF NOT EXISTS system_health (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, cpu_pct REAL, rss_mb REAL, sys_mem_pct REAL,
  disk_free_mb REAL, db_mb REAL, req_per_sec REAL, rate_limited INTEGER, ws_connected INTEGER,
  data_connected INTEGER, loop_lag_ms REAL, open_positions INTEGER, details TEXT);

CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, level TEXT, kind TEXT, message TEXT, details TEXT,
  delivered INTEGER);

CREATE TABLE IF NOT EXISTS validation_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, passed INTEGER, summary TEXT);

CREATE TABLE IF NOT EXISTS equity (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, env TEXT, equity REAL, realized REAL,
  unrealized REAL, exposure REAL, open_positions INTEGER);
CREATE INDEX IF NOT EXISTS ix_eq_env_ts ON equity(env, ts);
"""

JSON_COLS = {"strategy_versions", "states", "explanations", "features", "ineligible_reasons",
             "reject_reasons", "reasons", "details", "components", "payload", "entry_features",
             "data", "summary", "detail", "book"}


def _encode(v: Any) -> Any:
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(v, default=str)
    if isinstance(v, bool):
        return int(v)
    if hasattr(v, "value") and not isinstance(v, (int, float, str)):
        return v.value  # Enum
    return v


class Database:
    def __init__(self, path: str | Path, read_only: bool = False):
        self.path = Path(path)
        memory = str(path) == ":memory:"
        if not read_only and not memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        uri = "file::memory:" if memory else (f"file:{self.path}?mode=ro" if read_only else f"file:{self.path}")
        self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        if not read_only:
            with self.lock:
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA synchronous=NORMAL")
                self.conn.executescript(SCHEMA)
                self.conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                                  (str(SCHEMA_VERSION),))
                self.conn.commit()

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # ---- generic helpers -------------------------------------------------
    def insert(self, table: str, row: dict[str, Any], replace: bool = False) -> int:
        cols = list(row.keys())
        sql = (f"INSERT {'OR REPLACE ' if replace else ''}INTO {table} ({','.join(cols)}) "
               f"VALUES ({','.join('?' for _ in cols)})")
        with self.lock:
            cur = self.conn.execute(sql, [_encode(row[c]) for c in cols])
            self.conn.commit()
            return cur.lastrowid

    def insert_many(self, table: str, rows: Iterable[dict[str, Any]], ignore: bool = False) -> int:
        rows = list(rows)
        if not rows:
            return 0
        cols = list(rows[0].keys())
        sql = (f"INSERT {'OR IGNORE ' if ignore else ''}INTO {table} ({','.join(cols)}) "
               f"VALUES ({','.join('?' for _ in cols)})")
        with self.lock:
            self.conn.executemany(sql, [[_encode(r.get(c)) for c in cols] for r in rows])
            self.conn.commit()
        return len(rows)

    def upsert(self, table: str, row: dict[str, Any], key: str = "id") -> None:
        cols = list(row.keys())
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != key)
        sql = (f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)}) "
               f"ON CONFLICT({key}) DO UPDATE SET {updates}")
        with self.lock:
            self.conn.execute(sql, [_encode(row[c]) for c in cols])
            self.conn.commit()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(sql, list(params)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k, v in d.items():
                if k in JSON_COLS and isinstance(v, str) and v[:1] in "[{":
                    try:
                        d[k] = json.loads(v)
                    except ValueError:
                        pass
            out.append(d)
        return out

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        with self.lock:
            row = self.conn.execute(sql, list(params)).fetchone()
        return None if row is None else row[0]

    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self.lock:
            self.conn.execute(sql, list(params))
            self.conn.commit()

    # ---- kv state -------------------------------------------------------
    def set_meta(self, key: str, value: Any) -> None:
        self.insert("meta", {"key": key, "value": json.dumps(value, default=str)}, replace=True)

    def get_meta(self, key: str, default: Any = None) -> Any:
        v = self.scalar("SELECT value FROM meta WHERE key=?", (key,))
        if v is None:
            return default
        try:
            return json.loads(v)
        except ValueError:
            return v

    # ---- domain helpers -------------------------------------------------
    def record_validation(self, kind: str, passed: bool, summary: dict[str, Any]) -> None:
        self.insert("validation_runs", {"ts": time.time(), "kind": kind, "passed": passed, "summary": summary})

    def latest_validation(self, kind: str) -> dict[str, Any] | None:
        rows = self.query("SELECT * FROM validation_runs WHERE kind=? ORDER BY ts DESC LIMIT 1", (kind,))
        return rows[0] if rows else None

    def prune_snapshots(self, older_than_ts: float) -> int:
        with self.lock:
            cur = self.conn.execute("DELETE FROM snapshots WHERE ts < ?", (older_than_ts,))
            self.conn.execute("DELETE FROM market_states WHERE ts < ?", (older_than_ts,))
            self.conn.commit()
            return cur.rowcount

    def size_mb(self) -> float:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                total += p.stat().st_size
        return total / 1e6
