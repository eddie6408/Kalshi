"""The 21 September audit produced exactly two code changes. These are them.

1. max_lead_hours - trading is gated to the only lead-time band whose skill
   interval excludes zero. A regression here silently restores the band that
   measured worse than a climate table.
2. probability_log.market_yes_bid_cents - the audit could not compute a
   model-vs-market comparison because only the ask was ever stored. The column
   has to appear on databases that predate it, without touching their rows.
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.journal import Journal
from kalshibot.strategies.forecast import ForecastConfig


def test_the_lead_gate_defaults_to_the_measured_band():
    assert ForecastConfig().max_lead_hours == 6.0
    assert ForecastConfig.from_dict({}).max_lead_hours == 6.0


def test_the_lead_gate_is_still_configurable_and_disablable():
    assert ForecastConfig.from_dict({"max_lead_hours": 12}).max_lead_hours == 12.0
    assert ForecastConfig.from_dict({"max_lead_hours": 0}).max_lead_hours == 0.0


def test_both_sides_of_the_book_are_recorded():
    with tempfile.TemporaryDirectory() as tmp:
        j = Journal(Path(tmp) / "j.db")
        j.log_probability(strategy="forecast:w", estimator="w", series="S",
                          ticker="T-1", our_probability=0.4, confidence=0.9,
                          market_yes_cents=55, market_yes_bid_cents=40,
                          lead_hours=3.0)
        row = j.conn.execute("SELECT * FROM probability_log").fetchone()
        assert row["market_yes_cents"] == 55
        assert row["market_yes_bid_cents"] == 40
        # the mid, which is the number the audit actually needs
        mid = (row["market_yes_cents"] + row["market_yes_bid_cents"]) / 2
        assert mid == 47.5


def test_a_database_written_before_the_column_existed_is_migrated_in_place():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "old.db"
        # a journal as it looked before the audit: no bid column, one row
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE probability_log ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,"
            " strategy TEXT, estimator TEXT, series TEXT, ticker TEXT NOT NULL,"
            " target_date TEXT, strike TEXT, our_probability REAL NOT NULL,"
            " confidence REAL, market_yes_cents REAL, lead_hours REAL,"
            " traded INTEGER DEFAULT 0, settled_yes INTEGER, settled_at TEXT,"
            " brier REAL, UNIQUE (ticker, ts));")
        conn.execute("INSERT INTO probability_log (ts, ticker, our_probability,"
                     " market_yes_cents) VALUES ('2026-09-20T00:00:00Z',"
                     " 'OLD-1', 0.31, 44)")
        conn.commit()
        conn.close()

        j = Journal(path)
        cols = {r["name"] for r in
                j.conn.execute("PRAGMA table_info(probability_log)")}
        assert "market_yes_bid_cents" in cols

        # the pre-existing row survived untouched, with NULL for the new column
        old = j.conn.execute(
            "SELECT * FROM probability_log WHERE ticker='OLD-1'").fetchone()
        assert old["our_probability"] == 0.31
        assert old["market_yes_cents"] == 44
        assert old["market_yes_bid_cents"] is None

        # and opening it a second time is a no-op rather than an error
        j.conn.close()
        j2 = Journal(path)
        assert j2.conn.execute(
            "SELECT COUNT(*) FROM probability_log").fetchone()[0] == 1
