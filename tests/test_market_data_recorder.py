"""Package 3a: the market-data recorder, the production watch, and the
second (production) market-skill benchmark.

The promises pinned here: records come back exactly as written and in order;
the recorder never blocks or breaks the caller, and stops itself - not the
bot - when the disk runs low; production is read only through the public,
allowlisted reader, on the watch's own budget, never from the trading path;
the gate still decides on demo prices.
"""
import gzip
import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["KALSHIBOT_ALLOW_LOOPBACK_FOR_TESTS"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest
from kalshibot.depth import DepthBook, Level
from kalshibot.env_guard import ProductionBlocked, assert_public_market_data
from kalshibot.journal import Journal
from kalshibot.livebooks import LiveBookReader
from kalshibot.market_skill import (MarketSkillConfig, MarketSkillGate,
                                    compare_benchmarks, compute_cells, fetch_rows)
from kalshibot.market_state import MarketState
from kalshibot.prodwatch import (ProductionWatch, Quote, RateLimiter, WatchConfig,
                                 parse_trade)
from kalshibot.recorder import Recorder, RecorderConfig, book_levels, iter_ticks
from kalshibot.strategies.forecast import ForecastStrategy
from kalshibot.wsfeed import FeedConfig, KalshiFeed

from test_forecast import FEES, FakeEstimator, book, cfg, mkt
from test_wsfeed import delta, snapshot

T0 = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc).timestamp()


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def _rec(tmp, clock=None, **kw):
    return Recorder(Path(tmp) / "ticks", RecorderConfig.from_dict(kw), clock=clock or Clock())


# ------------------------------------------------------------------ recorder
def test_records_come_back_in_order_across_many_batches():
    with tempfile.TemporaryDirectory() as tmp:
        r = _rec(tmp)
        for i in range(5):
            r.record("prod_book", {"i": i})
            r.flush()                       # one gzip member per flush
        r.record("prod_book", {"i": 5})
        r.record("prod_trades", {"i": "t"})
        r.stop()
        got = [x["i"] for x in iter_ticks(Path(tmp) / "ticks", "2026-09-28", "prod_book")]
        assert got == [0, 1, 2, 3, 4, 5]
        assert [x["i"] for x in iter_ticks(Path(tmp) / "ticks", "2026-09-28",
                                           "prod_trades")] == ["t"]
        # our receive time is stamped on every record
        first = next(iter_ticks(Path(tmp) / "ticks", "2026-09-28", "prod_book"))
        assert first["rt"] == pytest.approx(T0)


def test_records_are_filed_under_their_own_utc_day():
    with tempfile.TemporaryDirectory() as tmp:
        clock = Clock(T0)
        r = _rec(tmp, clock)
        r.record("demo_book", {"d": 1})
        clock.t = T0 + 86400
        r.record("demo_book", {"d": 2})
        r.flush()
        root = Path(tmp) / "ticks"
        assert [x["d"] for x in iter_ticks(root, "2026-09-28", "demo_book")] == [1]
        assert [x["d"] for x in iter_ticks(root, "2026-09-29", "demo_book")] == [2]


def test_a_full_queue_drops_and_counts_but_never_blocks_or_raises():
    with tempfile.TemporaryDirectory() as tmp:
        r = _rec(tmp, queue_size=3)
        start = time.monotonic()
        for i in range(10):
            r.record("prod_book", {"i": i})
        assert time.monotonic() - start < 0.5
        assert r.dropped == 7
        assert r.flush() == 3


def test_demo_feed_recording_can_be_switched_off_on_its_own():
    with tempfile.TemporaryDirectory() as tmp:
        r = _rec(tmp, demo_ws=False)
        r.record("demo_ws", {"x": 1})
        r.record("prod_book", {"x": 2})
        assert r.flush() == 1


def test_low_disk_stops_recording_not_the_caller_and_alerts_once():
    with tempfile.TemporaryDirectory() as tmp:
        alerts = []
        r = Recorder(Path(tmp) / "ticks", RecorderConfig(min_free_gb=10 ** 9),
                     clock=Clock(), alert=alerts.append)
        r.record("prod_book", {"i": 1})
        assert r.flush() == 0
        assert r.paused_for_disk and len(alerts) == 1
        r.record("prod_book", {"i": 2})     # a no-op now, and still no exception
        r.flush()
        assert len(alerts) == 1
        assert not (Path(tmp) / "ticks" / "2026-09-28").exists()


