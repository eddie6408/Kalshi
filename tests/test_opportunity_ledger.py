"""Package 2: the opportunity journal.

Every evaluated opportunity - traded, rejected at any stage, or skipped before
it was priced - is a ledger row with its final decision, the stage that made
it, a link to whatever happened next, and eventually its outcome. These pin
each of those promises, plus the fail-closed rule and the write lock.
"""
import json
import re
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kalshibot import ledger
from kalshibot.gate import ProfitabilityGate
from kalshibot.journal import Journal, pnl_summary
from kalshibot.models import Market
from kalshibot.opportunity import DecisionStage, Opportunity
from kalshibot.strategies.arbitrage import NearMiss
from kalshibot.strategies.forecast import ForecastStrategy

import test_forecast as tf
import test_arbitrage as ta
from test_execution import FakeExec, FakeMarket, deep, opportunity
from kalshibot.execution_manager import ExecutionManager

ROOT = Path(__file__).resolve().parents[1]


def _j(tmp):
    return Journal(Path(tmp) / "j.db")


def _rows(j, sql, *a):
    return [dict(r) for r in j.conn.execute(sql, a).fetchall()]


# ------------------------------------------------------------ schema
def test_an_old_journal_gains_the_ledger_additively():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "old.db"
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE executions (id INTEGER PRIMARY KEY, ts TEXT, "
                  "opportunity_id INTEGER, strategy TEXT)")
        c.execute("CREATE TABLE leg_orders (id INTEGER PRIMARY KEY, ts TEXT, "
                  "ticker TEXT, side TEXT, action TEXT, filled INTEGER)")
        c.execute("INSERT INTO leg_orders (ts, ticker, side, action, filled) "
                  "VALUES ('t', 'OLD', 'yes', 'buy', 3)")
        c.commit()
        c.close()
        j = Journal(path)
        Journal(path)                                   # idempotent
        cols = {r[1] for r in j.conn.execute("PRAGMA table_info(leg_orders)")}
        assert "opportunity_uuid" in cols
        assert {r[1] for r in j.conn.execute("PRAGMA table_info(executions)")} >= {
            "opportunity_uuid"}
        assert _rows(j, "SELECT ticker FROM leg_orders") == [{"ticker": "OLD"}]
        assert _rows(j, "SELECT COUNT(*) n FROM opportunity_ledger") == [{"n": 0}]


# ------------------------------------------------------ skip reasons
def test_every_skip_reason_has_a_stage():
    """A new reason added to ForecastStrategy without a stage fails here."""
    src = (ROOT / "kalshibot" / "strategies" / "forecast.py").read_text()
    reasons = set(re.findall(r'self\._skip\(m, "([^"]+)"', src))
    gates = {g.lower().replace("_", " ") for g in re.findall(
        r'fire\("([A-Z_]+)"', (ROOT / "kalshibot" / "data_gates.py").read_text())}
    missing = sorted(r for r in reasons | gates if ledger.stage_for(r) is None)
    assert not missing, missing
    assert len(reasons) >= 18


def test_a_forecast_skip_is_recorded_with_what_was_known_once_an_hour():
    with tempfile.TemporaryDirectory() as tmp:
        j = _j(tmp)
        s = ForecastStrategy(tf.FakeEstimator(0.72, 0.30), tf.FEES,
                             tf.cfg(min_confidence=0.5), journal=j)
        s.execution_mode = "DEMO_LIVE"
        assert s.find([tf.mkt()], {"KXFAKE-1": tf.book(61)}, 500.0) == []
        s.find([tf.mkt()], {"KXFAKE-1": tf.book(61)}, 500.0)     # same hour
        rows = _rows(j, "SELECT * FROM opportunity_ledger")
        assert len(rows) == 1
        r = rows[0]
        assert (r["decision"], r["decision_stage"], r["rejection_reason"]) == \
            ("REJECTED", "MODEL", "low confidence")
        assert abs(r["probability_yes"] - 0.72) < 1e-9
        touch = json.loads(r["touch_json"])
        assert touch["yes_ask"] == 61 and touch["no_ask"] is not None
        assert r["execution_mode"] == "DEMO_LIVE" and r["code_version"]
        states = [e["state"] for e in _rows(j, "SELECT state FROM opportunity_events")]
        assert states == ["DETECTED", "REJECTED"]


