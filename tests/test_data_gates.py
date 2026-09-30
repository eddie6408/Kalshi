"""Package 14A phase 4: data-quality gates and operator alerts.

The rule these pin: outside data reaches a trade only after it has been
checked, and a check changes trading only when it is ENFORCED. From day one
exactly one thing is enforced - a 5F disagreement between the two forecasts,
which is a broken source, not an opinion. Everything else records what it
WOULD have done, so it can be switched on with evidence rather than hope.
"""
import datetime as dt
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml

import pytest
from kalshibot import data_gates
from kalshibot.alerts import Notifier
from kalshibot.config import _StrictLoader
from kalshibot.data_gates import GateConfig, evaluate
from kalshibot.estimators.base import Estimate
from kalshibot.journal import Journal
from kalshibot.sources import SourceMonitor, SourceRegistry

from test_forecast import FEES, book, cfg, mkt
from kalshibot.strategies.forecast import ForecastStrategy

ROOT = Path(__file__).resolve().parents[1]


def _ev(**kw):
    base = {"ensemble_mean_f": 70.0, "cross_check_f": 71.0,
            "cross_check_diff_f": -1.0, "cross_check_source": "nws-gridpoint",
            "forecast_age_seconds": 3600, "observation_age_seconds": 900,
            "observed_so_far_f": None, "observation_suspect": None,
            "target_date": "2026-09-25", "provenance_ids": [1, 2]}
    base.update(kw)
    return base


# ------------------------------------------------------------------ config
def test_the_shipped_gate_config_enforces_only_the_hard_cap():
    raw = yaml.load(open(ROOT / "config.yaml"), Loader=_StrictLoader)
    g = GateConfig.from_dict(raw["sources"]["gates"])
    assert g.conflict_hard_cap_f == 5.0
    # First fit 2026-09-28 (3.0F), set in SHADOW only: it must not block a
    # trade until the operator switches conflict_mode to enforce.
    assert g.conflict_threshold_f is None or 2.0 <= g.conflict_threshold_f < 5.0
    assert (g.conflict_mode, g.source_down_mode, g.stale_mode, g.obs_qc_mode) == \
        ("shadow", "shadow", "shadow", "shadow")
    assert raw["sources"]["nws_contact"]


def test_an_unknown_mode_is_refused():
    with pytest.raises(ValueError):
        GateConfig.from_dict({"stale_mode": "maybe"})


# ----------------------------------------------------------- DATA_CONFLICT
def test_a_normal_gap_passes():
    assert evaluate(_ev(), GateConfig()).verdicts == []


def test_past_the_hard_cap_is_enforced():
    r = evaluate(_ev(cross_check_diff_f=-6.4), GateConfig())
    assert r.blocked is not None and r.blocked.gate == "DATA_CONFLICT"
    assert "hard cap" in r.blocked.detail


def test_the_fitted_threshold_only_records_while_in_shadow():
    g = GateConfig(conflict_threshold_f=2.5, conflict_mode="shadow")
    r = evaluate(_ev(cross_check_diff_f=3.0), g)
    assert r.blocked is None
    assert [v.label for v in r.verdicts] == ["WOULD_DATA_CONFLICT"]
    g.conflict_mode = "enforce"
    assert evaluate(_ev(cross_check_diff_f=3.0), g).blocked.gate == "DATA_CONFLICT"


def test_no_cross_check_means_no_conflict_verdict():
    assert evaluate(_ev(cross_check_diff_f=None), GateConfig()).verdicts == []


# --------------------------------------------------- the shadow-only gates
def test_a_required_source_down_is_recorded():
    reg = SourceRegistry.from_config({"registry": {"ens": {
        "publisher": "Open-Meteo", "tier": 2, "roles": ["primary"],
        "endpoint": "x", "required_by": ["weather_temperature"]}}})
    mon = SourceMonitor(reg, down_after=2)
    mon.record("ens", False)
    mon.record("ens", False)
    r = evaluate(_ev(), GateConfig(), mon, "weather_temperature")
    assert [v.label for v in r.verdicts] == ["WOULD_SOURCE_UNAVAILABLE"]
    assert r.blocked is None


def test_stale_forecast_and_stale_observation():
    g = GateConfig(max_forecast_age_seconds=3600, max_observation_age_seconds=1800)
    r = evaluate(_ev(forecast_age_seconds=7200, observed_so_far_f=71.0,
                     observation_age_seconds=4000), g)
    assert [v.label for v in r.verdicts] == ["WOULD_STALE_SOURCE"] * 2
    # an old observation that is not being used as a floor is irrelevant
    r = evaluate(_ev(observation_age_seconds=4000), g)
    assert r.verdicts == []