def test_prune_removes_only_days_past_retention():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "ticks"
        for d in ("2026-06-01", "2026-06-30", "2026-07-01", "2026-09-27"):
            (root / d).mkdir(parents=True)
        r = Recorder(root, RecorderConfig(retention_days=90), clock=Clock())
        assert r.prune() == 1                     # cutoff is 2026-06-30
        assert sorted(p.name for p in root.iterdir()) == ["2026-06-30", "2026-07-01",
                                                          "2026-09-27"]


def test_a_batch_cut_short_by_a_crash_does_not_lose_what_came_before():
    with tempfile.TemporaryDirectory() as tmp:
        r = _rec(tmp)
        r.record("prod_book", {"i": 1})
        r.flush()
        path = Path(tmp) / "ticks" / "2026-09-28" / "prod_book.jsonl.gz"
        whole = gzip.compress(b'{"i": 2}\n')
        with open(path, "ab") as fh:
            fh.write(whole[: len(whole) // 2])    # a member truncated mid-write
        got = list(iter_ticks(Path(tmp) / "ticks", "2026-09-28", "prod_book"))
        assert got and got[0]["i"] == 1


def test_book_levels_are_the_two_bid_ladders():
    b = DepthBook("T", [Level(40, 5), Level(41, 2)], [Level(55, 7)])
    assert book_levels(b) == {"yes": [[41, 2], [40, 5]], "no": [[55, 7]]}
    assert book_levels(None) == {"yes": [], "no": []}


def test_the_demo_feed_records_what_it_applies():
    with tempfile.TemporaryDirectory() as tmp:
        r = _rec(tmp)
        f = KalshiFeed(MarketState(None), cfg=FeedConfig.from_dict({"enabled": True}))
        f.recorder = r
        f._handle(snapshot(ticker="T", seq=1))
        f._handle(delta(ticker="T", seq=2))
        r.flush()
        got = list(iter_ticks(Path(tmp) / "ticks", "2026-09-28", "demo_ws"))
        assert [g["k"] for g in got] == ["s", "d"]
        assert got[0]["yes"] == [[60, 100]] and got[0]["seq"] == 1
        assert got[1]["p"] == 60 and got[1]["q"] == -54 and got[1]["ok"] is True


# ------------------------------------------------------------ production watch
def test_rate_limiter_spaces_requests():
    t = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        t[0] += s
    rl = RateLimiter(2.0, clock=lambda: t[0], sleep=sleep)
    for _ in range(3):
        rl.wait()
    assert slept == [pytest.approx(0.5), pytest.approx(0.5)]


def test_trade_prints_parse_in_both_spellings():
    a = parse_trade({"trade_id": "x", "created_time": "2026-09-28T12:00:01.5Z",
                     "yes_price_dollars": "0.1700", "no_price_dollars": "0.8300",
                     "count_fp": "50.00", "taker_side": "yes"})
    assert (a.yes_cents, a.no_cents, a.count, a.taker_side) == (17, 83, 50, "yes")
    assert a.ts == pytest.approx(T0 + 1.5)
    b = parse_trade({"trade_id": "y", "created_time": "2026-09-28T12:00:00Z",
                     "yes_price": 30, "count": 4, "taker_side": "no"})
    assert (b.yes_cents, b.no_cents, b.count) == (30, 70, 4)
    assert parse_trade({"created_time": "2026-09-28T12:00:00Z", "count": 0,
                        "taker_side": "yes", "yes_price": 30}) is None
    assert parse_trade({"created_time": "bad", "count": 1, "taker_side": "yes"}) is None


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


class FakeReader:
    def __init__(self):
        self.books = {}
        self.prints = {}
        self.calls = []
        self.fail_trades = None

    def orderbook(self, ticker, depth=50):
        self.calls.append(("book", ticker))
        return self.books.get(ticker)

    def trades(self, ticker, min_ts=None, limit=1000, max_pages=5):
        self.calls.append(("trades", ticker, min_ts))
        if self.fail_trades:
            raise self.fail_trades
        return [p for p in self.prints.get(ticker, [])
                if min_ts is None or _ts_of(p) >= min_ts]


def _ts_of(p):
    return datetime.fromisoformat(p["created_time"].replace("Z", "+00:00")).timestamp()


def _print(tid, ts, yes, n=5, taker="no"):
    return {"trade_id": tid, "created_time": _iso(ts), "yes_price_dollars": f"{yes / 100:.4f}",
            "count_fp": f"{n}.00", "taker_side": taker}


class Listener:
    def __init__(self):
        self.books, self.trades = [], []

    def on_book(self, ticker, quote, info):
        self.books.append((ticker, quote, info))

    def on_trades(self, ticker, trades, complete_until):
        self.trades.append((ticker, [t.trade_id for t in trades], complete_until))


def _watch(reader, clock, recorder=None, **kw):
    cfg = WatchConfig.from_dict({"max_requests_per_second": 0, **kw})
    return ProductionWatch(reader, recorder, cfg, clock=clock,
                           limiter=RateLimiter(0, sleep=lambda s: None))


def test_the_watch_reads_books_and_prints_on_its_own_clock():
    clock = Clock()
    reader = FakeReader()
    reader.books["W-1"] = DepthBook("W-1", [Level(40, 10)], [Level(55, 10)])
    reader.prints["W-1"] = [_print("a", T0 - 10, 41)]
    with tempfile.TemporaryDirectory() as tmp:
        rec = _rec(tmp, clock)
        w = _watch(reader, clock, rec)
        li = Listener()
        w.listeners.append(li)
        w.watch("W-1", {"q": 0.6})
        w.run_pass()
        q = w.latest("W-1")
        assert (q.yes_bid, q.yes_ask) == (40, 45)
        assert li.books[0][2]["q"] == 0.6
        assert li.trades[0][1] == ["a"]
        # nothing is due again until the intervals pass
        n = len(reader.calls)
        clock.t += 60
        w.run_pass()
        assert len(reader.calls) == n
        # the same print served again is not counted twice
        reader.prints["W-1"].append(_print("b", T0 + 200, 42))
        clock.t = T0 + 301
        w.run_pass()
        assert li.trades[-1][1] == ["b"]
        rec.flush()
        assert len(list(iter_ticks(Path(tmp) / "ticks", "2026-09-28", "prod_book"))) == 2
        assert [x["id"] for x in iter_ticks(Path(tmp) / "ticks", "2026-09-28",
                                            "prod_trades")] == ["a", "b"]


def test_latest_respects_max_age_and_markets_are_forgotten():
    clock = Clock()
    reader = FakeReader()
    reader.books["W-1"] = DepthBook("W-1", [Level(40, 10)], [Level(55, 10)])
    w = _watch(reader, clock)
    w.watch("W-1")
    w.run_pass()
    clock.t += 301
    assert w.latest("W-1", max_age=300) is None
    assert w.latest("W-1") is not None
    clock.t += 31 * 60
    w.run_pass()
    assert w.watched() == [] and w.latest("W-1") is None


def test_mid_at_uses_the_nearest_read():
    clock = Clock()
    reader = FakeReader()
    w = _watch(reader, clock, book_interval_seconds=0)
    w.watch("W-1")
    for i, bid in enumerate((40, 50, 60)):
        clock.t = T0 + i * 120
        reader.books["W-1"] = DepthBook("W-1", [Level(bid, 1)], [Level(100 - bid - 4, 1)])
        w.run_pass()
    assert w.mid_at("W-1", T0 + 130) == pytest.approx(52)
    assert w.mid_at("W-1", T0 + 130, side="no") == pytest.approx(48)
    assert w.mid_at("W-1", T0 + 10_000) is None


def test_a_429_backs_off_and_ends_the_pass():
    clock = Clock()
    reader = FakeReader()

    class Resp:
        status_code = 429
    err = Exception("429")
    err.response = Resp()
    reader.fail_trades = err
    w = _watch(reader, clock)
    for t in ("A", "B"):
        w.watch(t)
    w.run_pass()
    assert sum(1 for c in reader.calls if c[0] == "trades") == 1
    n = len(reader.calls)
    clock.t += 30
    w.run_pass()
    assert len(reader.calls) == n            # still backing off


def test_the_trade_tape_is_public_market_data_and_accounts_are_not():
    assert_public_market_data("https://external-api.kalshi.com/trade-api/v2",
                              "/markets/trades", "GET")
    for path in ("/portfolio/orders", "/portfolio/fills", "/markets/../portfolio/balance"):
        with pytest.raises(ProductionBlocked):
            assert_public_market_data("https://external-api.kalshi.com/trade-api/v2",
                                      path, "GET")
    with pytest.raises(ProductionBlocked):
        assert_public_market_data("https://external-api.kalshi.com/trade-api/v2",
                                  "/markets/trades", "POST")
    # and the reader still has nothing that could send an order
    for name in ("place_order", "place_demo_order", "create_order", "cancel_order"):
        assert not hasattr(LiveBookReader, name)


# ------------------------------------------------- the forecast strategy's side
class StubWatch:
    def __init__(self, quote=None):
        self.quote = quote
        self.watched = []

    def watch(self, ticker, info=None):
        self.watched.append((ticker, info))

    def latest(self, ticker, max_age=None):
        return self.quote


class StrikeEstimator(FakeEstimator):
    """Describes its strikes, so the pass caches each estimate - which is
    what the live weather estimator does, and what _record logs from."""

    def parse_strike(self, market):
        return None, None, ""


def _strategy(tmp, watch=None, recorder=None, probability=0.72):
    j = Journal(Path(tmp) / "j.db")
    s = ForecastStrategy(StrikeEstimator(probability=probability), FEES, cfg(), journal=j)
    s.production_watch = watch
    s.recorder = recorder
    return s, j


def test_forecasts_are_logged_with_the_production_price_beside_the_demo_one():
    with tempfile.TemporaryDirectory() as tmp:
        q = Quote("KXFAKE-1", None, T0, yes_bid=52, yes_ask=54)
        rec = _rec(tmp, Clock(time.time()))
        s, j = _strategy(tmp, StubWatch(q), rec)
        ops = s.find([mkt()], {"KXFAKE-1": book()}, 500.0)
        row = j.conn.execute("SELECT * FROM probability_log").fetchone()
        assert (row["market_yes_cents"], row["prod_yes_ask_cents"],
                row["prod_yes_bid_cents"]) == (61, 54, 52)
        assert row["prod_read_at"].startswith("2026-09-28T12:00")
        # the market was handed to the watch with our verdict
        (ticker, info), = s.production_watch.watched
        assert ticker == "KXFAKE-1" and info["q"] == 0.72
        assert info["stage"] == ("ACCEPTED" if ops else info["stage"])
        rec.flush()
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert [x["m"] for x in iter_ticks(Path(tmp) / "ticks", day, "demo_book")] == ["KXFAKE-1"]


def test_a_skipped_market_carries_its_stage_to_the_watch():
    with tempfile.TemporaryDirectory() as tmp:
        s, _ = _strategy(tmp, StubWatch(), probability=0.60)   # market agrees: no edge
        assert s.find([mkt()], {"KXFAKE-1": book()}, 500.0) == []
        (_, info), = s.production_watch.watched
        assert info["stage"] == "MODEL" and info["reason"]


def test_without_a_watch_nothing_changes():
    with tempfile.TemporaryDirectory() as tmp:
        s, j = _strategy(tmp)
        s.find([mkt()], {"KXFAKE-1": book()}, 500.0)
        row = j.conn.execute("SELECT * FROM probability_log").fetchone()
        assert row["prod_yes_ask_cents"] is None and row["market_yes_cents"] == 61


# ------------------------------------------------------ the second benchmark
def _settled(j, ticker, ours, demo, prod, outcome, lead=3.0):
    j.conn.execute(
        """INSERT INTO probability_log (ts, strategy, estimator, series, ticker,
           our_probability, market_yes_cents, market_yes_bid_cents,
           prod_yes_ask_cents, prod_yes_bid_cents, lead_hours, settled_yes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"2026-09-28T0{len(ticker) % 9}:{ticker[-2:]}:00", "f", "weather_temperature",
         "KXHIGHNY", ticker, ours, demo, demo, prod, prod, lead, outcome))
    j.conn.commit()


def test_the_production_benchmark_is_reported_and_the_gate_stays_on_demo():
    with tempfile.TemporaryDirectory() as tmp:
        j = Journal(Path(tmp) / "j.db")
        # demo price is terrible (50c on things that resolve NO); production
        # is sharp (5c). Our 20% sits between them.
        for i in range(20):
            _settled(j, f"KXHIGHNY-{i:02d}", 0.20, 50, 5, 0)
        cfg_ = MarketSkillConfig(min_contracts=15)
        demo = compute_cells(fetch_rows(j.conn, "weather_temperature"), cfg_)
        prod = compute_cells(fetch_rows(j.conn, "weather_temperature",
                                        benchmark="production"), cfg_)
        assert demo[("KXHIGHNY", "0-6h")].verdict == "AHEAD"
        assert prod[("KXHIGHNY", "0-6h")].verdict == "BEHIND"
        both = compare_benchmarks(j.conn, "weather_temperature", cfg=cfg_)
        assert set(both) == {"demo", "production"}
        gate = MarketSkillGate(j, cfg_, estimator="weather_temperature")
        assert gate.may_enter("KXHIGHNY", 3.0)[0] is True      # decided on demo


def test_rows_without_a_production_price_are_left_out_of_that_benchmark_only():
    with tempfile.TemporaryDirectory() as tmp:
        j = Journal(Path(tmp) / "j.db")
        _settled(j, "KXHIGHNY-01", 0.2, 30, None, 0)
        assert len(fetch_rows(j.conn, "weather_temperature")) == 1
        assert fetch_rows(j.conn, "weather_temperature", benchmark="production") == []


# ------------------------------------------------------------- journal
def test_an_old_journal_gains_the_new_columns_and_keeps_its_rows():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "old.db"
        c = sqlite3.connect(db)
        c.executescript("""
            CREATE TABLE probability_log (id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL, strategy TEXT, estimator TEXT, series TEXT,
                ticker TEXT NOT NULL, target_date TEXT, strike TEXT,
                our_probability REAL NOT NULL, confidence REAL,
                market_yes_cents REAL, lead_hours REAL, traded INTEGER DEFAULT 0,
                settled_yes INTEGER, settled_at TEXT, brier REAL, UNIQUE (ticker, ts));
            CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_ts TEXT, ended_ts TEXT, mode TEXT, note TEXT);
            INSERT INTO probability_log (ts, ticker, our_probability) VALUES ('t', 'X', 0.5);
            INSERT INTO runs (started_ts, mode) VALUES ('t', 'DEMO_LIVE');
        """)
        c.commit()
        c.close()
        for _ in range(2):                        # idempotent
            j = Journal(db)
        cols = {r["name"] for r in j.conn.execute("PRAGMA table_info(probability_log)")}
        assert {"prod_yes_ask_cents", "prod_yes_bid_cents", "prod_read_at"} <= cols
        assert j.conn.execute("SELECT COUNT(*) FROM probability_log").fetchone()[0] == 1
        rid = j.start_run("DEMO_LIVE", "env=demo", code_version="abc123",
                          config_version="cfg-x", model_versions={"w": {"ensemble": "1"}},
                          fee_schedule_version="static")
        r = j.conn.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
        assert (r["code_version"], r["config_version"], r["fee_schedule_version"]) == \
            ("abc123", "cfg-x", "static")
        assert json.loads(r["model_versions"]) == {"w": {"ensemble": "1"}}
        assert j.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2


def test_the_shipped_config_records_and_watches_read_only():
    import yaml
    from kalshibot.config import _StrictLoader
    raw = yaml.load(open(Path(__file__).resolve().parents[1] / "config.yaml"),
                    Loader=_StrictLoader)
    assert RecorderConfig.from_dict(raw["recorder"]).retention_days == 90
    w = WatchConfig.from_dict(raw["production_watch"])
    assert 0 < w.max_requests_per_second <= 5
    # the market-skill gate still decides on the demo market
    assert "benchmark" not in (raw.get("market_skill") or {})
