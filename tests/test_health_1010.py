"""2026-10-10 health-check fixes.

Pinned: the calibration gate does not judge on one event or against a
hindsight baseline; the nowcast recalibration sharpens an under-confident
model, leaves a calibrated one alone, and is applied only where enabled; the
ensemble is re-downloaded only when a model publishes a new run; the
production watch reads books from the socket instead of REST when it can.
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pytest

from kalshibot import recalibration as rc
from kalshibot.calibration_gate import CalibrationConfig, CalibrationGate, CalibrationState


class _J:
    def __init__(self, stats):
        self.stats = stats

    def calibration(self, estimator=None, since=None):
        return self.stats


def _verdict(stats, **cfg):
    return CalibrationGate(_J(stats), CalibrationConfig.from_dict(cfg)).verdict("x", force=True)


# ---- fix 1 -----------------------------------------------------------------
def test_one_release_all_no_is_not_a_degraded_verdict():
    """The 2026-10-03 case: 198 rows, 6 strikes of one PCE release, all NO."""
    v = _verdict({"n": 198, "contracts": 6, "events": 1, "brier": 0.204, "base_rate": 0.0,
                  "mean_forecast": 0.3}, min_sample=30, min_contracts=100)
    assert v.state is CalibrationState.UNPROVEN and v.may_enter
    assert "1 distinct event" in v.detail


def test_a_one_sided_baseline_is_hindsight_not_a_benchmark():
    v = _verdict({"n": 500, "contracts": 200, "events": 40, "brier": 0.05, "base_rate": 0.0,
                  "mean_forecast": 0.1}, min_sample=30, min_contracts=100)
    assert v.state is CalibrationState.UNPROVEN and "hindsight" in v.detail


def test_a_real_degraded_model_is_still_suspended():
    v = _verdict({"n": 500, "contracts": 200, "events": 40, "brier": 0.30, "base_rate": 0.3,
                  "mean_forecast": 0.6}, min_sample=30, min_contracts=100)
    assert v.state is CalibrationState.DEGRADED and not v.may_enter


def test_the_journal_counts_events():
    import sqlite3
    from kalshibot.journal import Journal
    d = tempfile.mkdtemp()
    j = Journal(Path(d) / "j.db")
    for i, (t, date) in enumerate([("A-T1", "2026-10-01"), ("A-T2", "2026-10-01"), ("A-T1", "2026-10-02")]):
        j.conn.execute("INSERT INTO probability_log (ts, estimator, series, ticker, target_date, "
                       "our_probability, settled_yes, brier) VALUES (?,?,?,?,?,?,?,?)",
                       (f"2026-10-0{i+1}T00:00", "e", "A", t, date, 0.5, 1, 0.25))
    j.conn.commit()
    s = j.calibration(estimator="e")
    assert s["n"] == 3 and s["events"] == 2


# ---- fix 3 -----------------------------------------------------------------
def test_recalibration_sharpens_an_under_confident_model_and_leaves_a_good_one():
    rng = np.random.default_rng(1)
    true = rng.uniform(0.01, 0.99, 6000)
    y = (rng.uniform(size=6000) < true).astype(float)
    # under-confident: reported probabilities pulled halfway to 0.5 in logit space
    reported = rc.sigmoid(0.5 * rc.logit(true))
    br = np.zeros(6000)
    beta = rc.fit_map(reported, br, y, l2=0.1)
    assert 1.6 < beta[1] < 2.4                       # recovers the ~2x sharpening
    assert rc.brier(rc.apply_map(beta, reported, br), y) < rc.brier(reported, y)
    good = rc.fit_map(true, br, y, l2=0.1)
    assert abs(good[1] - 1.0) < 0.15 and abs(good[0]) < 0.15


def test_the_recalibrator_applies_only_enabled_kinds():
    r = rc.Recalibrator({"kinds": {"LOW": {"beta": [0, 2, 0, 0], "enabled": True},
                                   "HIGH": {"beta": [0, 2, 0, 0], "enabled": False}}})
    assert r.applies("LOW") and not r.applies("HIGH")
    assert r("LOW", 0.25, True) < 0.15                # long shot made rarer
    assert r("HIGH", 0.25, True) == 0.25              # not enabled: untouched
    assert rc.Recalibrator.load("/nonexistent.json").applies("LOW") is False


# ---- fix 2 -----------------------------------------------------------------
def test_the_ensemble_is_refetched_only_for_a_new_model_run():
    from kalshibot import weatherdata as wd
    d = wd.WeatherData()
    runs = {"gfs_seamless": "2026-10-10T06:00"}
    d._ensemble_runs = lambda: dict(runs)
    now = time.time()
    fresh = (now - 60, None, 200, 1.0, None, dict(runs))
    assert d._ensemble_still_current(fresh)                         # under 30 min
    same = (now - 3 * 3600, None, 200, 1.0, None, dict(runs))
    assert d._ensemble_still_current(same)                          # 3 h, same run
    runs["gfs_seamless"] = "2026-10-10T12:00"
    assert not d._ensemble_still_current(same)                      # new run published
    old = (now - 7 * 3600, None, 200, 1.0, None, dict(runs))
    assert not d._ensemble_still_current(old)                       # past 6 h regardless
    d._ensemble_runs = lambda: {}
    assert d._ensemble_still_current((now - 3600, None, 200, 1.0, None, {}))
    assert not d._ensemble_still_current((now - 3 * 3600, None, 200, 1.0, None, {}))


# ---- fix 4 -----------------------------------------------------------------
def test_the_production_watch_reads_books_from_the_socket_not_rest():
    from kalshibot.market_state import MarketState
    from kalshibot.prodwatch import ProductionWatch

    class Reader:
        calls = 0

        def orderbook(self, t):
            Reader.calls += 1
            return None
    live = MarketState(None)
    live.feed_live = lambda: True
    live.apply_snapshot("T", [(40, 10)], [(55, 5)])
    w = ProductionWatch(Reader())
    w.live_state = live
    w.watch("T")
    w.run_pass()
    assert Reader.calls == 0 and w.live_books == 1 and w.latest("T").yes_bid == 40
    w.watch("U")                                   # not on the socket: REST as before
    w._w["U"].book_at = 0
    w.run_pass()
    assert Reader.calls == 1
