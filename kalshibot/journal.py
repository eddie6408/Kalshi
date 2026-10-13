"""SQLite trade journal.

This is the part that makes the bot get better instead of just busy. Every
decision - taken or rejected - is written down with the probability we
believed at the time. Later you can ask the only question that matters:

    "When I said 70%, did it happen about 70% of the time?"

scripts/report.py answers that from this database.
"""
from __future__ import annotations

import functools
import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    ticker TEXT NOT NULL,
    series TEXT,
    title TEXT,
    side TEXT,
    market_yes_bid INTEGER,
    market_yes_ask INTEGER,
    our_probability REAL,
    market_probability REAL,
    confidence REAL,
    edge_after_fees REAL,
    action TEXT NOT NULL,          -- 'traded' | 'rejected' | 'skipped'
    reason TEXT,
    contracts INTEGER DEFAULT 0,
    limit_price_cents INTEGER,
    est_cost_usd REAL,
    est_fee_usd REAL,
    est_ev_usd REAL,
    estimator TEXT,
    evidence TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    action TEXT NOT NULL,          -- 'buy' | 'sell'
    contracts INTEGER NOT NULL,
    price_cents INTEGER NOT NULL,
    fee_usd REAL,
    order_id TEXT,
    client_order_id TEXT,
    status TEXT,
    our_probability REAL,
    exit_price_cents INTEGER,
    exit_ts TEXT,
    outcome TEXT,                  -- 'yes' | 'no' | NULL while open
    pnl_usd REAL,
    FOREIGN KEY(decision_id) REFERENCES decisions(id)
);

CREATE TABLE IF NOT EXISTS forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    series TEXT,
    station TEXT,
    target_date TEXT,
    predicted_mean_f REAL,
    predicted_sigma_f REAL,
    members INTEGER,
    lead_hours REAL,
    actual_f REAL
);

-- One row per MARKET we priced, whether or not we traded it. This is what
-- the calibration gate reads: a probability, and eventually the outcome it
-- was a probability of.
--
-- Separate from `forecasts` on purpose. `forecasts` scores the DISTRIBUTION
-- (was the mean right, was sigma wide enough) against the observed
-- temperature. This scores the CONTRACT (was 20% right) against how the
-- market actually resolved. When the two disagree, the settlement source is
-- wrong - which is exactly how Chicago was found to be forecasting the wrong
-- airport.
CREATE TABLE IF NOT EXISTS probability_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    strategy TEXT,
    estimator TEXT,
    series TEXT,
    ticker TEXT NOT NULL,
    target_date TEXT,
    strike TEXT,
    our_probability REAL NOT NULL,      -- P(YES), always in YES terms
    confidence REAL,
    market_yes_cents REAL,              -- best YES ASK at the time (what we
                                        -- would have paid, not a probability)
    market_yes_bid_cents REAL,          -- best YES BID at the time; the MID of
                                        -- bid/ask is the market's probability,
                                        -- and is what belongs in any
                                        -- model-vs-market comparison
    lead_hours REAL,
    traded INTEGER DEFAULT 0,
    settled_yes INTEGER,                -- 1 / 0 once the market resolves
    settled_at TEXT,
    brier REAL,                         -- (our_probability - settled_yes)^2
    UNIQUE (ticker, ts)
);

CREATE INDEX IF NOT EXISTS idx_problog_open
    ON probability_log (settled_yes, ticker);

-- The same market, the same instant, on both books.
--
-- Demo liquidity is synthetic, so every execution number this bot has
-- measured describes a fiction. This is the only table that can say what an
-- order would really cost. Written by a measurement worker; nothing in it
-- ever reaches a trading decision.
CREATE TABLE IF NOT EXISTS book_quality (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT,
    action TEXT,
    size INTEGER,
    demo_best_cents REAL, demo_spread_cents REAL, demo_depth_at_best INTEGER,
    demo_vwap_cents REAL, demo_filled INTEGER, demo_complete INTEGER,
    demo_slippage_cents REAL,
    live_best_cents REAL, live_spread_cents REAL, live_depth_at_best INTEGER,
    live_vwap_cents REAL, live_filled INTEGER, live_complete INTEGER,
    live_slippage_cents REAL,
    cost_gap_cents REAL, slippage_gap_cents REAL
);

CREATE INDEX IF NOT EXISTS idx_bookquality_ts ON book_quality (ts);

CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    mode TEXT,
    strategy TEXT,
    event_id TEXT,
    tickers TEXT,
    n_legs INTEGER,
    quantity INTEGER,
    max_executable INTEGER,
    min_profitable INTEGER,
    gross_cost REAL,
    guaranteed_min_payout REAL,
    gross_edge REAL,
    estimated_fees REAL,
    net_edge REAL,
    net_edge_per_contract REAL,
    expected_return_pct REAL,
    fee_drag_pct REAL,
    execution_risk TEXT,
    guaranteed INTEGER,
    decision TEXT,
    reject_reason TEXT,
    detail TEXT,
    rationale TEXT,
    legs_json TEXT,
    evidence TEXT
);

CREATE TABLE IF NOT EXISTS executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    opportunity_id INTEGER,
    mode TEXT,
    strategy TEXT,
    event_id TEXT,
    outcome TEXT,
    sets_completed INTEGER,
    total_cost_usd REAL,
    total_fees_usd REAL,
    realised_edge_usd REAL,
    exposed INTEGER,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS leg_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    execution_id INTEGER,
    ticker TEXT,
    side TEXT,
    action TEXT,
    requested INTEGER,
    filled INTEGER,
    limit_price_cents INTEGER,
    avg_price_cents REAL,
    fee_usd REAL,
    state TEXT,
    order_id TEXT,
    client_order_id TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    mode TEXT,
    markets_seen INTEGER DEFAULT 0,
    no_orderbook INTEGER DEFAULT 0,
    wrong_window INTEGER DEFAULT 0,
    other_untradable INTEGER DEFAULT 0,
    priced INTEGER DEFAULT 0,
    edge_too_small INTEGER DEFAULT 0,
    risk_rejected INTEGER DEFAULT 0,
    traded INTEGER DEFAULT 0,
    seconds REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_ts TEXT, ended_ts TEXT, mode TEXT, note TEXT
);

-- Package 14A. One row per external fetch that feeds a decision: WHO
-- published it, WHEN they produced it, WHEN we retrieved it, WHAT it said.
CREATE TABLE IF NOT EXISTS source_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    retrieved_at TEXT NOT NULL,
    source_id TEXT NOT NULL,
    publisher TEXT,
    tier INTEGER,
    endpoint TEXT,
    data_type TEXT,
    subject TEXT,
    target_date TEXT,
    value REAL,
    value_json TEXT,
    data_time TEXT,
    data_vintage TEXT,
    latency_ms REAL
);

-- Latest health per source, rewritten every heartbeat so the monitor and
-- the dashboard can show it without being inside the bot's process.
CREATE TABLE IF NOT EXISTS source_health (
    source_id TEXT PRIMARY KEY,
    updated_at TEXT,
    state TEXT,
    detail TEXT,
    publisher TEXT,
    tier INTEGER,
    calls INTEGER,
    failures INTEGER,
    consecutive_failures INTEGER,
    error_rate REAL,
    last_success_at TEXT,
    last_failure_at TEXT,
    last_status INTEGER,
    last_error TEXT,
    last_latency_ms REAL,
    last_data_time TEXT
);

-- 14A phase 4. One row per distinct forecast: the cross-check gap, data
-- ages, and every data-quality gate verdict (WOULD_X = shadow, X = enforced).
-- scripts/fit_conflict_threshold.py fits the conflict threshold from this.
CREATE TABLE IF NOT EXISTS data_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    series TEXT,
    target_date TEXT,
    lead_hours REAL,
    ensemble_f REAL,
    cross_check_f REAL,
    diff_f REAL,
    forecast_age_seconds REAL,
    observation_age_seconds REAL,
    observation_suspect TEXT,
    verdicts TEXT,
    enforced INTEGER DEFAULT 0,
    provenance TEXT
);