def test_a_skip_never_blocks_anything_even_if_the_ledger_fails():
    class Broken:
        def ledger_record(self, *a, **k):
            raise sqlite3.OperationalError("disk I/O error")

        def log_probability(self, **k):
            pass

        def log_forecast(self, **k):
            pass
    s = ForecastStrategy(tf.FakeEstimator(0.72, 0.30), tf.FEES, tf.cfg(), journal=Broken())
    assert s.find([tf.mkt()], {"KXFAKE-1": tf.book(61)}, 500.0) == []


# --------------------------------------------- final decision, fail closed
class _Settings:
    def __init__(self, **kw):
        self.kw = kw

    def get(self, key, default=None):
        return self.kw.get(key, default)


def _engine(journal, risk_ok=True, **settings):
    from kalshibot.arb_engine import ArbitrageEngine, TradingMode
    e = ArbitrageEngine.__new__(ArbitrageEngine)
    e.gate, e.journal, e.mode = ProfitabilityGate({}), journal, TradingMode.DEMO_LIVE
    e.s = _Settings(**settings)
    e._risk_ok = lambda op: (True, "ok") if risk_ok else (False, "exposure cap")
    return e


def _arb():
    b = ta.book("T", yes_ask=40, yes_ask_qty=100, no_ask=52, no_ask_qty=100)
    from kalshibot.strategies.arbitrage import StructuralArbitrageScanner
    return StructuralArbitrageScanner(ta.FEES, {"max_contracts_per_trade": 500}).scan_event(
        "EV", [ta.mkt("T")], {"T": b})[0]


def test_the_ledger_holds_the_final_decision_not_the_gate_verdict():
    from kalshibot.arb_engine import ScanReport
    with tempfile.TemporaryDirectory() as tmp:
        j = _j(tmp)
        op = _arb()
        _engine(j, risk_ok=False)._judge(op, ScanReport())
        r = _rows(j, "SELECT * FROM opportunity_ledger WHERE opportunity_id=?",
                  op.opportunity_id)[0]
        assert (r["decision"], r["decision_stage"], r["rejection_reason"]) == \
            ("REJECTED", "RISK", "exposure cap")
        # the legacy table still says what it always said
        assert _rows(j, "SELECT decision FROM opportunities") == [{"decision": "TRADE"}]
        ok = _arb()
        report = ScanReport()
        _engine(j)._judge(ok, report)
        assert report.accepted == [ok]
        assert _rows(j, "SELECT decision FROM opportunity_ledger WHERE opportunity_id=?",
                     ok.opportunity_id) == [{"decision": "ACCEPTED"}]


class _LedgerDown(Journal):
    def ledger_record(self, *a, **k):
        raise sqlite3.OperationalError("database is locked")


def test_fail_closed_an_unrecordable_opportunity_is_not_traded():
    from kalshibot.arb_engine import ScanReport
    with tempfile.TemporaryDirectory() as tmp:
        j = _LedgerDown(Path(tmp) / "j.db")
        op, report = _arb(), ScanReport()
        _engine(j, **{"journal.fail_closed": True})._judge(op, report)
        assert report.accepted == []
        assert op.decision == "REJECTED" and "LEDGER_WRITE_FAILED" in op.rejection_reason
        op2, report2 = _arb(), ScanReport()
        _engine(j, **{"journal.fail_closed": False})._judge(op2, report2)
        assert report2.accepted == [op2]