def test_a_suspect_observation_is_recorded():
    r = evaluate(_ev(observed_so_far_f=97.0,
                     observation_suspect="97.0F is 8.0F clear of the next reading"),
                 GateConfig())
    assert [v.label for v in r.verdicts] == ["WOULD_OBSERVATION_QC"]


def test_off_means_not_evaluated():
    g = GateConfig(stale_mode="off")
    assert evaluate(_ev(forecast_age_seconds=10 ** 9), g).verdicts == []


# ------------------------------------------------ inside the strategy
class EvEstimator:
    name = "fake"

    def __init__(self, ev):
        self.ev = ev

    def candidate_series(self):
        return ["KXFAKE"]

    def estimate(self, market):
        return Estimate(0.72, 0.9, self.name, dict(self.ev))


def test_the_strategy_skips_a_hard_conflict_and_alerts_once():
    sent = []
    import kalshibot.strategies.forecast as fmod
    old = fmod.ALERTS
    fmod.ALERTS = Notifier(topic="t", post=lambda *a, **k: sent.append(k) or
                           type("R", (), {"status_code": 200})())
    try:
        with tempfile.TemporaryDirectory() as tmp:
            j = Journal(Path(tmp) / "j.db")
            s = ForecastStrategy(EvEstimator(_ev(cross_check_diff_f=6.0)), FEES,
                                 cfg(), journal=j, gates=GateConfig())
            assert s.find([mkt()], {"KXFAKE-1": book()}, 500.0) == []
            assert s.skips[-1].reason == "data conflict"
            s.find([mkt()], {"KXFAKE-1": book()}, 500.0)
            assert len(sent) == 1                    # once per city per day
            row = j.conn.execute("SELECT * FROM data_checks").fetchall()
            assert len(row) == 1                     # once per distinct forecast
            assert row[0]["verdicts"] == "DATA_CONFLICT" and row[0]["enforced"] == 1
    finally:
        fmod.ALERTS = old


def test_shadow_verdicts_never_stop_a_trade():
    with tempfile.TemporaryDirectory() as tmp:
        j = Journal(Path(tmp) / "j.db")
        ev = _ev(cross_check_diff_f=3.0, forecast_age_seconds=10 ** 6)
        g = GateConfig(conflict_threshold_f=2.0, max_forecast_age_seconds=60)
        s = ForecastStrategy(EvEstimator(ev), FEES, cfg(), journal=j, gates=g)
        ops = s.find([mkt()], {"KXFAKE-1": book()}, 500.0)
        assert len(ops) == 1, s.skips
        v = j.conn.execute("SELECT verdicts, enforced FROM data_checks").fetchone()
        assert v["enforced"] == 0
        assert set(v["verdicts"].split(",")) == {"WOULD_DATA_CONFLICT", "WOULD_STALE_SOURCE"}


def test_no_gates_configured_is_the_old_behaviour():
    s = ForecastStrategy(EvEstimator(_ev(cross_check_diff_f=50.0)), FEES, cfg())
    assert len(s.find([mkt()], {"KXFAKE-1": book()}, 500.0)) == 1


# -------------------------------------------------------------- alerts
def test_alerts_are_deduplicated_and_never_raise():
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("network down")
    n = Notifier(topic="secret-topic", post=boom)
    assert n.send("k", "t", "m") is False and n.failed == 1
    assert n.send("k", "t", "m") is False and len(calls) == 1