-- Package 2. One row per EVALUATED opportunity - traded, rejected at any
-- stage, or skipped before it was priced - keyed by the Package 1 uuid.
-- The authoritative record of what the bot decided and what came of it.
CREATE TABLE IF NOT EXISTS opportunity_ledger (
    opportunity_id TEXT PRIMARY KEY,
    dedupe_key TEXT UNIQUE,
    detected_at TEXT NOT NULL,
    opportunity_class TEXT,
    strategy TEXT,
    event_id TEXT,
    markets TEXT,
    direction TEXT,
    quantity INTEGER,
    probability_yes REAL,
    fair_probability REAL,
    market_probability REAL,
    market_price_source TEXT,
    touch_json TEXT,
    capital_required REAL,
    max_loss REAL,
    max_profit REAL,
    gross_edge REAL,
    fees REAL,
    net_ev REAL,
    ev_complete INTEGER,
    settlement_time TEXT,
    liquidity_available INTEGER,
    data_quality TEXT,
    source_quality_tier INTEGER,
    confidence REAL,
    model_version TEXT,
    code_version TEXT,
    config_version TEXT,
    fee_schedule_version TEXT,
    execution_mode TEXT,
    decision TEXT,
    decision_stage TEXT,
    rejection_reason TEXT,
    final_state TEXT,
    outcome_json TEXT,
    resolved_at TEXT,
    realised_pnl REAL,
    hypothetical_pnl REAL,
    hypothetical_basis TEXT,
    record_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_ledger_detected ON opportunity_ledger(detected_at);
CREATE INDEX IF NOT EXISTS idx_ledger_pending ON opportunity_ledger(resolved_at, settlement_time);

-- The lifecycle of each ledger row: DETECTED, REJECTED, ACCEPTED, SUBMITTED,
-- FILLED, PARTIALLY_FILLED, NOT_FILLED, EXPIRED, EXITED, SETTLED.
CREATE TABLE IF NOT EXISTS opportunity_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    state TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_opp ON opportunity_events(opportunity_id);

-- Package C2, phase M0: a SIMULATED resting (maker) order for an entry the
-- forecast strategy priced, checked against the PRODUCTION trade tape. No
-- order is ever sent; this measures whether one would have filled, and what
-- the price did afterwards (adverse selection). Times are unix seconds.
CREATE TABLE IF NOT EXISTS maker_shadow (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    ticker TEXT NOT NULL,
    series TEXT,
    side TEXT NOT NULL,              -- the side we would BUY
    probability_yes REAL,
    fair_cents REAL,                 -- our probability of `side`, in cents
    stage TEXT,                      -- ACCEPTED, or the stage that blocked it
    reason TEXT,
    lead_hours REAL,
    book_age_s REAL,                 -- how old the production book was
    best_bid INTEGER,                -- production, on `side`
    best_ask INTEGER,
    price_cents INTEGER NOT NULL,    -- where the order would rest
    contracts INTEGER NOT NULL,
    queue_ahead INTEGER,             -- displayed size at our price when posted
    maker_fee_cents REAL,
    taker_fee_cents REAL,            -- per contract, had we crossed instead
    expires_at REAL NOT NULL,
    state TEXT NOT NULL,             -- OPEN, FILLED, PARTIAL, EXPIRED,
                                     -- INTERRUPTED (bot restarted; excluded)
    filled INTEGER DEFAULT 0,
    first_fill_at REAL,
    full_fill_at REAL,
    fill_kind TEXT,                  -- through (price traded past us) | queue
    drift_5m REAL,                   -- production mid on `side` minus our
    drift_30m REAL,                  -- price, N minutes after the first fill
    drift_60m REAL,                  -- (positive = moved our way)
    closed_at REAL,
    settled_yes INTEGER,
    maker_markout_cents REAL,        -- per contract at settlement, if filled
    taker_markout_cents REAL,        -- per contract had we crossed at the ask
    code_version TEXT,
    config_version TEXT
);
CREATE INDEX IF NOT EXISTS idx_maker_shadow_state ON maker_shadow(state, ticker);

-- Packages 6/7: the FAKE-MONEY account on production prices (paper_live.py).
-- One row per leg; an arbitrage basket shares group_id. Times: unix seconds.
CREATE TABLE IF NOT EXISTS paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at REAL NOT NULL,
    kind TEXT NOT NULL,              -- FORECAST | ARB
    group_id TEXT NOT NULL,
    strategy TEXT, event_id TEXT,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    contracts INTEGER NOT NULL,
    price_cents REAL NOT NULL,       -- vwap walking the production book
    cost_usd REAL NOT NULL,
    fee_usd REAL NOT NULL,
    expected_edge_usd REAL,          -- the strategy's EV at entry, this leg
    probability_yes REAL, confidence REAL, lead_hours REAL, model TEXT,
    gate_blocked INTEGER DEFAULT 0,  -- 1: the market-skill gate would block it
    note TEXT,
    book_age_s REAL,
    state TEXT NOT NULL,             -- OPEN, SETTLED, VOID
    settled_at REAL, settled_yes INTEGER, payout_usd REAL, pnl_usd REAL,
    code_version TEXT, config_version TEXT
);
CREATE INDEX IF NOT EXISTS idx_paper_state ON paper_positions(state, ticker);

-- Does an arbitrage the demo sweep inspected exist on PRODUCTION books?
CREATE TABLE IF NOT EXISTS prod_arb_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    event_id TEXT NOT NULL,
    markets INTEGER, prod_books INTEGER,
    demo_found INTEGER, demo_best_edge REAL,
    prod_found INTEGER, prod_accepted INTEGER, prod_best_edge REAL,
    prod_near_miss_gap REAL
);

-- An operator acknowledging a tripped consecutive-loss breaker. Losses
-- closed before the latest row no longer count toward the run.
CREATE TABLE IF NOT EXISTS breaker_resets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    losses_at_reset INTEGER,
    note TEXT
);

-- The demo account balance, as the exchange reports it, every heartbeat.
-- The one P/L number nothing in the bot's own bookkeeping can get wrong.
CREATE TABLE IF NOT EXISTS account_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    balance_usd REAL
);

-- What Kalshi says each series settles on, as read from the exchange.
CREATE TABLE IF NOT EXISTS settlement_sources (
    series TEXT NOT NULL,
    name TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    first_seen TEXT,
    last_seen TEXT,
    PRIMARY KEY (series, name, url)
);

CREATE INDEX IF NOT EXISTS idx_decisions_ticker ON decisions(ticker);
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
CREATE INDEX IF NOT EXISTS idx_source_obs_source ON source_observations(source_id, retrieved_at);
"""


class JournalError(RuntimeError):
    """Raised when the journal database cannot be opened or written."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def pnl_summary(conn, since: str | None = None) -> dict:
    """Realised P/L from what actually happened: fills, and settlements
    booked as closing legs. Shared by the bot and the watch screen so the
    two can never disagree.

        realised        all closed quantity, all time
        realised_since  only positions whose last close is after `since`
        closed / wins   matched positions, and how many made money
        open / open_cost  lines still open, and what they cost

    NOT executions.realised_edge_usd. That is the edge a trade was EXPECTED
    to make at entry, and for a forecast trade - which has no guaranteed
    payout - it was recorded as minus the whole purchase price, which made
    every weather purchase look like a total loss (fixed 27 September).
    """
    flows: dict = {}
    for r in conn.execute(
            "SELECT ticker, side, action, filled, avg_price_cents, "
            "limit_price_cents, ts FROM leg_orders WHERE filled > 0 ORDER BY id"):
        f = flows.setdefault((r[0], r[1]), {"b": 0, "s": 0, "sp": 0.0, "g": 0.0, "last": ""})
        n = int(r[3] or 0)
        px = r[4] if r[4] is not None else (r[5] or 0)
        px = float(px) / 100.0
        if r[2] == "buy":
            f["b"] += n
            f["sp"] += n * px
        else:
            f["s"] += n
            f["g"] += n * px
            f["last"] = r[6] or ""
    out = {"realised": 0.0, "realised_since": 0.0, "closed": 0, "wins": 0,
           "open": 0, "open_cost": 0.0}
    for f in flows.values():
        if f["b"] and f["s"]:
            matched = min(f["b"], f["s"])
            pnl = matched * (f["g"] / f["s"] - f["sp"] / f["b"])
            out["realised"] += pnl
            out["closed"] += 1
            out["wins"] += 1 if pnl > 0 else 0
            if since and f["last"] >= since:
                out["realised_since"] += pnl
        if f["b"] > f["s"]:
            out["open"] += 1
            out["open_cost"] += (f["b"] - f["s"]) * (f["sp"] / f["b"])
    return out


def _ids(ids) -> str | None:
    """[3, 7] -> "3,7". The provenance ids behind a row, or None."""
    return ",".join(str(int(i)) for i in ids) if ids else None


class _Result:
    """A cursor's results, fetched in full while the lock was held."""
    __slots__ = ("_rows", "_i", "rowcount", "lastrowid", "description")

    def __init__(self, rows, rowcount, lastrowid, description):
        self._rows, self._i = rows, 0
        self.rowcount, self.lastrowid, self.description = rowcount, lastrowid, description

    def fetchone(self):
        if self._i >= len(self._rows):
            return None
        self._i += 1
        return self._rows[self._i - 1]

    def fetchall(self):
        rest, self._i = self._rows[self._i:], len(self._rows)
        return rest

    def fetchmany(self, size=1):
        chunk = self._rows[self._i:self._i + size]
        self._i += len(chunk)
        return chunk

    def __iter__(self):
        while self._i < len(self._rows):
            self._i += 1
            yield self._rows[self._i - 1]


