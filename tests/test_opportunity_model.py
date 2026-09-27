"""Package 1: the standard Opportunity.

Two promises are pinned here. First, the model is richer - every opportunity
says what it is, what it was expected to be worth and how that number is
built, what is measured versus modelled versus unknown, and which versions
produced it. Second, NOTHING about trading changed: the same books produce the
same sizes, costs, fees, edges and gate decisions as before (p1_golden.json
was captured from the code before Package 1).
"""
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kalshibot import versioning
from kalshibot.gate import ProfitabilityGate
from kalshibot.opportunity import (ArbitrageOpportunity, Basis, Leg, Opportunity,
                                   OpportunityClass)
from kalshibot.strategies.arbitrage import StructuralArbitrageScanner

import p1_golden_cases
import test_arbitrage as ta
import test_forecast as tf

HERE = Path(__file__).resolve().parent


def _scanner():
    return StructuralArbitrageScanner(ta.FEES, {"max_contracts_per_trade": 500,
                                                "min_net_edge_per_contract": 0.001})


def _same_market():
    b = ta.book("T", yes_ask=40, yes_ask_qty=10, no_ask=52, no_ask_qty=100,
                extra_yes=((44, 50),), extra_no=((57, 60),))
    return _scanner().scan_event("EV", [ta.mkt("T")], {"T": b})[0]


def _forecast(evidence=None):
    class E(tf.FakeEstimator):
        def estimate(self, market):
            e = super().estimate(market)
            e.evidence = dict(evidence or {})
            return e
    s = tf.ForecastStrategy(E(0.72, 0.9), tf.FEES, tf.cfg())
    return s.find([tf.mkt()], {"KXFAKE-1": tf.book(61)}, 500.0)[0]


# --------------------------------------------------------- nothing changed
def test_trading_decisions_are_identical_to_before_package_1():
    golden = json.loads((HERE / "fixtures" / "p1_golden.json").read_text())
    assert p1_golden_cases.cases() == golden


def test_the_old_name_is_the_same_class():
    assert ArbitrageOpportunity is Opportunity


# ------------------------------------------------------------ structural
def test_a_structural_opportunity_says_what_it_is():
    op = _same_market()
    assert op.opportunity_class is OpportunityClass.STRUCTURAL_ARB
    assert op.direction == "basket of 2"
    assert op.model_version == "structural/same_market@1"
    assert op.fee_schedule_version.startswith("static-config:")
    assert op.source_quality_tier == 1 and op.data_quality == "OK"
    assert op.settlement_time == ta.SOON
    assert all(l.best_ask_cents is not None for l in op.legs)
    assert len(op.opportunity_id) == 32


def test_capital_and_worst_case_follow_from_the_numbers():
    op = _same_market()
    assert abs(op.capital_required - (op.gross_cost + op.estimated_fees)) < 1e-9
    assert abs(op.max_loss - max(0.0, op.capital_required - op.guaranteed_min_payout)) < 1e-9
    assert abs(op.max_profit - (op.max_payout - op.capital_required)) < 1e-9
    assert op.holding_period_hours is not None and op.holding_period_hours > 5


def test_the_ev_breakdown_reproduces_the_edge_the_gate_uses():
    op = _same_market()
    ev = op.ev_breakdown()
    assert abs(ev.net_ev - op.net_edge) < 1e-9
    assert ev.get("payout_ev").basis is Basis.MEASURED
    slip = ev.get("slippage")
    touch = sum(l.quantity * l.best_ask_cents for l in op.legs) / 100.0
    assert slip.basis is Basis.MEASURED
    assert abs(slip.usd - (op.gross_cost - touch)) < 1e-9 and slip.usd > 0   # walked deeper
    assert ev.get("fees").basis is Basis.MODELLED


def test_what_is_not_modelled_is_unknown_never_zero():
    ev = _same_market().ev_breakdown()
    for name in ("adverse_selection", "execution_loss"):
        c = ev.get(name)
        assert c.basis is Basis.NOT_MODELLED and c.usd is None
    assert not ev.complete
    assert {"adverse_selection", "execution_loss"} <= set(ev.unmodelled)


def test_a_safety_buffer_shows_up_as_a_risk_adjustment():
    s = StructuralArbitrageScanner(ta.FEES, {"max_contracts_per_trade": 500,
                                             "min_net_edge_per_contract": 0.001,
                                             "safety_buffer_usd": 0.10})
    b = ta.book("T", yes_ask=40, yes_ask_qty=100, no_ask=52, no_ask_qty=100)
    op = s.scan_event("EV", [ta.mkt("T")], {"T": b})[0]
    ev = op.ev_breakdown()
    assert abs(ev.get("risk_adjustment").usd - 0.10) < 1e-9
    assert abs(ev.net_ev - op.net_edge) < 1e-9


def test_the_spread_paid_is_measured_when_both_sides_are_quoted():
    b = ta.book("T", yes_ask=40, yes_ask_qty=100, no_ask=52, no_ask_qty=100)
    op = _scanner().scan_event("EV", [ta.mkt("T")], {"T": b})[0]
    sc = op.ev_breakdown().spread_cost
    # YES ask 40 / YES bid 48 (from the NO ask of 52): an inverted book, the
    # arbitrage itself. Whatever its sign, it is measured, not guessed.
    assert sc.basis is Basis.MEASURED and sc.usd is not None


