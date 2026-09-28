"""SQLite storage. Two files, both owned by this bot alone:

* data/lvt.db   - state: runs, signals, orders, fills, positions, alerts,
                  health, readiness evidence, backtest summaries.
* data/tape.db  - recorded market data (quotes, depth, sports context,
                  market metadata). Replay reads only this file.

Every trade-bearing row carries `env` (PAPER / SHADOW / LIVE / BACKTEST) so
simulated and real results can never be mixed in a report (section 51).

One connection per thread. The engine thread owns the writer connection;
the dashboard opens its own read-only connection. All access to a shared
connection goes through a lock anyway.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, env TEXT, kind TEXT, started_ts REAL, ended_ts REAL,
    git_commit TEXT, config_hash TEXT, strategy_versions TEXT, host TEXT, notes TEXT
);

CREATE TABLE IF NOT EXISTS signals (
    id TEXT PRIMARY KEY, run_id TEXT, env TEXT, ts REAL, ticker TEXT, event_ticker TEXT,
    category TEXT, sport TEXT, strategy TEXT, strategy_version TEXT, side TEXT,
    action TEXT, stage TEXT, decision TEXT, reject_reason TEXT, score REAL,
    bid REAL, ask REAL, mid REAL, spread REAL, target REAL, stop REAL,
    expected_net_cents REAL, reward_risk REAL, state TEXT, regime TEXT,
    components TEXT, explanation TEXT, text TEXT, anchor TEXT, data_ts REAL,
    position_id TEXT, hour_utc INTEGER
);
CREATE INDEX IF NOT EXISTS ix_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS ix_signals_ticker ON signals(ticker, ts);

CREATE TABLE IF NOT EXISTS signal_outcomes (
    signal_id TEXT, horizon_s INTEGER, mid_change REAL, mfe REAL, mae REAL,
    first_hit TEXT, observations INTEGER, labeled_ts REAL,
    PRIMARY KEY (signal_id, horizon_s)
);

CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY, run_id TEXT, env TEXT, venue TEXT,
    ticker TEXT, event_ticker TEXT, strategy TEXT, action TEXT, side TEXT, count INTEGER,
    limit_price REAL, purpose TEXT, position_id TEXT, signal_id TEXT, exit_reason TEXT,
    status TEXT, filled_count INTEGER DEFAULT 0, avg_fill_price REAL, fees_usd REAL DEFAULT 0,
    ref_price REAL, slippage_cents REAL, event_ts REAL, data_ts REAL, signal_ts REAL,
    created_ts REAL, submitted_ts REAL, arrival_ts REAL, ack_ts REAL, final_ts REAL,
    exchange_order_id TEXT, reject_reason TEXT, payload TEXT
);
CREATE INDEX IF NOT EXISTS ix_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS ix_orders_ticker ON orders(ticker, created_ts);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT, client_order_id TEXT, run_id TEXT, env TEXT,
    ts REAL, ticker TEXT, side TEXT, action TEXT, price REAL, count INTEGER,
    fee_usd REAL, exchange_fill_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_fills_order ON fills(client_order_id);

CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY, run_id TEXT, env TEXT, ticker TEXT, event_ticker TEXT,
    category TEXT, sport TEXT, strategy TEXT, strategy_version TEXT, side TEXT,
    qty INTEGER, entry_qty INTEGER, exit_qty INTEGER, avg_entry REAL, avg_exit REAL,
    cost_usd REAL, entry_fees_usd REAL, exit_fees_usd REAL,
    realized_pnl_usd REAL, gross_pnl_usd REAL, gross_mid_pnl_usd REAL,
    spread_cost_usd REAL, slippage_cost_usd REAL,
    target REAL, stop REAL, peak_bid REAL, trail_stop REAL,
    entry_mid REAL, exit_mid REAL, entry_ref_ask REAL, exit_ref_bid REAL,
    last_bid REAL, unrealized_pnl_usd REAL, mfe_cents REAL, mae_cents REAL,
    opened_ts REAL, closed_ts REAL, status TEXT, entry_reason TEXT, exit_reason TEXT,
    signal_id TEXT, signal_score REAL, regime TEXT, state TEXT, hour_utc INTEGER,
    updated_ts REAL
);
CREATE INDEX IF NOT EXISTS ix_positions_status ON positions(env, status);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, run_id TEXT, env TEXT, ticker TEXT,
    position_id TEXT, decision TEXT, reason TEXT, details TEXT
);
CREATE INDEX IF NOT EXISTS ix_decisions_pos ON decisions(position_id);

CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, run_id TEXT, env TEXT, ticker TEXT,
    signal_id TEXT, check_name TEXT, passed INTEGER, detail TEXT
);

CREATE TABLE IF NOT EXISTS eligibility_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, run_id TEXT, ticker TEXT,
    eligible INTEGER, reasons TEXT, metrics TEXT
);
CREATE INDEX IF NOT EXISTS ix_elig_ticker ON eligibility_log(ticker, ts);

CREATE TABLE IF NOT EXISTS market_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, run_id TEXT, ticker TEXT,
    state TEXT, labels TEXT, explanation TEXT
);
CREATE INDEX IF NOT EXISTS ix_states_ticker ON market_states(ticker, ts);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, run_id TEXT, level TEXT, code TEXT,
    message TEXT, details TEXT
);

CREATE TABLE IF NOT EXISTS health (
    ts REAL, run_id TEXT, cpu_pct REAL, rss_mb REAL, disk_free_gb REAL,
    state_db_mb REAL, tape_db_mb REAL, requests INTEGER, request_errors INTEGER,
    http_429 INTEGER, req_per_s REAL, websockets INTEGER, max_data_age_s REAL,
    open_positions INTEGER, open_orders INTEGER, fail_closed TEXT, loop_ms REAL
);

CREATE TABLE IF NOT EXISTS status (k TEXT PRIMARY KEY, ts REAL, v TEXT);
CREATE TABLE IF NOT EXISTS scanner_state (ticker TEXT PRIMARY KEY, ts REAL, v TEXT);

CREATE TABLE IF NOT EXISTS readiness_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT, item TEXT, ts REAL, git_commit TEXT,
    config_hash TEXT, passed INTEGER, details TEXT
);

CREATE TABLE IF NOT EXISTS live_authorizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, authorized_by TEXT, git_commit TEXT,
    config_hash TEXT, readiness TEXT, revoked_ts REAL
);

CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id TEXT PRIMARY KEY, kind TEXT, segment TEXT, ts REAL, data_start REAL, data_end REAL,
    params TEXT, metrics TEXT, git_commit TEXT, config_hash TEXT, strategy_versions TEXT,
    db_path TEXT, notes TEXT
);

-- edge engine (EDGE_ENGINE_V1)
CREATE TABLE IF NOT EXISTS reference_values (
    ts REAL, env TEXT, ticker TEXT, kind TEXT, reference REAL, ref_lo REAL, ref_hi REAL, confidence REAL,
    market_mid REAL, dislocation REAL, strength_d REAL, samples INTEGER, state TEXT, mapping TEXT, context_age_s REAL
);
CREATE INDEX IF NOT EXISTS ix_refvals ON reference_values(ticker, ts);
CREATE TABLE IF NOT EXISTS event_responses (
    id TEXT PRIMARY KEY, ts REAL, match_event TEXT, ticker TEXT, kinds TEXT, tags TEXT, severity TEXT,
    expected_impact REAL, ref_before REAL, ref_after REAL, mid_at_event REAL, flags TEXT,
    state_before TEXT, state_after TEXT, env TEXT,
    actual_10s REAL, actual_30s REAL, actual_60s REAL, actual_180s REAL, response TEXT, labeled_ts REAL
);
CREATE INDEX IF NOT EXISTS ix_evresp ON event_responses(ticker, ts);
CREATE TABLE IF NOT EXISTS trade_diagnostics (
    position_id TEXT PRIMARY KEY, env TEXT, strategy TEXT, outcome TEXT, category TEXT, detail TEXT, ts REAL
);
CREATE TABLE IF NOT EXISTS counterfactuals (
    signal_id TEXT, horizon_s INTEGER, label TEXT, hyp_pnl_cents REAL, time_to_target_s REAL,
    time_to_stop_s REAL, max_favorable REAL, max_adverse REAL, ts REAL,
    PRIMARY KEY (signal_id, horizon_s)
);
CREATE TABLE IF NOT EXISTS strategy_experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, name TEXT, baseline_env TEXT, challenger_env TEXT,
    window_start REAL, window_end REAL, result TEXT
);

CREATE VIEW IF NOT EXISTS v_missed_trades AS
    SELECT o.*, s.strategy AS signal_strategy, s.score AS signal_score
    FROM orders o LEFT JOIN signals s ON s.id = o.signal_id
    WHERE o.purpose = 'ENTRY' AND o.status IN ('NO_FILL', 'REJECTED');

CREATE VIEW IF NOT EXISTS v_rejected_signals AS
    SELECT * FROM signals WHERE decision = 'REJECTED';

CREATE VIEW IF NOT EXISTS v_rejected_opportunities AS
    SELECT * FROM signals WHERE decision IN ('REJECTED', 'WATCH', 'NO_EDGE');
"""

