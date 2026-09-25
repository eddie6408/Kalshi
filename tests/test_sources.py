"""Package 14A: the source registry and the health monitor.

The registry is the list of places the bot is allowed to learn things from.
These tests pin the rules that make it worth having: tier 4 never reaches a
trade, fallbacks are named in advance, and the endpoints the code actually
calls are the endpoints the registry says it calls.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

import pytest
from kalshibot.config import ENDPOINTS, _StrictLoader
from kalshibot.sources import (DEGRADED, DOWN, HEALTHY, OFF, UNKNOWN,
                               SourceConfigError, SourceMonitor,
                               SourceRegistry, nws_user_agent)

ROOT = Path(__file__).resolve().parents[1]


def _config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as fh:
        return yaml.load(fh, Loader=_StrictLoader)


def _reg(**sources) -> SourceRegistry:
    return SourceRegistry.from_config({"registry": sources})


def _src(**kw) -> dict:
    base = {"publisher": "Someone", "tier": 1, "roles": ["primary"],
            "endpoint": "https://example.test/"}
    base.update(kw)
    return base


# ---------------------------------------------------------------- registry
def test_the_shipped_registry_loads_and_obeys_the_rules():
    reg = SourceRegistry.from_config(_config()["sources"])
    assert "open_meteo_ensemble" in reg
    assert reg.chain("open_meteo_ensemble") == ["open_meteo_ensemble", "nws_gridpoint"]
    assert reg.chain("nws_climate_report") == ["nws_climate_report", "nws_observations"]
    assert all(s.tier in (1, 2) for s in reg)


def test_tier_4_may_not_be_a_trading_input():
    with pytest.raises(SourceConfigError):
        _reg(blog=_src(tier=4, roles=["primary"]))
    # ...but it may point the way, as long as nothing trades on it
    reg = _reg(blog=_src(tier=4, roles=["discovery"]))
    assert not reg.get("blog").can_reach_a_decision


def test_no_strategy_may_require_a_tier_4_source():
    with pytest.raises(SourceConfigError):
        _reg(blog=_src(tier=4, roles=["discovery"], required_by=["x"]))


def test_a_fallback_must_be_named_in_advance():
    with pytest.raises(SourceConfigError):
        _reg(a=_src(fallback_to="whatever_google_returns"))


def test_a_fallback_may_not_be_tier_4():
    with pytest.raises(SourceConfigError):
        _reg(a=_src(fallback_to="b"), b=_src(tier=4, roles=["discovery"]))


def test_fallback_cycles_are_refused():
    with pytest.raises(SourceConfigError):
        _reg(a=_src(fallback_to="b"), b=_src(fallback_to="a"))


def test_every_source_needs_a_publisher_a_tier_and_a_known_role():
    for bad in (_src(publisher=""), _src(tier=7), _src(roles=[]),
                _src(roles=["vibes"])):
        with pytest.raises(SourceConfigError):
            _reg(a=bad)


def test_the_registry_describes_the_endpoints_the_code_really_calls():
    """A registry that drifts from the code is a comment, not a control."""
    from kalshibot import forecast_scoring, livebooks, weatherdata
    reg = SourceRegistry.from_config(_config()["sources"])
    assert reg.get("kalshi_rest").endpoint == ENDPOINTS["demo"]["rest"]
    assert reg.get("kalshi_ws").endpoint == ENDPOINTS["demo"]["ws"]
    assert reg.get("kalshi_live_market_data").endpoint == livebooks.LIVE_MARKET_DATA_BASE
    assert reg.get("open_meteo_ensemble").endpoint == weatherdata.ENSEMBLE_URL
    assert reg.get("open_meteo_model_runs").endpoint == weatherdata.MODEL_META_URL
    assert reg.get("nws_observations").endpoint == weatherdata.NWS_OBS
    assert reg.get("nws_climate_report").endpoint == forecast_scoring.CLI_LIST_URL


def test_every_weather_series_names_its_climate_report():
    for s in _config()["weather"]["series"]:
        assert s.get("climate_report"), s["ticker_prefix"]


def test_nws_user_agent_carries_a_contact_only_when_given():
    assert "@" not in nws_user_agent({})
    assert nws_user_agent({"nws_contact": "me@example.com"}).endswith("me@example.com)")


# ----------------------------------------------------------------- monitor
class _Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def test_health_moves_from_unknown_to_healthy_to_degraded_to_down():
    m = SourceMonitor(down_after=3)
    assert m.state("s") == UNKNOWN
    m.record("s", True)
    assert m.state("s") == HEALTHY
    m.record("s", False, error="boom")
    assert m.state("s") == DEGRADED
    m.record("s", False)
    m.record("s", False)
    assert m.state("s") == DOWN
    # One success ends DOWN, but three of the last five failing is still
    # not a healthy source.
    m.record("s", True)
    assert m.state("s") == DEGRADED


def test_a_high_recent_error_rate_is_degraded_even_after_a_success():
    m = SourceMonitor(down_after=10, degraded_error_rate=0.25, window=8)
    for ok in (True, False, True, False, True, False, True):
        m.record("s", ok)
    assert m.state("s") == DEGRADED


def test_stale_data_is_degraded():
    clock = _Clock()
    reg = _reg(s=_src(max_data_age_seconds=60))
    m = SourceMonitor(reg, clock=clock)
    from datetime import datetime, timezone
    m.record("s", True, data_time=datetime.fromtimestamp(clock.t - 30, timezone.utc))
    assert m.state("s") == HEALTHY
    clock.t += 120
    assert m.state("s") == DEGRADED


def test_track_records_failures_and_reraises_untouched():
    m = SourceMonitor()
    with pytest.raises(ValueError):
        with m.track("s"):
            raise ValueError("nope")
    snap = {r["source_id"]: r for r in m.snapshot()}["s"]
    assert snap["failures"] == 1 and "nope" in snap["last_error"]


def test_track_treats_an_http_error_status_as_a_failure():
    m = SourceMonitor()
    with m.track("s") as call:
        call.status = 503
    assert m.state("s") == DEGRADED


def test_observe_without_a_sink_is_a_no_op_and_a_broken_sink_never_raises():
    m = SourceMonitor()
    assert m.observe("s", value=1.0) is None

    def broken(**_kw):
        raise RuntimeError("disk full")
    m.set_sink(broken)
    assert m.observe("s", value=1.0) is None


def test_observe_stamps_publisher_and_tier_from_the_registry():
    got = {}
    m = SourceMonitor(_reg(s=_src(publisher="NOAA", tier=1)))
    m.set_sink(lambda **kw: got.update(kw) or 7)
    assert m.observe("s", value=3.0) == 7
    assert got["publisher"] == "NOAA" and got["tier"] == 1


def test_a_probe_overrides_the_counted_state():
    m = SourceMonitor()
    m.add_probe("ws", lambda: (OFF, "feed disabled"))
    assert m.state("ws") == OFF
    row = {r["source_id"]: r for r in m.snapshot()}["ws"]
    assert row["state"] == OFF and row["detail"] == "feed disabled"
