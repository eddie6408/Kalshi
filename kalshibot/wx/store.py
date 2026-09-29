"""The weather research store: data/weather_research.db.

Its own SQLite file, so research writes never hold the trading journal's lock.
Append-first: rules versions, forecasts, observations and model runs are
inserted, never overwritten - a revision is a new row with its own
retrieved_at, which is what makes point-in-time replay possible.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;

-- WX-1 ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wx_series (
    series TEXT PRIMARY KEY, title TEXT, frequency TEXT, contract_url TEXT,
    settlement_sources TEXT, first_seen TEXT, last_seen TEXT
);
CREATE TABLE IF NOT EXISTS wx_contracts (
    ticker TEXT PRIMARY KEY, event_ticker TEXT, series TEXT, title TEXT,
    cls TEXT, variable TEXT, aggregation TEXT, operator TEXT,
    threshold REAL, threshold_hi REAL, run_k INTEGER, unit TEXT,
    window_start TEXT, window_end TEXT, at_time TEXT, station TEXT, cli TEXT,
    location TEXT, source TEXT, source_text TEXT, rounding TEXT, min_hours INTEGER,
    revision_policy TEXT, strike_type TEXT, floor_strike REAL, cap_strike REAL,
    close_time TEXT, parse_ok INTEGER, notes TEXT,
    status TEXT, status_reason TEXT, rules_version INTEGER, parser_version TEXT,
    market_status TEXT, result TEXT, yes_bid REAL, yes_ask REAL, volume REAL,
    first_seen TEXT, last_seen TEXT, classified_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_wxc_cls ON wx_contracts (cls, status);
CREATE INDEX IF NOT EXISTS idx_wxc_series ON wx_contracts (series);
-- Never overwritten: a changed hash is a new version.
CREATE TABLE IF NOT EXISTS wx_rules (
    ticker TEXT NOT NULL, version INTEGER NOT NULL, sha256 TEXT NOT NULL,
    rules_primary TEXT, rules_secondary TEXT, retrieved_at TEXT, parser_version TEXT,
    PRIMARY KEY (ticker, version)
);
CREATE TABLE IF NOT EXISTS wx_status_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, ticker TEXT, cls TEXT,
    old TEXT, new TEXT, reason TEXT
);
-- Class-level lifecycle, set by WX-7 evaluation; a market inherits it.
CREATE TABLE IF NOT EXISTS wx_class_status (
    cls TEXT PRIMARY KEY, status TEXT, reason TEXT, at TEXT
);
CREATE TABLE IF NOT EXISTS wx_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, kind TEXT, subject TEXT, detail TEXT
);

-- WX-2 ---------------------------------------------------------------------
-- One row per (model, station, day, variable) per SNAPSHOT: revisions kept.
CREATE TABLE IF NOT EXISTS wx_forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT, model TEXT, station TEXT, valid_date TEXT, variable TEXT,
    value REAL, issued_at TEXT, lead_days INTEGER, retrieved_at TEXT, approx_issue INTEGER DEFAULT 0,
    UNIQUE (source, model, station, valid_date, variable, issued_at)
);
CREATE INDEX IF NOT EXISTS idx_wxf_lookup ON wx_forecasts (station, variable, valid_date);
-- Preliminary AND final kept; a revision is a new row.
CREATE TABLE IF NOT EXISTS wx_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT, station TEXT, obs_date TEXT, variable TEXT, value REAL,
    status TEXT, quality TEXT, hours INTEGER, retrieved_at TEXT,
    UNIQUE (source, station, obs_date, variable, status, value)
);
CREATE INDEX IF NOT EXISTS idx_wxo_lookup ON wx_observations (station, variable, obs_date);
CREATE TABLE IF NOT EXISTS wx_hourly_obs (
    source TEXT, station TEXT, ts_utc TEXT, local_date TEXT, local_hour INTEGER,
    temp_f REAL, status TEXT, retrieved_at TEXT,
    PRIMARY KEY (source, station, ts_utc)
);
CREATE TABLE IF NOT EXISTS wx_climatology (
    station TEXT, obs_date TEXT, maxt REAL, mint REAL, avgt REAL,
    PRIMARY KEY (station, obs_date)
);
CREATE TABLE IF NOT EXISTS wx_quota (
    provider TEXT, day TEXT, calls INTEGER DEFAULT 0, refused INTEGER DEFAULT 0,
    PRIMARY KEY (provider, day)
);

-- WX-3 ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wx_calibration (
    version TEXT, model TEXT, station TEXT, variable TEXT, lead_days INTEGER,
    season TEXT, split TEXT, n INTEGER, bias REAL, mae REAL, rmse REAL, sd REAL,
    fitted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_wxcal ON wx_calibration (version, variable, lead_days);
CREATE TABLE IF NOT EXISTS wx_daily_shadow (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, ticker TEXT, series TEXT,
    target_date TEXT, kind TEXT, lead_hours REAL, centre_f REAL,
    p_live REAL, sigma_live REAL, p_sharp REAL, sigma_sharp REAL, calib_version TEXT,
    market_mid REAL, settled_yes INTEGER, UNIQUE (ticker, at)
);

-- WX-4 / WX-5 --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wx_model_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, ticker TEXT, cls TEXT,
    method TEXT, model_version TEXT, calib_version TEXT, rules_version INTEGER,
    seed INTEGER, n_paths INTEGER, prob REAL, mc_se REAL, prob_lo REAL, prob_hi REAL,
    stat_hist TEXT, inputs TEXT, market_yes_bid REAL, market_yes_ask REAL,
    settled_yes INTEGER
);
CREATE INDEX IF NOT EXISTS idx_wxmr ON wx_model_runs (ticker, at);

-- WX-6 ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wx_paper (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, ticker TEXT, cls TEXT,
    side TEXT, prob REAL, price REAL, fee REAL, edge_net REAL, required_edge REAL,
    ci_width REAL, decision TEXT, reason TEXT, run_id INTEGER,
    settled_yes INTEGER, pnl REAL, settled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_wxp ON wx_paper (ticker, decision);

-- WX-7 ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wx_eligibility (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, cls TEXT, eligible INTEGER,
    status TEXT, criteria TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WxStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    # ------------------------------------------------------------------
    def execute(self, sql: str, params=()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def executemany(self, sql: str, rows) -> int:
        rows = list(rows)
        if not rows:
            return 0
        with self.lock:
            before = self.conn.total_changes
            self.conn.executemany(sql, rows)
            self.conn.commit()
            return self.conn.total_changes - before

    def query(self, sql: str, params=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params=()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def alert(self, kind: str, subject: str, detail) -> None:
        self.execute("INSERT INTO wx_alerts (at, kind, subject, detail) VALUES (?,?,?,?)",
                     (utcnow(), kind, subject,
                      detail if isinstance(detail, str) else json.dumps(detail, default=str)))

    def close(self) -> None:
        with self.lock:
            self.conn.close()
