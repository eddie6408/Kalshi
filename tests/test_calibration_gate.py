"""The gate that turns a Brier score into a permission.

The dangerous failures here are asymmetric, so they are tested asymmetrically:

  * Letting a bad strategy keep trading costs money slowly.
  * Blocking an EXIT traps a losing position and costs money fast.
  * Suspending on three unlucky forecasts looks like a broken bot.

So: suspension must be hard to trigger, and exits must be impossible to block.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.calibration_gate import (CalibrationConfig, CalibrationGate,
                                        CalibrationState, climatology_brier)


class FakeJournal:
    """Stands in for the probability_log. `stats` is what calibration() returns."""

    def __init__(self, stats=None, explode=False):
        self.stats = stats or {"n": 0, "brier": None,
                               "mean_forecast": None, "base_rate": None}
        self.explode = explode
        self.reads = 0
        self.last_kwargs: dict = {}

    def calibration(self, estimator=None, series=None, max_lead_hours=None,
                    since=None):
        self.reads += 1
        self.last_kwargs = {"estimator": estimator, "series": series,
                            "max_lead_hours": max_lead_hours, "since": since}
        if self.explode:
            raise RuntimeError("database is locked")
        return self.stats


def _gate(stats=None, explode=False, **cfg):
    j = FakeJournal(stats, explode)
    return CalibrationGate(j, CalibrationConfig(**cfg)), j


# --------------------------- the benchmark ---------------------------
def test_climatology_is_the_benchmark_not_a_round_number():
    # A coin flip is genuinely hard: 0.25 is the best a base-rate forecaster does.
    assert climatology_brier(0.5) == 0.25
    # A rare event is easy to score well on by saying "no" every time, so the
    # bar for claiming skill there is much higher.
    assert climatology_brier(0.05) < 0.05
    assert climatology_brier(None) == 0.25


def test_climatology_clamps_nonsense_base_rates():
    assert climatology_brier(1.7) == 0.0
    assert climatology_brier(-0.3) == 0.0


# --------------------------- small samples ---------------------------
def test_nothing_is_judged_below_min_sample():
    # A perfect score over 5 forecasts is luck, not evidence.
    gate, _ = _gate({"n": 5, "brier": 0.01, "mean_forecast": 0.5,
                     "base_rate": 0.5}, min_sample=30)
    v = gate.verdict("weather_temperature")
    assert v.state is CalibrationState.UNPROVEN
    assert "need 30" in v.detail


def test_a_terrible_score_below_min_sample_still_may_not_suspend():
    # This is the noise case. Three bad days must not stop the bot.
    gate, _ = _gate({"n": 3, "brier": 0.9, "mean_forecast": 0.5,
                     "base_rate": 0.5}, min_sample=30)
    ok, _ = gate.may_enter("weather_temperature")
    assert ok is True


def test_unproven_may_be_configured_to_wait_instead():
    gate, _ = _gate({"n": 3, "brier": None, "mean_forecast": None,
                     "base_rate": None}, unproven_may_trade=False)
    ok, why = gate.may_enter("weather_temperature")
    assert ok is False
    assert "UNPROVEN" in why


# --------------------------- the verdicts ---------------------------
def test_beating_climatology_is_calibrated():
    # base rate 0.5 -> benchmark 0.25; 0.18 beats it by 0.07.
    gate, _ = _gate({"n": 60, "brier": 0.18, "mean_forecast": 0.48,
                     "base_rate": 0.5})
    v = gate.verdict("weather_temperature")
    assert v.state is CalibrationState.CALIBRATED
    assert round(v.edge_over_climatology, 4) == 0.07
    assert v.may_enter is True


def test_losing_to_climatology_is_degraded_and_blocks_entry():
    gate, _ = _gate({"n": 60, "brier": 0.31, "mean_forecast": 0.48,
                     "base_rate": 0.5})
    v = gate.verdict("weather_temperature")
    assert v.state is CalibrationState.DEGRADED
    assert v.may_enter is False
    ok, why = gate.may_enter("weather_temperature")
    assert ok is False
    assert "WORSE" in v.detail


def test_the_middle_is_a_verdict_of_its_own():
    # Neither clearly better nor clearly worse. Keep trading, keep watching.
    gate, _ = _gate({"n": 60, "brier": 0.25, "mean_forecast": 0.5,
                     "base_rate": 0.5})
    v = gate.verdict("weather_temperature")
    assert v.state is CalibrationState.UNPROVEN
    assert v.may_enter is True


def test_a_good_absolute_score_can_still_be_degraded():
    # 0.09 sounds excellent. On a market that resolves YES 10% of the time,
    # saying "10%" every day scores 0.09 - so 0.13 is WORSE than knowing
    # nothing. This is the whole reason the benchmark is climatology.
    gate, _ = _gate({"n": 60, "brier": 0.13, "mean_forecast": 0.11,
                     "base_rate": 0.10})
    assert gate.verdict("x").state is CalibrationState.DEGRADED


# --------------------------- the absolute rule ---------------------------
def test_a_degraded_strategy_may_still_exit():
    gate, _ = _gate({"n": 200, "brier": 0.49, "mean_forecast": 0.5,
                     "base_rate": 0.5})
    assert gate.verdict("weather_temperature").state is CalibrationState.DEGRADED
    ok, why = CalibrationGate.may_exit("weather_temperature")
    assert ok is True
    assert "never gated" in why


def test_may_exit_needs_no_journal_at_all():
    # If reading the score is what blocks the exit, the exit is not safe.
    ok, _ = CalibrationGate.may_exit("anything")
    assert ok is True


# --------------------------- failure modes ---------------------------
def test_a_broken_journal_does_not_stop_trading():
    # Failing closed here means one database hiccup halts the bot.
    gate, _ = _gate(explode=True)
    v = gate.verdict("weather_temperature")
    assert v.state is CalibrationState.UNPROVEN
    assert "unavailable" in v.detail
    assert gate.may_enter("weather_temperature")[0] is True


def test_disabled_gate_never_reads_the_journal():
    gate, j = _gate({"n": 500, "brier": 0.9, "mean_forecast": 0.5,
                     "base_rate": 0.5}, enabled=False)
    assert gate.may_enter("weather_temperature")[0] is True
    assert j.reads == 0


def test_verdicts_are_cached_between_scans():
    gate, j = _gate({"n": 60, "brier": 0.18, "mean_forecast": 0.5,
                     "base_rate": 0.5}, refresh_seconds=300)
    gate.verdict("weather_temperature")
    gate.verdict("weather_temperature")
    gate.verdict("weather_temperature")
    assert j.reads == 1
    gate.verdict("weather_temperature", force=True)
    assert j.reads == 2


def test_each_estimator_is_judged_separately():
    gate, _ = _gate({"n": 60, "brier": 0.31, "mean_forecast": 0.5,
                     "base_rate": 0.5})
    gate.verdict("weather_temperature")
    # A second estimator shares the gate but not the verdict cache.
    gate.journal.stats = {"n": 60, "brier": 0.10, "mean_forecast": 0.5,
                          "base_rate": 0.5}
    assert gate.verdict("inflation").state is CalibrationState.CALIBRATED
    assert gate.verdict("weather_temperature").state is CalibrationState.DEGRADED


def test_describe_survives_having_no_data():
    gate, _ = _gate()
    assert "no settled forecasts" in gate.verdict("weather_temperature").describe()


def test_config_reads_from_yaml_shape():
    cfg = CalibrationConfig.from_dict({"enabled": True, "min_sample": 30,
                                       "calibrated_margin": 0.01,
                                       "degraded_margin": 0.03,
                                       "refresh_seconds": 300,
                                       "unproven_may_trade": True})
    assert cfg.min_sample == 30
    assert cfg.degraded_margin == 0.03
    # A missing section must not be a crash on startup.
    assert CalibrationConfig.from_dict(None).enabled is True


# ---------------- rows are not evidence ----------------
def test_calibrated_needs_distinct_contracts_not_just_rows():
    """The gate reported CALIBRATED over 1,605 forecasts while the dashboard,
    counting contracts, said there was not enough to judge. Same database."""
    j = FakeJournal({"n": 1605, "contracts": 74, "brier": 0.1377,
                     "base_rate": 0.21, "mean_forecast": 0.18})
    g = CalibrationGate(j, CalibrationConfig(min_sample=30, min_contracts=100))
    v = g.verdict("weather_temperature")
    assert v.state is CalibrationState.UNPROVEN
    assert "74 distinct contracts" in v.detail


def test_enough_contracts_can_still_be_calibrated():
    j = FakeJournal({"n": 1605, "contracts": 140, "brier": 0.1377,
                     "base_rate": 0.21, "mean_forecast": 0.18})
    g = CalibrationGate(j, CalibrationConfig(min_sample=30, min_contracts=100))
    assert g.verdict("weather_temperature").state is CalibrationState.CALIBRATED


def test_degraded_is_still_decided_on_rows():
    """Asymmetric on purpose: refusing to trade on thin evidence costs an
    opportunity, permitting it costs money. A clearly bad score must be able
    to suspend trading without first waiting for 100 contracts."""
    j = FakeJournal({"n": 200, "contracts": 12, "brier": 0.30,
                     "base_rate": 0.21, "mean_forecast": 0.18})
    g = CalibrationGate(j, CalibrationConfig(min_sample=30, min_contracts=100))
    assert g.verdict("weather_temperature").state is CalibrationState.DEGRADED


def test_the_since_boundary_is_passed_to_the_journal():
    """A configuration change makes a different forecaster."""
    j = FakeJournal({"n": 200, "contracts": 140, "brier": 0.10,
                     "base_rate": 0.21, "mean_forecast": 0.18})
    g = CalibrationGate(j, CalibrationConfig(since="2026-09-21",
                                             min_contracts=100))
    g.verdict("weather_temperature")
    assert j.last_kwargs.get("since") == "2026-09-21"