def test_no_topic_means_log_only():
    n = Notifier(topic="", post=lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert not n.active
    assert n.send("k", "t", "m") is False


# ------------------------------------------------ live observation QC
def _obs_weather(temps_c, flags=None):
    from kalshibot.weatherdata import WeatherData
    from test_provenance import RouteSession
    flags = flags or ["V"] * len(temps_c)
    feats = [{"properties": {"timestamp": f"2026-09-25T{10 + i:02d}:51:00+00:00",
                             "temperature": {"value": t, "qualityControl": q}}}
             for i, (t, q) in enumerate(zip(temps_c, flags))]
    wd = WeatherData(monitor=SourceMonitor())
    wd._session = RouteSession({"/observations": {"features": feats}})
    return wd


def _today(tz="America/New_York"):
    from zoneinfo import ZoneInfo
    return dt.datetime.now(ZoneInfo(tz)).date()


def test_a_lone_spike_in_todays_observations_is_flagged():
    wd = _obs_weather([20.0, 20.5, 21.0, 21.2, 27.0])     # 80.6F vs 70.2F
    value, _t, _id, suspect = wd._observed_extreme("KNYC", "America/New_York",
                                                   _today(), "high")
    assert value is not None and "spike" in suspect


def test_a_normal_afternoon_is_not_flagged():
    wd = _obs_weather([18.0, 19.5, 21.0, 22.0, 22.8])
    assert wd._observed_extreme("KNYC", "America/New_York", _today(), "high")[3] == ""


def test_an_nws_rejected_reading_is_flagged():
    wd = _obs_weather([20.0, 21.0, 23.0], flags=["V", "V", "X"])
    assert "quality flag X" in wd._observed_extreme(
        "KNYC", "America/New_York", _today(), "high")[3]


# ------------------------------------------------- the threshold fit
def test_the_fit_counts_city_days_not_rows():
    sys.path.insert(0, str(ROOT / "scripts"))
    import fit_conflict_threshold as fit
    with tempfile.TemporaryDirectory() as tmp:
        j = Journal(Path(tmp) / "j.db")
        for _ in range(40):                            # one noisy day, many rows
            j.log_data_check(series="KXHIGHLAX", target_date="2026-09-25",
                             lead_hours=3.0, diff_f=6.0)
        j.log_data_check(series="KXHIGHNY", target_date="2026-09-25",
                         lead_hours=3.0, diff_f=-1.0)
        j.log_data_check(series="KXHIGHNY", target_date="2026-09-26",
                         lead_hours=30.0, diff_f=9.0)   # too far out: ignored
        days = fit.city_days(j.conn, 6.0)
        assert days == {("KXHIGHLAX", "2026-09-25"): 6.0,
                        ("KXHIGHNY", "2026-09-25"): 1.0}
        assert fit.percentile([1.0, 2.0, 3.0], 50) == 2.0


def test_checks_are_recorded_past_the_lead_gate_but_not_acted_on():
    """Most of the day every market is past max_lead_hours. Their data
    checks must still be recorded (the fit needs them), but a conflict on a
    market we would not trade anyway must not alert anyone."""
    sent = []
    import kalshibot.strategies.forecast as fmod
    old = fmod.ALERTS
    fmod.ALERTS = Notifier(topic="t", post=lambda *a, **k: sent.append(k))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            j = Journal(Path(tmp) / "j.db")
            ev = _ev(cross_check_diff_f=6.4, lead_hours=30.0)
            s = ForecastStrategy(EvEstimator(ev), FEES, cfg(max_lead_hours=6),
                                 journal=j, gates=GateConfig())
            assert s.find([mkt()], {"KXFAKE-1": book()}, 500.0) == []
            assert s.skips[-1].reason == "too far out to have skill"
            assert j.conn.execute("SELECT COUNT(*) FROM data_checks").fetchone()[0] == 1
            assert sent == []
    finally:
        fmod.ALERTS = old


def test_highs_and_lows_have_their_own_conflict_thresholds():
    """2026-09-30 re-fit: highs 4.0F (approved); lows keep the 5F cap until
    their own fit - late-day NWS lows cover only the remaining hours."""
    from kalshibot.data_gates import GateConfig, evaluate
    g = GateConfig.from_dict({"conflict_threshold_f": 3.0, "conflict_threshold_high_f": 4.0,
                              "conflict_threshold_low_f": 5.0, "conflict_mode": "enforce"})
    def ev(kind, diff):
        return {"kind": kind, "cross_check_diff_f": diff, "ensemble_mean_f": 70,
                "cross_check_f": 70 - diff, "cross_check_source": "nws"}
    assert evaluate(ev("high", 3.5), g).blocked is None          # under 4.0
    assert evaluate(ev("high", 4.2), g).blocked is not None      # over 4.0
    assert evaluate(ev("low", 4.8), g).blocked is None           # lows: only the 5F cap
    assert evaluate(ev("low", 5.5), g).blocked is not None       # hard cap
    assert evaluate(ev("", 3.5), g).blocked is not None          # unknown kind: generic 3.0
    shadow = GateConfig.from_dict({"conflict_threshold_high_f": 4.0, "conflict_mode": "shadow"})
    r = evaluate(ev("high", 4.2), shadow)
    assert r.blocked is None and r.verdicts[0].label == "WOULD_DATA_CONFLICT"


def test_the_live_config_carries_the_approved_thresholds_and_exposure():
    import yaml
    from pathlib import Path
    c = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text())
    g = c["sources"]["gates"]
    assert g["conflict_threshold_high_f"] == 4.0 and g["conflict_threshold_low_f"] == 5.0
    assert c["limits"]["max_total_exposure_usd"] == 200.0
    assert c["limits"]["max_market_exposure_usd"] == 25.0 and c["limits"]["max_event_exposure_usd"] == 30.0