# --------------------------------------------------------------- forecast
def test_a_forecast_opportunity_carries_both_probabilities_and_its_model():
    op = _forecast({"model_version": "weather_temperature/nowcast@1", "source_tier": 2})
    assert op.opportunity_class is OpportunityClass.FORECAST     # estimator "fake"
    assert op.direction == "buy yes"
    assert abs(op.fair_probability - 0.72) < 1e-9
    assert 0.6 < op.market_probability < 0.62
    assert op.model_version == "weather_temperature/nowcast@1"
    assert op.source_quality_tier == 2 and op.data_quality == "OK"
    assert op.settlement_time == tf.SOON


def test_a_forecast_ev_breakdown_reproduces_its_ev():
    op = _forecast()
    ev = op.ev_breakdown()
    assert abs(ev.net_ev - op.ev_usd) < 1e-9 and abs(ev.net_ev - op.net_edge) < 1e-9
    assert ev.get("payout_ev").basis is Basis.MODELLED
    assert op.max_loss == op.capital_required                   # no floor


def test_a_degraded_input_is_named():
    op = _forecast({"observation_suspect": "spike"})
    assert op.data_quality.startswith("DEGRADED") and "spike" in op.data_quality


def test_a_weather_estimator_is_classed_as_weather():
    class W(tf.FakeEstimator):
        name = "weather_temperature"
    s = tf.ForecastStrategy(W(0.72, 0.9), tf.FEES, tf.cfg())
    s.estimator.candidate_series = lambda: ["KXFAKE"]
    op = s.find([tf.mkt()], {"KXFAKE-1": tf.book(61)}, 500.0)[0]
    assert op.opportunity_class is OpportunityClass.WEATHER_FORECAST


# --------------------------------------------------------- legacy objects
def test_an_opportunity_without_recorded_touches_still_adds_up():
    op = Opportunity(strategy_type="x", event_id="E", market_tickers=["T"],
                     legs=[Leg("T", "yes", "buy", 10, 50, vwap_cents=50.0)],
                     quantity=10, gross_cost=5.0, guaranteed_min_payout=10.0,
                     gross_edge=5.0, estimated_fees=0.2, net_edge=4.8)
    ev = op.ev_breakdown()
    assert ev.get("slippage").basis is Basis.NOT_MODELLED
    assert abs(ev.net_ev - 4.8) < 1e-9


# ----------------------------------------------------------------- record
def test_the_record_is_complete_and_json_safe():
    rec = _same_market().to_record()
    back = json.loads(json.dumps(rec))
    for key in ("opportunity_id", "detected_at", "opportunity_class", "legs", "ev",
                "capital_required", "max_loss", "settlement_time", "model_version",
                "fee_schedule_version", "code_version", "config_version",
                "decision", "decision_stage", "market_price_source"):
        assert key in back, key
    assert back["ev"]["complete"] is False and "net_ev" in back["ev"]
    assert back["legs"][0]["best_ask_cents"] is not None


# -------------------------------------------------------------- versioning
def test_versions_are_recorded():
    op = _same_market()
    assert op.code_version and op.config_version.startswith("cfg-")
    assert op.code_version == versioning.code_version()


def test_the_config_version_follows_the_file_content():
    with tempfile.TemporaryDirectory() as tmp:
        a, b = Path(tmp) / "a.yaml", Path(tmp) / "b.yaml"
        a.write_text("x: 1\n")
        b.write_text("x: 2\n")
        assert versioning.config_version(str(a)) != versioning.config_version(str(b))
        assert versioning.config_version(str(Path(tmp) / "missing")) == versioning.UNKNOWN


# ---------------------------------------------------- the engine's verdict
class _Settings:
    def get(self, key, default=None):
        return default


def _engine(risk_ok=True):
    from kalshibot.arb_engine import ArbitrageEngine, TradingMode
    e = ArbitrageEngine.__new__(ArbitrageEngine)
    e.gate, e.journal, e.mode, e.s = ProfitabilityGate({}), None, TradingMode.DEMO_LIVE, _Settings()
    e._risk_ok = lambda op: (True, "ok") if risk_ok else (False, "exposure cap")
    return e


def test_the_engine_records_where_the_decision_was_made():
    from kalshibot.arb_engine import ScanReport
    op = _same_market()
    _engine(risk_ok=False)._judge(op, ScanReport())
    assert (op.decision, op.decision_stage, op.rejection_reason) == \
        ("REJECTED", "RISK", "exposure cap")
    assert op.execution_mode == "DEMO_LIVE" and op.market_price_source == "demo"

    op2 = _same_market()
    _engine()._judge(op2, ScanReport())
    assert (op2.decision, op2.decision_stage) == ("ACCEPTED", "RISK")

    tiny = _same_market()
    tiny.net_edge = -1.0                                     # the gate refuses this
    _engine()._judge(tiny, ScanReport())
    assert (tiny.decision, tiny.decision_stage) == ("REJECTED", "GATE")
    assert tiny.rejection_reason