TAPE_SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    ticker TEXT PRIMARY KEY, event_ticker TEXT, series_ticker TEXT, category TEXT,
    sport TEXT, title TEXT, sub_title TEXT, close_ts REAL, occurrence_ts REAL,
    fee_type TEXT, fee_multiplier REAL, can_close_early INTEGER,
    volume_24h REAL, status TEXT, result TEXT, first_seen REAL, last_seen REAL
);
CREATE TABLE IF NOT EXISTS quotes (
    ts REAL, ticker TEXT, yes_bid REAL, yes_ask REAL, bid_size REAL, ask_size REAL,
    last REAL, volume REAL, oi REAL, status TEXT
);
CREATE INDEX IF NOT EXISTS ix_quotes_ts ON quotes(ts);
CREATE INDEX IF NOT EXISTS ix_quotes_ticker ON quotes(ticker, ts);
-- slow-tier (probe) quotes: research only, never replayed as tradeable data
CREATE TABLE IF NOT EXISTS probe_quotes (
    ts REAL, ticker TEXT, yes_bid REAL, yes_ask REAL, bid_size REAL, ask_size REAL,
    last REAL, volume REAL, oi REAL, status TEXT
);
CREATE INDEX IF NOT EXISTS ix_probe_ticker ON probe_quotes(ticker, ts);
CREATE TABLE IF NOT EXISTS trades (
    ts REAL, ticker TEXT, yes_price REAL, count REAL, taker_side TEXT, trade_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_trades_ts ON trades(ts);
CREATE INDEX IF NOT EXISTS ix_trades_ticker ON trades(ticker, ts);
CREATE TABLE IF NOT EXISTS sport_events (
    ts_received REAL, ts_provider REAL, event_ticker TEXT, sport TEXT, kind TEXT, actor TEXT,
    detail TEXT, flags TEXT
);
CREATE INDEX IF NOT EXISTS ix_sport_events ON sport_events(event_ticker, ts_received);
CREATE TABLE IF NOT EXISTS event_meta (
    event_ticker TEXT PRIMARY KEY, milestone_id TEXT, type TEXT, title TEXT, details TEXT,
    grp TEXT, start_date TEXT, first_seen REAL
);
CREATE TABLE IF NOT EXISTS depth (
    ts REAL, ticker TEXT, yes TEXT, no TEXT
);
CREATE INDEX IF NOT EXISTS ix_depth_ts ON depth(ts);
CREATE INDEX IF NOT EXISTS ix_depth_ticker ON depth(ticker, ts);
CREATE TABLE IF NOT EXISTS context (
    ts REAL, event_ticker TEXT, provider TEXT, sport TEXT, status TEXT,
    event_ts REAL, summary TEXT
);
CREATE INDEX IF NOT EXISTS ix_context_event ON context(event_ticker, ts);
"""


class Database:
    def __init__(self, path: str | Path, schema: str | None = None, readonly: bool = False):
        self.path = str(path)
        self.readonly = readonly
        self._lock = threading.RLock()
        if readonly:
            uri = f"file:{self.path}?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=10)
        else:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.row_factory = sqlite3.Row
        if schema and not readonly:
            with self._lock:
                self.conn.executescript(schema)
                self.conn.commit()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            self.conn.executemany(sql, rows)
            self.conn.commit()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
        with self._lock:
            row = self.conn.execute(sql, tuple(params)).fetchone()
        return default if row is None or row[0] is None else row[0]

    def insert(self, table: str, row: dict, replace: bool = False) -> None:
        cols = list(row.keys())
        verb = "INSERT OR REPLACE" if replace else "INSERT"
        sql = f"{verb} INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
        self.execute(sql, [_enc(row[c]) for c in cols])

    def update(self, table: str, key: str, key_value: Any, fields: dict) -> None:
        sets = ",".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE {table} SET {sets} WHERE {key}=?",
                     [_enc(v) for v in fields.values()] + [key_value])

    def close(self) -> None:
        with self._lock:
            self.conn.close()


def _enc(v: Any) -> Any:
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(v, default=str)
    if isinstance(v, bool):
        return int(v)
    return v


def open_state_db(path: str | Path) -> Database:
    return Database(path, STATE_SCHEMA)


def open_tape_db(path: str | Path) -> Database:
    return Database(path, TAPE_SCHEMA)