# --------------------------------------------------------------- the chain
def test_opportunity_execution_exit_and_settlement_share_one_id():
    with tempfile.TemporaryDirectory() as tmp:
        j = _j(tmp)
        op = opportunity(("A", "B"), sides=("yes",))
        j.ledger_record(op)
        m = ExecutionManager(FakeExec(), None, FakeMarket({"A": deep("A"), "B": deep("B")}),
                             ProfitabilityGate({}), ta.FEES, j,
                             {"max_partial_fill_time_seconds": 0.2,
                              "fill_poll_seconds": 0.01, "max_unhedged_exposure_usd": 1000})
        m.execute(op)
        uid = op.opportunity_id
        assert _rows(j, "SELECT opportunity_uuid u FROM executions") == [{"u": uid}]
        assert {r["u"] for r in _rows(j, "SELECT opportunity_uuid u FROM leg_orders")} == {uid}
        j.log_leg_fill(ticker="A", side="yes", action="sell", filled=op.quantity,
                       price_cents=45.0, note="take profit")
        j.log_settlement(ticker="B", side="yes", contracts=op.quantity, won=True)
        assert {r["u"] for r in _rows(j, "SELECT opportunity_uuid u FROM leg_orders")} == {uid}
        states = [e["state"] for e in _rows(
            j, "SELECT state FROM opportunity_events WHERE opportunity_id=? ORDER BY id", uid)]
        assert states[:1] == ["DETECTED"] and "SUBMITTED" in states
        assert "FILLED" in states and states[-2:] == ["EXITED", "SETTLED"]
        pnl, closed = j.realised_for(uid)
        assert closed and abs(pnl - pnl_summary(j.conn)["realised"]) < 1e-9


# ------------------------------------------------------ hypothetical P&L
def _stub_row(j, q, yes_ask, no_ask, ticker="W-1"):
    op = Opportunity.stub(strategy_type="forecast:w", event_id="E", ticker=ticker,
                          stage=DecisionStage.MODEL, reason="low confidence",
                          probability_yes=q,
                          touch={"yes_ask": yes_ask, "no_ask": no_ask},
                          settlement_time=datetime.now(timezone.utc) - timedelta(hours=2))
    j.ledger_record(op)
    return _rows(j, "SELECT * FROM opportunity_ledger WHERE opportunity_id=?",
                 op.opportunity_id)[0]


def test_the_hypothetical_takes_the_side_the_model_preferred_at_the_touch():
    with tempfile.TemporaryDirectory() as tmp:
        j = _j(tmp)
        row = _stub_row(j, 0.70, 60, 45)             # YES: +0.10 vs NO: -0.15
        won, basis = ledger.hypothetical_pnl(row, {"W-1": True}, ta.FEES)
        lost, _ = ledger.hypothetical_pnl(row, {"W-1": False}, ta.FEES)
        fee = ta.FEES.entry_fee_usd(60, 1)
        assert abs(won - (1 - 0.60 - fee)) < 1e-9 and abs(lost - (-0.60 - fee)) < 1e-9
        assert basis.startswith("SIMULATED") and "YES @ 60c" in basis


def test_no_price_or_no_probability_means_no_hypothetical():
    with tempfile.TemporaryDirectory() as tmp:
        j = _j(tmp)
        assert ledger.hypothetical_pnl(_stub_row(j, None, 60, 45, "W-2"),
                                       {"W-2": True}, ta.FEES)[0] is None
        assert ledger.hypothetical_pnl(_stub_row(j, 0.5, None, None, "W-3"),
                                       {"W-3": True}, ta.FEES)[0] is None


def test_a_priced_opportunity_is_valued_as_one_set():
    with tempfile.TemporaryDirectory() as tmp:
        j = _j(tmp)
        op = _arb()                                   # YES 40 + NO 52 on one market
        op.decision, op.decision_stage, op.rejection_reason = "REJECTED", "RISK", "cap"
        j.ledger_record(op)
        row = _rows(j, "SELECT * FROM opportunity_ledger")[0]
        pnl, _ = ledger.hypothetical_pnl(row, {"T": True}, ta.FEES)
        expected = 1.0 - 0.40 - 0.52 - ta.FEES.entry_fee_usd(40, 1) - ta.FEES.entry_fee_usd(52, 1)
        assert abs(pnl - expected) < 1e-9


# --------------------------------------------------------- outcome pass
class _Markets:
    def __init__(self, results):
        self.results = results

    def get_market(self, ticker):
        r = self.results.get(ticker)
        raw = {"status": "settled", "result": r} if r else {"status": "active"}
        return Market(ticker=ticker, raw=raw, status=raw["status"])