class _SafeConnection:
    """The journal's one SQLite connection, made safe to share between threads.

    Python's sqlite3 leaves serialising a shared connection to the caller.
    The bot did not: its workers read and wrote through one connection at
    once, and on the two-core server that failed outright - "bad parameter
    or other API misuse", a thread killed, writes lost (found 27 September
    while testing Package 2; a write-only lock was not enough). Every call
    now holds the journal lock, and results are fetched completely before it
    is released, so no cursor is ever live on the connection outside it.
    """

    def __init__(self, conn: sqlite3.Connection, lock):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_lock", lock)

    def _run(self, fn, *a):
        with self._lock:
            cur = fn(*a)
            rows = cur.fetchall() if cur.description else []
            return _Result(rows, cur.rowcount, cur.lastrowid, cur.description)

    def execute(self, sql, params=()):
        return self._run(self._conn.execute, sql, params)

    def executemany(self, sql, seq):
        return self._run(self._conn.executemany, sql, seq)

    def executescript(self, script):
        with self._lock:
            self._conn.executescript(script)

    def commit(self):
        with self._lock:
            self._conn.commit()

    def rollback(self):
        with self._lock:
            self._conn.rollback()

    def close(self):
        with self._lock:
            self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        setattr(self._conn, name, value)


class Journal:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(self.path, check_same_thread=False)
        raw.row_factory = sqlite3.Row
        #: Audit H1. Every write method holds this for its whole
        #: execute...commit, so workers cannot interleave transactions; and
        #: EVERY call through `conn` - reads included, from any module - holds
        #: it too (see _SafeConnection). Re-entrant: a write may call another.
        self.lock = threading.RLock()
        self.conn = _SafeConnection(raw, self.lock)
        try:
            self.conn.executescript(SCHEMA)
            self.conn.commit()
            self._migrate()
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_legs_opp "
                              "ON leg_orders(opportunity_uuid)")
            self.conn.commit()
        except sqlite3.OperationalError as exc:
            raise JournalError(
                f"Could not open the journal database at {self.path}.\n"
                f"  SQLite said: {exc}\n\n"
                "  This almost always means the folder is on a network or "
                "cloud-synced drive\n"
                "  (iCloud Drive, Dropbox, Google Drive, a mounted share). "
                "SQLite needs real file\n"
                "  locking, which those do not provide.\n\n"
                "  Fix: point journal.database_path in config.yaml at a local "
                "folder, e.g.\n"
                "      journal:\n"
                "        database_path: ~/Library/Application Support/kalshibot/journal.db\n"
                "  or move the whole project off the synced drive."
            ) from exc

    # -------------------------------------------------------------
    def log_decision(self, *, mode: str, market, estimate, edge=None, action: str,
                     reason: str = "", sized=None, side: str = "") -> int:
        market_prob = (market.yes_ask / 100.0) if market.yes_ask else None
        cur = self.conn.execute(
            """INSERT INTO decisions
               (ts, mode, ticker, series, title, side, market_yes_bid, market_yes_ask,
                our_probability, market_probability, confidence, edge_after_fees,
                action, reason, contracts, limit_price_cents, est_cost_usd,
                est_fee_usd, est_ev_usd, estimator, evidence)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                _now(), mode, market.ticker, market.series_ticker,
                (market.title or market.subtitle)[:200], side or (edge.side if edge else ""),
                market.yes_bid, market.yes_ask,
                getattr(estimate, "probability_yes", None), market_prob,
                getattr(estimate, "confidence", None),
                getattr(edge, "edge_after_fees", None),
                action, reason[:500],
                getattr(sized, "contracts", 0) or 0,
                getattr(edge, "price_cents", None),
                getattr(sized, "est_cost_usd", None),
                getattr(sized, "est_fee_usd", None),
                getattr(sized, "est_ev_usd", None),
                getattr(estimate, "source", ""),
                json.dumps(getattr(estimate, "evidence", {}) or {}),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def log_trade(self, *, decision_id: int | None, mode: str, ticker: str, side: str,
                  action: str, contracts: int, price_cents: int, fee_usd: float,
                  our_probability: float, order_id: str = "", client_order_id: str = "",
                  status: str = "filled") -> int:
        cur = self.conn.execute(
            """INSERT INTO trades
               (decision_id, ts, mode, ticker, side, action, contracts, price_cents,
                fee_usd, order_id, client_order_id, status, our_probability)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (decision_id, _now(), mode, ticker, side, action, contracts, price_cents,
             fee_usd, order_id, client_order_id, status, our_probability),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def close_open_trades(self, ticker: str, yes_exit_cents: int, no_exit_cents: int,
                          outcome: str | None = None) -> tuple[int, float]:
        """Close every open trade on a ticker and book each one's own P/L.

        A position is often built from several entries at different prices.
        Closing only the most recent one leaves the rest looking permanently
        open, which quietly corrupts exposure, win rate and P/L.
        """
        rows = self.conn.execute(
            "SELECT id, side, contracts, price_cents, fee_usd FROM trades "
            "WHERE ticker=? AND status != 'closed' AND action='buy'", (ticker,)
        ).fetchall()
        total = 0.0
        for r in rows:
            exit_px = yes_exit_cents if r["side"] == "yes" else no_exit_cents
            pnl = r["contracts"] * (exit_px - r["price_cents"]) / 100.0 - (r["fee_usd"] or 0.0)
            total += pnl
            self.conn.execute(
                "UPDATE trades SET exit_price_cents=?, exit_ts=?, outcome=?, "
                "pnl_usd=?, status='closed' WHERE id=?",
                (exit_px, _now(), outcome, pnl, r["id"]),
            )
        self.conn.commit()
        return len(rows), total

    def close_trade(self, trade_id: int, exit_price_cents: int, outcome: str | None,
                    pnl_usd: float) -> None:
        self.conn.execute(
            "UPDATE trades SET exit_price_cents=?, exit_ts=?, outcome=?, pnl_usd=?, status='closed'"
            " WHERE id=?",
            (exit_price_cents, _now(), outcome, pnl_usd, trade_id),
        )
        self.conn.commit()

    def log_forecast(self, *, series: str, station: str, target_date: str,
                     mean_f: float, sigma_f: float, members: int, lead_hours: float,
                     source: str | None = None,
                     provenance: list[int] | None = None) -> None:
        self.conn.execute(
            """INSERT INTO forecasts (ts, series, station, target_date,
               predicted_mean_f, predicted_sigma_f, members, lead_hours,
               source, provenance)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (_now(), series, station, target_date, mean_f, sigma_f, members, lead_hours,
             source, _ids(provenance)),
        )
        self.conn.commit()

    # ---------------- schema migration ----------------
    #: Columns added after the first release, as {table: {column: decl}}.
    #: Adding a column is additive and idempotent; nothing is ever dropped or
    #: rewritten, so an older database keeps every row it already has.
    _ADDED_COLUMNS = {
        "probability_log": {
            "market_yes_bid_cents": "REAL",
            # 14A: the source_observations ids behind this probability.
            "provenance": "TEXT",
            # Phase B: which model priced it ("ensemble" | "nowcast"), and the
            # other model's probability for the same contract at the same
            # moment - so the two can be scored like for like.
            "model": "TEXT",
            "alt_probability": "REAL",
            # Package 3a: the PRODUCTION book at (about) the same moment, so
            # the model can be benchmarked against the market that matters.
            "prod_yes_ask_cents": "REAL",
            "prod_yes_bid_cents": "REAL",
            "prod_read_at": "TEXT",
        },
        # Package 3a: which build produced a run.
        "runs": {
            "code_version": "TEXT",
            "config_version": "TEXT",
            "model_versions": "TEXT",
            "fee_schedule_version": "TEXT",
        },
        # Package 2: which opportunity an execution and each of its legs -
        # and later the exits and settlements of that position - belong to.
        "executions": {"opportunity_uuid": "TEXT"},
        "leg_orders": {"opportunity_uuid": "TEXT"},
        "forecasts": {
            # 14A: which source the forecast came from, the observation ids
            # behind it, and which source supplied actual_f.
            "source": "TEXT",
            "provenance": "TEXT",
            "actual_source": "TEXT",
        },
    }

    def _migrate(self) -> None:
        for table, columns in self._ADDED_COLUMNS.items():
            try:
                have = {r["name"] for r in
                        self.conn.execute(f"PRAGMA table_info({table})")}
            except sqlite3.Error:
                continue
            if not have:
                continue
            for name, decl in columns.items():
                if name in have:
                    continue
                try:
                    self.conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                    self.conn.commit()
                    log.info("journal: added %s.%s", table, name)
                except sqlite3.Error as exc:
                    log.debug("could not add %s.%s: %s", table, name, exc)

    # ---------------- calibration ----------------
    def log_probability(self, *, strategy: str, estimator: str, series: str,
                        ticker: str, our_probability: float,
                        confidence: float | None = None,
                        market_yes_cents: float | None = None,
                        market_yes_bid_cents: float | None = None,
                        target_date: str = "", strike: str = "",
                        lead_hours: float | None = None,
                        traded: bool = False,
                        provenance: list[int] | None = None,
                        model: str | None = None,
                        alt_probability: float | None = None,
                        prod_yes_ask_cents: float | None = None,
                        prod_yes_bid_cents: float | None = None,
                        prod_read_at: str | None = None) -> None:
        """Record what we thought, before we know whether it was right.

        Written for every market an estimator prices - traded or not. Scoring
        only the ones we traded would measure the gate, not the forecast, and
        would throw away most of the data.
        """
        try:
            self.conn.execute(
                """INSERT OR IGNORE INTO probability_log
                   (ts, strategy, estimator, series, ticker, target_date, strike,
                    our_probability, confidence, market_yes_cents,
                    market_yes_bid_cents, lead_hours, traded, provenance,
                    model, alt_probability, prod_yes_ask_cents,
                    prod_yes_bid_cents, prod_read_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_now(), strategy, estimator, series, ticker, target_date, strike,
                 float(our_probability),
                 None if confidence is None else float(confidence),
                 None if market_yes_cents is None else float(market_yes_cents),
                 None if market_yes_bid_cents is None
                 else float(market_yes_bid_cents),
                 None if lead_hours is None else float(lead_hours),
                 1 if traded else 0, _ids(provenance), model,
                 None if alt_probability is None else float(alt_probability),
                 None if prod_yes_ask_cents is None else float(prod_yes_ask_cents),
                 None if prod_yes_bid_cents is None else float(prod_yes_bid_cents),
                 prod_read_at),
            )
            self.conn.commit()
        except sqlite3.Error as exc:
            log.debug("could not log probability for %s: %s", ticker, exc)

    def unsettled_probabilities(self, limit: int = 2000) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM probability_log WHERE settled_yes IS NULL "
            "ORDER BY ts LIMIT ?", (limit,)))

    def unsettled_tickers(self, limit: int = 300, skip: set | None = None) -> list[sqlite3.Row]:
        """Markets with unsettled forecasts, finished days first.

        Work comes by MARKET, ordered by target date (oldest first, undated
        last), so markets that cannot resolve yet - next month's CPI - never
        sit in front of ones that already have. Reading the oldest 2,000 ROWS
        did exactly that: from 1 Oct every slot was an inflation forecast for
        a release weeks away, and no weather forecast was scored for 12 days.
        """
        skip = skip or set()
        out = []
        for r in self.conn.execute(
                "SELECT ticker, MAX(target_date) target_date, COUNT(*) n, MIN(ts) first_ts "
                "FROM probability_log WHERE settled_yes IS NULL GROUP BY ticker "
                "ORDER BY (MAX(target_date) IS NULL), MAX(target_date), MIN(ts)"):
            if r["ticker"] in skip:
                continue
            out.append(r)
            if len(out) >= limit:
                break
        return out

    def settle_ticker(self, ticker: str, settled_yes: bool) -> list[float]:
        """Score every unsettled forecast of one market in one write. Returns
        their Brier scores."""
        outcome = 1 if settled_yes else 0
        rows = list(self.conn.execute(
            "SELECT id, our_probability FROM probability_log WHERE ticker=? "
            "AND settled_yes IS NULL", (ticker,)))
        now = _now()
        self.conn.executemany(
            "UPDATE probability_log SET settled_yes=?, settled_at=?, brier=? WHERE id=?",
            [(outcome, now, (float(r["our_probability"]) - outcome) ** 2, r["id"]) for r in rows])
        self.conn.commit()
        return [(float(r["our_probability"]) - outcome) ** 2 for r in rows]

    def settle_probability(self, row_id: int, settled_yes: bool) -> float:
        """Score one forecast against what happened. Returns its Brier score."""
        row = self.conn.execute(
            "SELECT our_probability FROM probability_log WHERE id=?",
            (row_id,)).fetchone()
        if row is None:
            return 0.0
        outcome = 1 if settled_yes else 0
        brier = (float(row["our_probability"]) - outcome) ** 2
        self.conn.execute(
            "UPDATE probability_log SET settled_yes=?, settled_at=?, brier=? "
            "WHERE id=?", (outcome, _now(), brier, row_id))
        self.conn.commit()
        return brier

    def log_leg_fill(self, *, ticker: str, side: str, action: str, filled: int,
                     price_cents: float, note: str = "",
                     order_id: str = "") -> None:
        """Record one fill the position worker made, in the table that counts.

        `entry_for` reconstructs what we hold from `leg_orders` by subtracting
        sells from buys. A sale that never reaches this table leaves that
        arithmetic describing a position larger than the one we have - which
        is exactly the class of error that silently disabled the stop loss
        once already.
        """
        try:
            uuid_ = self.uuid_for_position(ticker, side)
            self.conn.execute(
                """INSERT INTO leg_orders
                   (ts, execution_id, ticker, side, action, requested, filled,
                    limit_price_cents, avg_price_cents, fee_usd, state,
                    order_id, client_order_id, error, opportunity_uuid)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_now(), None, ticker, side, action, filled, filled,
                 int(price_cents), float(price_cents), None, "FILLED",
                 order_id, "", note[:300], uuid_),
            )
            if uuid_ and action == "sell":
                self._event(uuid_, "EXITED", f"sold {filled} {side} {ticker} "
                                             f"@ {price_cents:.0f}c {note[:120]}")
            self.conn.commit()
        except sqlite3.Error as exc:
            log.error("could not log the %s of %s: %s", action, ticker, exc)

    def calibration(self, estimator: str | None = None, series: str | None = None,
                    max_lead_hours: float | None = None,
                    since: str | None = None) -> dict:
        """Brier score and its components over settled forecasts.

        Brier decomposes usefully: a score can be bad because the model is
        biased (says 30% when things happen 60% of the time) or because it is
        timid (never strays from the base rate). `mean_forecast` against
        `base_rate` separates the two.
        """
        where = ["settled_yes IS NOT NULL"]
        args: list = []
        if estimator:
            where.append("estimator=?"); args.append(estimator)
        if series:
            where.append("series=?"); args.append(series)
        if max_lead_hours is not None:
            where.append("lead_hours IS NOT NULL AND lead_hours <= ?")
            args.append(max_lead_hours)
        if since:
            where.append("ts >= ?"); args.append(since)
        clause = " AND ".join(where)

        r = self.conn.execute(
            "SELECT COUNT(*) n, AVG(brier) brier, AVG(our_probability) mean_p, "
            f"AVG(settled_yes) base FROM probability_log WHERE {clause}",
            args).fetchone()
        n = int(r["n"] or 0)

        # Distinct CONTRACTS, which is the number that carries information.
        # The same contract is re-priced roughly six times a day, so 452
        # settled rows were 74 independent things and a bootstrap over
        # contracts put the skill interval across zero. A verdict read off
        # the row count states as settled what the evidence leaves open.
        contracts = int(self.conn.execute(
            f"SELECT COUNT(*) c FROM (SELECT DISTINCT ticker, strike "
            f"FROM probability_log WHERE {clause})", args).fetchone()["c"] or 0)

        # Distinct EVENTS (series x target date: one weather day, one data
        # release). Contracts of one event share one outcome draw - six CPI
        # strikes are one release, not six facts.
        events = int(self.conn.execute(
            f"SELECT COUNT(*) c FROM (SELECT DISTINCT series, target_date "
            f"FROM probability_log WHERE {clause})", args).fetchone()["c"] or 0)

        return {
            "n": n,
            "contracts": contracts,
            "events": events,
            "brier": float(r["brier"]) if n else None,
            "mean_forecast": float(r["mean_p"]) if n else None,
            "base_rate": float(r["base"]) if n else None,
        }

    def calibration_by(self, column: str, estimator: str | None = None) -> list[dict]:
        """Same, grouped. `column` must be a known column, not user input."""
        if column not in ("series", "estimator", "strategy"):
            raise ValueError(f"cannot group calibration by {column!r}")
        sql = (f"SELECT {column} AS k, COUNT(*) n, AVG(brier) brier,"
               " AVG(our_probability) mean_p, AVG(settled_yes) base"
               " FROM probability_log WHERE settled_yes IS NOT NULL")
        args: list = []
        if estimator:
            sql += " AND estimator=?"; args.append(estimator)
        sql += f" GROUP BY {column} ORDER BY n DESC"
        return [{"key": r["k"], "n": int(r["n"] or 0),
                 "brier": float(r["brier"]) if r["n"] else None,
                 "mean_forecast": float(r["mean_p"]) if r["n"] else None,
                 "base_rate": float(r["base"]) if r["n"] else None}
                for r in self.conn.execute(sql, args)]

    # -------------------------------------------------------------
    def open_trades(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM trades WHERE status != 'closed'"))

    def opened_at(self, ticker: str, side: str) -> str | None:
        """When the CURRENT holding in this market was opened.

        The earliest buy after the last time the position went flat. A stop
        loss needs this: without it there is no such thing as "too soon to
        stop", and a wide spread makes every entry an instant stop-out.
        """
        rows = list(self.conn.execute(
            "SELECT ts, action, filled FROM leg_orders "
            "WHERE ticker=? AND side=? AND filled > 0 ORDER BY id", (ticker, side)))
        net = 0
        opened: str | None = None
        for r in rows:
            n = int(r["filled"] or 0)
            if r["action"] == "buy":
                if net <= 0:
                    opened = r["ts"]
                net += n
            else:
                net -= n
                if net <= 0:
                    opened = None
        return opened if net > 0 else None

    def last_exit_at(self, ticker: str) -> str | None:
        """The most recent sell in this market, on either side."""
        row = self.conn.execute(
            "SELECT ts FROM leg_orders WHERE ticker=? AND action='sell' "
            "AND filled > 0 ORDER BY id DESC LIMIT 1", (ticker,)).fetchone()
        return row["ts"] if row else None

    #: How a settlement leg is marked, so it is never mistaken for a trade.
    SETTLEMENT_NOTE = "SETTLEMENT"

    def open_lines(self) -> list[tuple[str, str, int]]:
        """Every (ticker, side, contracts) our fills say is still open.

        SIGNED. A positive count is a long. A NEGATIVE count means more was
        sold than bought on that side, which on this exchange is a short - the
        bot opened four of them between 19 and 20 September by selling into
        positions it had already closed.

        Both need closing out at settlement, in opposite directions, so both
        are returned. Derived from leg_orders, the only record of what this
        bot actually did.
        """
        net: dict[tuple[str, str], int] = {}
        for r in self.conn.execute(
                "SELECT ticker, side, action, filled FROM leg_orders "
                "WHERE filled > 0 ORDER BY id"):
            n = int(r["filled"] or 0)
            key = (r["ticker"], r["side"])
            net[key] = net.get(key, 0) + (n if r["action"] == "buy" else -n)
        return [(t, sd, n) for (t, sd), n in net.items() if n != 0]

    def log_settlement(self, *, ticker: str, side: str, contracts: int,
                       won: bool) -> bool:
        """Close a position out at what settlement actually paid.

        A settled contract pays 100c or 0c and no order is ever sent, so
        nothing was being written to leg_orders - the table every money number
        on the dashboard is built from. The consequence was not cosmetic:
        a position that SETTLED rather than being sold contributed nothing to
        realised P/L and sat in the open list forever. For a weather bot, that
        is how most positions end, so most of the P/L was missing.

        Written at the settlement price and marked with SETTLEMENT_NOTE, so
        the arithmetic closes, the payout lands in the P/L, and nobody later
        mistakes it for a trade that was executed.

        `contracts` is SIGNED. A long closes with a sell; a short - one of the
        four the bot opened by selling into a position it had already closed -
        closes with a BUY at the same price. Getting that direction wrong
        would leave the line at double the size instead of flat.

        `won` says whether THE CONTRACT NAMED BY `side` paid out - a NO
        contract on a market that resolved NO did. That is a fact about the
        contract, not about us, so the settlement PRICE is the same either
        way: 100c if it paid out, 0c if it did not. Only the direction of the
        closing leg depends on which way we were holding it.

        Short 13 NO at 41c, NO expires worthless: buy 13 back at 0c, and the
        $5.33 received is kept. Short 13 NO at 41c, NO pays out: buy 13 back
        at 100c for $13.00, a $7.67 loss. Both fall out of the arithmetic
        without a special case.

        Returns True if it wrote a row, False if this line was already booked.
        """
        already = self.conn.execute(
            "SELECT 1 FROM leg_orders WHERE ticker=? AND side=? AND state=? "
            "LIMIT 1", (ticker, side, "SETTLED")).fetchone()
        if already:
            return False
        if not contracts:
            return False
        action = "sell" if contracts > 0 else "buy"
        n = abs(int(contracts))
        price = 100 if won else 0
        try:
            uuid_ = self.uuid_for_position(ticker, side)
            self.conn.execute(
                """INSERT INTO leg_orders
                   (ts, execution_id, ticker, side, action, requested, filled,
                    limit_price_cents, avg_price_cents, fee_usd, state,
                    order_id, client_order_id, error, opportunity_uuid)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_now(), None, ticker, side, action, n, n,
                 price, float(price), 0.0, "SETTLED",
                 "", "", self.SETTLEMENT_NOTE, uuid_),
            )
            if uuid_:
                self._event(uuid_, "SETTLED", f"{ticker} {side} {n} at {price}c")
            self.conn.commit()
            return True
        except sqlite3.Error as exc:
            log.error("could not book settlement for %s %s: %s",
                      ticker, side, exc)
            return False

    def realised_pnl(self, since: str | None = None) -> float:
        """Realised P/L from fills, optionally only those closed since a time.

        Matched buy/sell pairs per (ticker, side), which now includes
        settlement legs - so a position that resolved rather than being sold
        finally counts. Before settlement booking existed this number was
        missing most of the P/L, which is why nothing could safely be built
        on top of it.
        """
        flows: dict[tuple[str, str], dict] = {}
        for r in self.conn.execute(
                "SELECT ticker, side, action, filled, avg_price_cents, "
                "limit_price_cents, ts FROM leg_orders WHERE filled > 0 "
                "ORDER BY id"):
            f = flows.setdefault((r["ticker"], r["side"]),
                                 {"b": 0, "s": 0, "sp": 0.0, "g": 0.0,
                                  "last": ""})
            n = int(r["filled"] or 0)
            px = float(r["avg_price_cents"] or r["limit_price_cents"] or 0) / 100.0
            if r["action"] == "buy":
                f["b"] += n
                f["sp"] += n * px
            else:
                f["s"] += n
                f["g"] += n * px
                f["last"] = r["ts"] or ""

        total = 0.0
        for f in flows.values():
            if not (f["b"] and f["s"]):
                continue
            if since and f["last"] < since:
                continue
            matched = min(f["b"], f["s"])
            total += matched * (f["g"] / f["s"] - f["sp"] / f["b"])
        return total

    def log_book_quality(self, row: dict) -> None:
        """Record one demo-vs-live book comparison."""
        cols = ("ticker", "side", "action", "size",
                "demo_best_cents", "demo_spread_cents", "demo_depth_at_best",
                "demo_vwap_cents", "demo_filled", "demo_complete",
                "demo_slippage_cents",
                "live_best_cents", "live_spread_cents", "live_depth_at_best",
                "live_vwap_cents", "live_filled", "live_complete",
                "live_slippage_cents", "cost_gap_cents", "slippage_gap_cents")
        vals = [_now()]
        for c in cols:
            v = row.get(c)
            vals.append(int(v) if isinstance(v, bool) else v)
        try:
            self.conn.execute(
                f"INSERT INTO book_quality (ts, {', '.join(cols)}) "
                f"VALUES ({', '.join(['?'] * (len(cols) + 1))})", vals)
            self.conn.commit()
        except sqlite3.Error as exc:
            log.debug("could not log book quality for %s: %s",
                      row.get("ticker"), exc)

    def book_quality(self, since: str | None = None) -> list[dict]:
        sql = "SELECT * FROM book_quality"
        args: tuple = ()
        if since:
            sql += " WHERE ts >= ?"
            args = (since,)
        try:
            return [dict(r) for r in self.conn.execute(sql + " ORDER BY id", args)]
        except sqlite3.Error:
            return []

    def held_from_fills(self, ticker: str, side: str) -> int | None:
        """What OUR OWN fill record says is still held. None = no opinion.

        The distinction matters. A return of 0 means "I have fills for this
        market and they net to flat"; None means "I have never recorded a fill
        here, so do not let me veto anything".

        This exists because the exchange lags and this table does not. Every
        order this bot sends is written here the moment it is accepted, while
        /portfolio/positions can still report the pre-trade holding seconds
        later. Between 19 and 20 September that gap turned five exits into
        five new SHORT positions: the bot sold out of a market, the next
        review still saw the old quantity, and sold again with nothing left to
        sell. On Kalshi a sell with no position open is not a no-op - it opens
        the opposite position, at whatever price was on the screen.
        """
        rows = list(self.conn.execute(
            "SELECT action, filled FROM leg_orders "
            "WHERE ticker=? AND side=? AND filled > 0", (ticker, side)))
        if not rows:
            return None
        net = 0
        for r in rows:
            n = int(r["filled"] or 0)
            net += n if r["action"] == "buy" else -n
        if net < 0:
            # More sold than bought on this side. That is not "we hold zero" -
            # it is proof that our accounting for this market is already
            # wrong, and a broken ledger must not be allowed to veto an exit.
            # Vetoing here would trap a real position on the exchange behind
            # our own bad arithmetic. Abstain, loudly, and let the exchange
            # decide; `diagnostics/reconcile.py` is where this gets resolved.
            log.warning("JOURNAL  our fill record for %s %s is oversold by %d "
                        "- abstaining rather than blocking an exit; run "
                        "RECONCILE", ticker, side.upper(), -net)
            return None
        return net

    def entry_for(self, ticker: str, side: str) -> tuple[int, float | None]:
        """What we paid for what we still hold, from the REAL fill record.

        There are two write paths into this database and they do not share a
        table. The original weather engine writes `trades`; the arbitrage and
        forecast engines write `leg_orders` through the execution manager - and
        `leg_orders` is where every fill this bot has actually made lives.

        Reading only `trades` returned no entry price for any real position,
        which silently disabled both exit rules that need one: the stop loss
        and the thesis check. Two of six rules, dead and quiet.

        Returns (net contracts still held, weighted average buy price in cents).
        """
        rows = list(self.conn.execute(
            "SELECT action, filled, avg_price_cents, limit_price_cents "
            "FROM leg_orders WHERE ticker=? AND side=? AND filled > 0",
            (ticker, side)))
        bought = sold = 0
        spend = 0.0
        for r in rows:
            n = int(r["filled"] or 0)
            price = r["avg_price_cents"] or r["limit_price_cents"] or 0
            if r["action"] == "buy":
                bought += n
                spend += n * float(price)
            else:
                sold += n
        net = bought - sold
        if bought <= 0:
            # Fall back to the older `trades` table for positions opened by
            # the original engine.
            legacy = list(self.conn.execute(
                "SELECT contracts, price_cents FROM trades "
                "WHERE ticker=? AND side=? AND action='buy' AND status != 'closed'",
                (ticker, side)))
            n = sum(int(r["contracts"] or 0) for r in legacy)
            if not n:
                return 0, None
            spend = sum(int(r["contracts"] or 0) * float(r["price_cents"] or 0)
                        for r in legacy)
            return n, spend / n
        return net, spend / bought

    def realized_pnl_today_usd(self) -> float:
        today = datetime.now(timezone.utc).date().isoformat()
        row = self.conn.execute(
            "SELECT COALESCE(SUM(pnl_usd),0) AS s FROM trades WHERE exit_ts LIKE ?",
            (f"{today}%",),
        ).fetchone()
        return float(row["s"] or 0.0)

    def consecutive_losses(self) -> int:
        """How many closed positions in a row lost money, most recent first.

        The model breaking does not look like one big loss; it looks like a
        run of small ones. This is the number meant to notice - and until
        24 September it read only the `trades` table, which nothing on the
        live path writes. It therefore returned 0 forever, which is why
        max_consecutive_losses never fired however badly a day went.

        Reads real fills first and falls back to `trades` for the offline
        simulator, which is the only thing that still writes there.

        Only positions closed AFTER the latest breaker reset count. Without
        that, the run could never end once the breaker tripped: the count
        reads all history, only a winning close ends a run, and a tripped
        breaker opens nothing that could win - so a restart re-tripped it on
        the first check, forever (26 September).
        """
        since = self.last_breaker_reset() or ""
        closed: list[tuple[str, float]] = []
        flows: dict[tuple[str, str], dict] = {}
        for r in self.conn.execute(
                "SELECT ticker, side, action, filled, avg_price_cents, "
                "limit_price_cents, ts FROM leg_orders WHERE filled > 0 "
                "ORDER BY id"):
            f = flows.setdefault((r["ticker"], r["side"]),
                                 {"b": 0, "s": 0, "sp": 0.0, "g": 0.0,
                                  "last": ""})
            n = int(r["filled"] or 0)
            px = float(r["avg_price_cents"] or r["limit_price_cents"] or 0) / 100.0
            if r["action"] == "buy":
                f["b"] += n
                f["sp"] += n * px
            else:
                f["s"] += n
                f["g"] += n * px
                f["last"] = r["ts"] or ""
        for f in flows.values():
            if f["b"] and f["s"] and f["last"] > since:
                matched = min(f["b"], f["s"])
                closed.append((f["last"],
                               matched * (f["g"] / f["s"] - f["sp"] / f["b"])))
        if closed or since:
            closed.sort(key=lambda x: x[0], reverse=True)
            run = 0
            for _ts, pnl in closed:
                if pnl < 0:
                    run += 1
                else:
                    break
            return run

        rows = self.conn.execute(
            "SELECT pnl_usd FROM trades WHERE status='closed' ORDER BY id DESC LIMIT 50"
        ).fetchall()
        n = 0
        for r in rows:
            if (r["pnl_usd"] or 0) < 0:
                n += 1
            else:
                break
        return n

    def log_balance(self, balance_usd: float) -> None:
        self.conn.execute("INSERT INTO account_snapshots (ts, balance_usd) VALUES (?, ?)",
                          (_now(), float(balance_usd)))
        self.conn.commit()

    def last_breaker_reset(self) -> str | None:
        row = self.conn.execute(
            "SELECT ts FROM breaker_resets ORDER BY id DESC LIMIT 1").fetchone()
        return row["ts"] if row else None

    def reset_breaker(self, note: str = "") -> int:
        """The operator has seen the losing run and says: count from here.
        Returns how many losses in a row there were at the moment of reset."""
        losses = self.consecutive_losses()
        self.conn.execute(
            "INSERT INTO breaker_resets (ts, losses_at_reset, note) VALUES (?,?,?)",
            (_now(), losses, note[:500]))
        self.conn.commit()
        return losses

    def log_scan(self, mode: str, counts: dict, seconds: float = 0.0) -> None:
        """One row per scan, so you can always tell a bot that is running and
        finding nothing from a bot that is not running at all."""
        self.conn.execute(
            """INSERT INTO scans (ts, mode, markets_seen, no_orderbook, wrong_window,
               other_untradable, priced, edge_too_small, risk_rejected, traded, seconds)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (_now(), mode, counts.get("seen", 0), counts.get("no_orderbook", 0),
             counts.get("wrong_window", 0), counts.get("other_untradable", 0),
             counts.get("priced", 0), counts.get("edge_too_small", 0),
             counts.get("risk_rejected", 0), counts.get("traded", 0), seconds),
        )
        self.conn.commit()

    def last_scan(self):
        return self.conn.execute(
            "SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()

    def log_opportunity(self, mode: str, op, decision) -> int:
        legs = [{"ticker": l.ticker, "side": l.side, "action": l.action,
                 "quantity": l.quantity, "limit_price_cents": l.limit_price_cents,
                 "vwap_cents": round(l.vwap_cents, 3), "available": l.available}
                for l in op.legs]
        cur = self.conn.execute(
            """INSERT INTO opportunities
               (ts, mode, strategy, event_id, tickers, n_legs, quantity,
                max_executable, min_profitable, gross_cost, guaranteed_min_payout,
                gross_edge, estimated_fees, net_edge, net_edge_per_contract,
                expected_return_pct, fee_drag_pct, execution_risk, guaranteed,
                decision, reject_reason, detail, rationale, legs_json, evidence)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_now(), mode, op.strategy_type, op.event_id,
             ",".join(op.market_tickers), op.n_legs, op.quantity,
             op.max_executable_quantity, op.min_profitable_quantity,
             op.gross_cost, op.guaranteed_min_payout, op.gross_edge,
             op.estimated_fees, op.net_edge, op.net_edge_per_contract,
             op.expected_return_pct, op.fee_drag_pct,
             op.execution_risk.value, int(op.guaranteed),
             "TRADE" if decision.accepted else "REJECT",
             decision.reason.value, decision.detail[:400], op.rationale[:400],
             json.dumps(legs), json.dumps(op.evidence, default=str)[:4000]),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def log_trade_result(self, result, opportunity_id: int | None = None) -> int:
        op = result.opportunity
        uuid_ = getattr(op, "opportunity_id", None)
        if opportunity_id is None:
            opportunity_id = getattr(op, "journal_row_id", None)
        cur = self.conn.execute(
            """INSERT INTO executions
               (ts, opportunity_id, opportunity_uuid, mode, strategy, event_id, outcome,
                sets_completed, total_cost_usd, total_fees_usd,
                realised_edge_usd, exposed, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_now(), opportunity_id, uuid_, getattr(result, "mode", ""), op.strategy_type,
             op.event_id, result.outcome.value, result.sets_completed,
             result.total_cost_usd, result.total_fees_usd,
             result.realised_edge_usd, int(result.exposed),
             " | ".join(result.notes)[:1000]),
        )
        exec_id = int(cur.lastrowid)
        for lx in result.legs:
            self.conn.execute(
                """INSERT INTO leg_orders
                   (ts, execution_id, ticker, side, action, requested, filled,
                    limit_price_cents, avg_price_cents, fee_usd, state,
                    order_id, client_order_id, error, opportunity_uuid)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_now(), exec_id, lx.leg.ticker, lx.leg.side, lx.leg.action,
                 lx.requested, lx.filled, lx.leg.limit_price_cents,
                 lx.avg_price_cents, lx.fee_usd, lx.state.value,
                 lx.order_id, lx.client_order_id, lx.error[:300], uuid_),
            )
        if uuid_:
            outcome = getattr(result.outcome, "value", str(result.outcome))
            state = {"COMPLETE": "FILLED", "PARTIAL_FILLED": "PARTIALLY_FILLED",
                     "PARTIAL_EXPOSED": "PARTIALLY_FILLED",
                     "PARTIAL_RECOVERED": "NOT_FILLED",
                     "ABORTED_PRE_TRADE": "EXPIRED", "FAILED": "NOT_FILLED"
                     }.get(outcome, outcome)
            self._event(uuid_, "SUBMITTED", f"execution {exec_id}")
            self._event(uuid_, state, f"{outcome}; " + " | ".join(result.notes)[:200])
        self.conn.commit()
        return exec_id

    def arbitrage_summary(self) -> dict:
        def one(sql, *a):
            row = self.conn.execute(sql, a).fetchone()
            return row[0] if row and row[0] is not None else 0
        return {
            "opportunities": one("SELECT COUNT(*) FROM opportunities"),
            "accepted": one("SELECT COUNT(*) FROM opportunities WHERE decision='TRADE'"),
            "rejected": one("SELECT COUNT(*) FROM opportunities WHERE decision='REJECT'"),
            "executions": one("SELECT COUNT(*) FROM executions"),
            "complete": one("SELECT COUNT(*) FROM executions WHERE outcome='COMPLETE'"),
            "partial": one("SELECT COUNT(*) FROM executions WHERE outcome LIKE 'PARTIAL%'"),
            "exposed": one("SELECT COUNT(*) FROM executions WHERE exposed=1"),
            # Guaranteed edge booked at entry - arbitrage only. Forecast
            # trades have no guaranteed payout and no entry edge to book.
            "net_pnl": one("SELECT SUM(realised_edge_usd) FROM executions "
                           "WHERE strategy NOT LIKE 'forecast%'"),
            "fees": one("SELECT SUM(total_fees_usd) FROM executions"),
            "legs_submitted": one("SELECT COUNT(*) FROM leg_orders"),
            "legs_filled": one("SELECT COUNT(*) FROM leg_orders WHERE state='FILLED'"),
            "legs_partial": one("SELECT COUNT(*) FROM leg_orders WHERE state='PARTIALLY_FILLED'"),
        }

    def start_run(self, mode: str, note: str = "", *, code_version: str = "",
                  config_version: str = "", model_versions: dict | None = None,
                  fee_schedule_version: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_ts, mode, note, code_version, config_version, "
            "model_versions, fee_schedule_version) VALUES (?,?,?,?,?,?,?)",
            (_now(), mode, note, code_version, config_version,
             json.dumps(model_versions or {}, sort_keys=True), fee_schedule_version))
        self.conn.commit()
        return int(cur.lastrowid)

    # ---------------- maker shadow (package C2, phase M0) ----------------
    _MAKER_COLUMNS = (
        "created_at", "ticker", "series", "side", "probability_yes", "fair_cents",
        "stage", "reason", "lead_hours", "book_age_s", "best_bid", "best_ask",
        "price_cents", "contracts", "queue_ahead", "maker_fee_cents",
        "taker_fee_cents", "expires_at", "state", "filled", "first_fill_at",
        "full_fill_at", "fill_kind", "drift_5m", "drift_30m", "drift_60m",
        "closed_at", "settled_yes", "maker_markout_cents", "taker_markout_cents",
        "code_version", "config_version")

    def maker_shadow_insert(self, row: dict) -> int:
        cols = [c for c in self._MAKER_COLUMNS if c in row]
        cur = self.conn.execute(
            f"INSERT INTO maker_shadow ({', '.join(cols)}) VALUES "
            f"({', '.join('?' for _ in cols)})", [row[c] for c in cols])
        self.conn.commit()
        return int(cur.lastrowid)

    def maker_shadow_update(self, row_id: int, fields: dict) -> None:
        cols = [c for c in fields if c in self._MAKER_COLUMNS]
        if not cols:
            return
        self.conn.execute(
            f"UPDATE maker_shadow SET {', '.join(c + '=?' for c in cols)} WHERE id=?",
            [fields[c] for c in cols] + [row_id])
        self.conn.commit()

    # ---------------- live paper account (packages 6/7) ----------------
    _PAPER_COLUMNS = (
        "opened_at", "kind", "group_id", "strategy", "event_id", "ticker", "side",
        "contracts", "price_cents", "cost_usd", "fee_usd", "expected_edge_usd",
        "probability_yes", "confidence", "lead_hours", "model", "gate_blocked",
        "note", "book_age_s", "state", "settled_at", "settled_yes", "payout_usd",
        "pnl_usd", "code_version", "config_version")
    _PROD_ARB_COLUMNS = ("ts", "event_id", "markets", "prod_books", "demo_found",
                         "demo_best_edge", "prod_found", "prod_accepted",
                         "prod_best_edge", "prod_near_miss_gap")

    def _insert(self, table: str, allowed: tuple, row: dict) -> int:
        cols = [c for c in allowed if c in row]
        cur = self.conn.execute(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES "
            f"({', '.join('?' for _ in cols)})", [row[c] for c in cols])
        self.conn.commit()
        return int(cur.lastrowid)

    def paper_insert(self, row: dict) -> int:
        return self._insert("paper_positions", self._PAPER_COLUMNS, row)

    def paper_update(self, row_id: int, fields: dict) -> None:
        cols = [c for c in fields if c in self._PAPER_COLUMNS]
        if cols:
            self.conn.execute(
                f"UPDATE paper_positions SET {', '.join(c + '=?' for c in cols)} "
                f"WHERE id=?", [fields[c] for c in cols] + [row_id])
            self.conn.commit()

    def paper_rows(self, where: str = "1=1", args: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            f"SELECT * FROM paper_positions WHERE {where} ORDER BY id", args)]

    def prod_arb_insert(self, row: dict) -> int:
        return self._insert("prod_arb_checks", self._PROD_ARB_COLUMNS, row)

    def maker_shadow_rows(self, where: str = "1=1", args: tuple = (),
                          limit: int = 5000) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            f"SELECT * FROM maker_shadow WHERE {where} ORDER BY id LIMIT ?",
            (*args, limit))]

    def end_run(self, run_id: int) -> None:
        self.conn.execute("UPDATE runs SET ended_ts=? WHERE id=?", (_now(), run_id))
        self.conn.commit()

    # ---------------- sources (package 14A) ----------------
    def log_source_observation(self, *, source_id: str, publisher: str = "",
                               tier: int | None = None, endpoint: str = "",
                               data_type: str = "", subject: str = "",
                               target_date: str | None = None,
                               value: float | None = None, value_json: Any = None,
                               data_time: str | None = None, data_vintage: Any = None,
                               retrieved_at: str | None = None,
                               latency_ms: float | None = None) -> int:
        """One external data point, with where and when it came from."""
        def _j(v):
            if v is None or isinstance(v, str):
                return v
            return json.dumps(v, default=str)[:4000]
        cur = self.conn.execute(
            """INSERT INTO source_observations
               (retrieved_at, source_id, publisher, tier, endpoint, data_type,
                subject, target_date, value, value_json, data_time, data_vintage,
                latency_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (retrieved_at or _now(), source_id, publisher, tier, endpoint,
             data_type, subject, target_date,
             None if value is None else float(value), _j(value_json),
             data_time, _j(data_vintage), latency_ms),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def log_data_check(self, *, series: str, target_date: str,
                       lead_hours=None, ensemble_f=None, cross_check_f=None,
                       diff_f=None, forecast_age_seconds=None,
                       observation_age_seconds=None, observation_suspect=None,
                       verdicts: list[str] | None = None, enforced: bool = False,
                       provenance: list[int] | None = None) -> int:
        cur = self.conn.execute(
            """INSERT INTO data_checks
               (ts, series, target_date, lead_hours, ensemble_f, cross_check_f,
                diff_f, forecast_age_seconds, observation_age_seconds,
                observation_suspect, verdicts, enforced, provenance)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_now(), series, target_date, lead_hours, ensemble_f, cross_check_f,
             diff_f, forecast_age_seconds, observation_age_seconds,
             observation_suspect, ",".join(verdicts or []) or None,
             1 if enforced else 0, _ids(provenance)))
        self.conn.commit()
        return int(cur.lastrowid)

    def save_source_health(self, rows: list[dict]) -> None:
        now = _now()
        cols = ("state", "detail", "publisher", "tier", "calls", "failures",
                "consecutive_failures", "error_rate", "last_success_at",
                "last_failure_at", "last_status", "last_error",
                "last_latency_ms", "last_data_time")
        self.conn.executemany(
            f"""INSERT INTO source_health (source_id, updated_at, {", ".join(cols)})
                VALUES (?, ?, {", ".join("?" for _ in cols)})
                ON CONFLICT(source_id) DO UPDATE SET updated_at=excluded.updated_at,
                {", ".join(f"{c}=excluded.{c}" for c in cols)}""",
            [(r["source_id"], now, *(r.get(c) for c in cols)) for r in rows])
        self.conn.commit()

    def record_settlement_sources(self, series: str,
                                  sources: list[dict]) -> tuple[list, list]:
        """Store what Kalshi says `series` settles on.

        Returns (added, dropped) against what was stored before - both empty
        when nothing changed, which is the normal case. A change means the
        contract now resolves on something else, which a person should see.
        """
        now = _now()
        seen = {(str(x.get("name") or ""), str(x.get("url") or ""))
                for x in (sources or [])}
        before = {(r["name"], r["url"]) for r in self.conn.execute(
            "SELECT name, url FROM settlement_sources WHERE series=?", (series,))}
        for name, url in seen:
            self.conn.execute(
                """INSERT INTO settlement_sources (series, name, url, first_seen, last_seen)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(series, name, url) DO UPDATE SET last_seen=excluded.last_seen""",
                (series, name, url, now, now))
        self.conn.commit()
        if not before:
            return [], []            # first sighting is not a change
        return sorted(seen - before), sorted(before - seen)

    # ---------------- opportunity ledger (Package 2) ----------------
    def _event(self, opportunity_id: str, state: str, detail: str = "",
               final: bool = True) -> None:
        """One lifecycle transition. Caller commits (or holds the lock)."""
        self.conn.execute(
            "INSERT INTO opportunity_events (opportunity_id, ts, state, detail) "
            "VALUES (?,?,?,?)", (opportunity_id, _now(), state, detail[:500]))
        if final:
            self.conn.execute("UPDATE opportunity_ledger SET final_state=? "
                              "WHERE opportunity_id=?", (state, opportunity_id))

    def ledger_record(self, op, dedupe_key: str | None = None) -> bool:
        """Write one evaluated opportunity. True if a row was written, False
        if an identical one (same dedupe_key) is already there. Raises on a
        database error - the caller decides whether that blocks a trade."""
        rec = op.to_record()
        ev = rec["ev"]
        touch = (op.evidence or {}).get("touch") or {
            l["ticker"] + ":" + l["side"]: {"ask": l["best_ask_cents"],
                                            "bid": l["best_bid_cents"]}
            for l in rec["legs"]}
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO opportunity_ledger
               (opportunity_id, dedupe_key, detected_at, opportunity_class, strategy,
                event_id, markets, direction, quantity, probability_yes,
                fair_probability, market_probability, market_price_source,
                touch_json, capital_required, max_loss, max_profit, gross_edge,
                fees, net_ev, ev_complete, settlement_time, liquidity_available,
                data_quality, source_quality_tier, confidence, model_version,
                code_version, config_version, fee_schedule_version,
                execution_mode, decision, decision_stage, rejection_reason,
                final_state, record_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rec["opportunity_id"], dedupe_key, rec["detected_at"],
             rec["opportunity_class"], rec["strategy"], rec["event_id"],
             ",".join(rec["markets"]), rec["direction"], rec["quantity"],
             (op.evidence or {}).get("probability_yes",
                                     op.probability_yes if not op.guaranteed else None),
             rec["fair_probability"], rec["market_probability"],
             rec["market_price_source"], json.dumps(touch, default=str),
             rec["capital_required"], rec["max_loss"], rec["max_profit"],
             rec["gross_edge"], rec["fees"], ev["net_ev"], int(ev["complete"]),
             rec["settlement_time"], (rec["liquidity"] or {}).get("available"),
             rec["data_quality"], rec["source_quality_tier"], rec["confidence"],
             rec["model_version"], rec["code_version"], rec["config_version"],
             rec["fee_schedule_version"], rec["execution_mode"], rec["decision"],
             rec["decision_stage"], rec["rejection_reason"], rec["decision"],
             json.dumps(rec, default=str)))
        written = cur.rowcount == 1
        if written:
            self._event(rec["opportunity_id"], "DETECTED", final=False)
            if rec["decision"] != "DETECTED":
                self._event(rec["opportunity_id"], rec["decision"],
                            f"{rec['decision_stage']}: {rec['rejection_reason']}")
        self.conn.commit()
        return written

    def ledger_decide(self, op) -> None:
        """Record a later decision on an already-written opportunity (the
        risk check comes after the gate, and it is the FINAL word)."""
        self.conn.execute(
            """UPDATE opportunity_ledger SET decision=?, decision_stage=?,
               rejection_reason=?, execution_mode=? WHERE opportunity_id=?""",
            (op.decision, op.decision_stage, op.rejection_reason,
             op.execution_mode, op.opportunity_id))
        self._event(op.opportunity_id, op.decision,
                    f"{op.decision_stage}: {op.rejection_reason}")
        self.conn.commit()

    def uuid_for_position(self, ticker: str, side: str) -> str | None:
        """The opportunity whose entry opened this ticker/side most recently."""
        r = self.conn.execute(
            "SELECT opportunity_uuid FROM leg_orders WHERE ticker=? AND side=? "
            "AND action='buy' AND opportunity_uuid IS NOT NULL "
            "ORDER BY id DESC LIMIT 1", (ticker, side)).fetchone()
        return r[0] if r else None

    def pending_outcomes(self, now_iso: str, limit: int = 200) -> list:
        return self.conn.execute(
            """SELECT * FROM opportunity_ledger WHERE resolved_at IS NULL
               AND settlement_time IS NOT NULL AND settlement_time < ?
               ORDER BY settlement_time LIMIT ?""", (now_iso, limit)).fetchall()

    def set_outcome(self, opportunity_id: str, outcome: dict | None,
                    realised: float | None, hypothetical: float | None,
                    basis: str) -> None:
        self.conn.execute(
            """UPDATE opportunity_ledger SET outcome_json=?, resolved_at=?,
               realised_pnl=?, hypothetical_pnl=?, hypothetical_basis=?
               WHERE opportunity_id=?""",
            (json.dumps(outcome) if outcome is not None else None, _now(),
             realised, hypothetical, basis[:300], opportunity_id))
        self.conn.commit()

    def realised_for(self, opportunity_id: str) -> tuple[float | None, bool]:
        """(realised P&L, fully closed?) from the fills, exits and settlement
        legs linked to this opportunity."""
        flows: dict = {}
        for r in self.conn.execute(
                "SELECT ticker, side, action, filled, avg_price_cents, "
                "limit_price_cents FROM leg_orders WHERE opportunity_uuid=? "
                "AND filled > 0 ORDER BY id", (opportunity_id,)):
            f = flows.setdefault((r[0], r[1]), [0, 0, 0.0, 0.0])
            px = float(r[4] if r[4] is not None else (r[5] or 0)) / 100.0
            if r[2] == "buy":
                f[0] += r[3]; f[2] += r[3] * px
            else:
                f[1] += r[3]; f[3] += r[3] * px
        if not flows:
            return None, False
        closed = all(b == s_ for b, s_, _x, _y in flows.values())
        pnl = sum(min(b, s_) * (g / s_ - sp / b)
                  for b, s_, sp, g in flows.values() if b and s_)
        return pnl, closed

    def prune_ledger_records(self, days: int = 90) -> int:
        """Drop the full JSON of old REJECTED rows; every column, and every
        accepted or traded row, is kept."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = self.conn.execute(
            "UPDATE opportunity_ledger SET record_json=NULL WHERE decision='REJECTED' "
            "AND record_json IS NOT NULL AND detected_at < ?", (cutoff,))
        self.conn.commit()
        return cur.rowcount

    def close(self) -> None:
        self.conn.close()


def _locked(fn):
    @functools.wraps(fn)
    def inner(self, *a, **kw):
        with self.lock:
            return fn(self, *a, **kw)
    return inner


#: Every method that writes. Held for the whole execute...commit (audit H1).
_WRITERS = ("log_decision", "log_trade", "close_open_trades", "close_trade",
            "log_forecast", "log_probability", "settle_probability", "log_leg_fill",
            "log_settlement", "log_book_quality", "log_balance", "reset_breaker",
            "log_scan", "log_opportunity", "log_trade_result", "start_run", "end_run",
            "log_source_observation", "log_data_check", "save_source_health",
            "record_settlement_sources", "ledger_record", "ledger_decide",
            "set_outcome", "prune_ledger_records", "maker_shadow_insert",
            "maker_shadow_update", "paper_insert", "paper_update", "prod_arb_insert")
for _name in _WRITERS:
    setattr(Journal, _name, _locked(getattr(Journal, _name)))