def test_outcomes_are_filled_once_markets_settle():
    with tempfile.TemporaryDirectory() as tmp:
        j = _j(tmp)
        _stub_row(j, 0.70, 60, 45, "W-1")
        _stub_row(j, 0.70, 60, 45, "W-2")
        stats = ledger.resolve_outcomes(j, _Markets({"W-1": "yes"}), ta.FEES)
        assert stats["resolved"] == 1 and stats["pending"] == 1
        r = _rows(j, "SELECT * FROM opportunity_ledger WHERE markets='W-1'")[0]
        assert json.loads(r["outcome_json"]) == {"W-1": "YES"}
        assert r["hypothetical_pnl"] > 0 and r["realised_pnl"] is None
        assert r["hypothetical_basis"].startswith("SIMULATED")


def test_old_rejected_records_are_pruned_but_their_columns_kept():
    with tempfile.TemporaryDirectory() as tmp:
        j = _j(tmp)
        row = _stub_row(j, 0.7, 60, 45)
        j.conn.execute("UPDATE opportunity_ledger SET detected_at='2020-01-01T00:00:00+00:00'")
        j.conn.commit()
        assert j.prune_ledger_records(90) == 1
        r = _rows(j, "SELECT * FROM opportunity_ledger")[0]
        assert r["record_json"] is None and r["rejection_reason"] == row["rejection_reason"]


# ------------------------------------------------------------ near misses
def test_near_misses_are_recorded_with_their_known_value_and_capped():
    with tempfile.TemporaryDirectory() as tmp:
        j = _j(tmp)
        e = _engine(j, **{"journal.near_miss_record_limit": 2})
        nms = [NearMiss("ladder", "EV", [f"L{i}", f"H{i}"], 104 + i, 100) for i in range(5)]
        e._ledger_near_misses(nms)
        rows = _rows(j, "SELECT * FROM opportunity_ledger ORDER BY hypothetical_pnl DESC")
        assert len(rows) == 2
        assert rows[0]["decision_stage"] == "MODEL"
        assert rows[0]["rejection_reason"] == "NO_STRUCTURAL_EDGE"
        assert abs(rows[0]["hypothetical_pnl"] - (-0.04)) < 1e-9
        assert rows[0]["markets"] == "L0,H0"


# -------------------------------------------------------------- the lock
def test_concurrent_writers_do_not_interleave():
    with tempfile.TemporaryDirectory() as tmp:
        j = _j(tmp)
        errors = []

        def ledger_writer():
            try:
                for i in range(60):
                    _stub_row(j, 0.5, 50, 50, f"T{threading.get_ident()}-{i}")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def forecast_writer():
            try:
                for i in range(60):
                    j.log_forecast(series="S", station="K", target_date=f"d{i}",
                                   mean_f=70, sigma_f=1.6, members=10, lead_hours=5)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        ts = [threading.Thread(target=ledger_writer) for _ in range(3)] + \
             [threading.Thread(target=forecast_writer) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert not errors
        assert _rows(j, "SELECT COUNT(*) n FROM opportunity_ledger") == [{"n": 180}]
        assert _rows(j, "SELECT COUNT(*) n FROM forecasts") == [{"n": 120}]
        assert _rows(j, "SELECT COUNT(*) n FROM opportunity_events") == [{"n": 360}]


# ------------------------------------------------------------ reporting
def test_the_watch_screen_and_the_report_show_the_flow():
    import io
    from contextlib import redirect_stdout
    sys.path.insert(0, str(ROOT / "scripts"))
    import monitor
    import opportunity_report
    with tempfile.TemporaryDirectory() as tmp:
        j = _j(tmp)
        _stub_row(j, 0.7, 60, 45)
        j.ledger_record(_arb())
        conn = sqlite3.connect(f"file:{Path(tmp) / 'j.db'}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        frame = monitor.render(conn, Path(tmp) / "j.db", 5)
        assert "OPPORTUNITY FLOW" in frame and "MODEL 1" in frame
        out = io.StringIO()
        with redirect_stdout(out):
            sys.argv = ["opportunity_report.py", "--db", str(Path(tmp) / "j.db")]
            assert opportunity_report.main() == 0
        assert "low confidence" in out.getvalue() and "SIMULATED" in out.getvalue()
