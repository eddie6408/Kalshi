"""Production lockout and the profitability gate.

If any test in this file fails, the bot must not be run.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.clients import DemoExecutionClient, MarketDataClient, PortfolioClient
from kalshibot.env_guard import (DEMO_HOSTS, PRODUCTION_HOSTS, ProductionBlocked,
                                 assert_demo_environment, assert_demo_url,
                                 assert_trading_allowed, is_demo_host)
from kalshibot.client import KalshiClient as KalshiClientForBody
from kalshibot.gate import ProfitabilityGate, Reject
from kalshibot.opportunity import ArbitrageOpportunity, ExecutionRisk, Leg

DEMO = "https://external-api.demo.kalshi.co/trade-api/v2"


# ===================== production lockout =====================
def test_production_endpoint_rejected():
    for host in PRODUCTION_HOSTS:
        with pytest.raises(ProductionBlocked):
            assert_demo_url(f"https://{host}/trade-api/v2")


def test_demo_endpoints_allowed():
    for host in DEMO_HOSTS:
        assert_demo_url(f"https://{host}/trade-api/v2")
        assert is_demo_host(f"https://{host}/x")


def test_demo_environment_required():
    for bad in ("prod", "production", "live", "", "PROD", "dem0"):
        with pytest.raises(ProductionBlocked):
            assert_demo_environment(bad)
    assert assert_demo_environment("demo") == "demo"
    assert assert_demo_environment("  DEMO  ") == "demo"


def test_production_credentials_rejected():
    """A demo env name with a production URL, or the reverse, must both fail."""
    with pytest.raises(ProductionBlocked):
        assert_trading_allowed("demo", "https://external-api.kalshi.com/trade-api/v2")
    with pytest.raises(ProductionBlocked):
        assert_trading_allowed("prod", DEMO)
    assert_trading_allowed("demo", DEMO)


def test_lookalike_domains_rejected():
    for url in ("https://external-api.demo.kalshi.co.attacker.net/x",
                "https://kalshi.com.evil.io/x",
                "https://notkalshi.com/x",
                "https://kalshi.co/x",
                "https://demo.kalshi.evil/x"):
        assert not is_demo_host(url), url
        with pytest.raises(ProductionBlocked):
            assert_demo_url(url)


def test_every_client_refuses_production():
    prod = "https://external-api.kalshi.com/trade-api/v2"
    with pytest.raises(ProductionBlocked):
        MarketDataClient(prod)
    with pytest.raises(ProductionBlocked):
        PortfolioClient(prod, None)
    with pytest.raises(ProductionBlocked):
        DemoExecutionClient(prod, None, "demo")


def test_execution_client_has_no_generic_place_order():
    """A stray place_order() must be an AttributeError, not a live trade."""
    c = DemoExecutionClient(DEMO, None, "demo")
    assert not hasattr(c, "place_order")
    assert hasattr(c, "place_demo_order")


def test_execution_client_rechecks_on_every_order():
    """Mutating the URL after construction must not open a path."""
    c = DemoExecutionClient(DEMO, None, "demo")
    c.base_url = "https://external-api.kalshi.com/trade-api/v2"
    with pytest.raises(ProductionBlocked):
        c.place_demo_order("T", "buy", "yes", 1, 50)


def test_execution_client_rechecks_environment_on_every_order():
    c = DemoExecutionClient(DEMO, None, "demo")
    c.env = "prod"
    with pytest.raises(ProductionBlocked):
        c.place_demo_order("T", "buy", "yes", 1, 50)


def test_zero_or_negative_quantity_refused_at_the_client():
    c = DemoExecutionClient(DEMO, None, "demo")
    for n in (0, -1, -100):
        with pytest.raises(ValueError):
            c.place_demo_order("T", "buy", "yes", n, 50)


def test_out_of_range_price_refused_at_the_client():
    c = DemoExecutionClient(DEMO, None, "demo")
    for p in (0, 100, 101, -5):
        with pytest.raises(ValueError):
            c.place_demo_order("T", "buy", "yes", 1, p)


# ===================== profitability gate =====================
def _op(**kw):
    d = dict(
        strategy_type="same_market", event_id="EV", market_tickers=["T"],
        legs=[Leg("T", "yes", "buy", 10, 40, 40.0, 100),
              Leg("T", "no", "buy", 10, 52, 52.0, 100)],
        quantity=10, gross_cost=9.20, guaranteed_min_payout=10.0, max_payout=10.0,
        gross_edge=0.80, estimated_fees=0.35, net_edge=0.45,
        net_edge_per_contract=0.045, expected_return_pct=4.89,
        execution_risk=ExecutionRisk.LOW, guaranteed=True, fee_drag_pct=43.8,
        max_executable_quantity=100, min_profitable_quantity=2, optimal_quantity=10,
    )
    d.update(kw)
    return ArbitrageOpportunity(**d)


def test_profitability_gate():
    g = ProfitabilityGate({})
    d = g.evaluate(_op())
    assert d.accepted and d.reason is Reject.OK
    assert len(d.checks) >= 8


def test_gate_rejects_zero_quantity():
    assert ProfitabilityGate({}).evaluate(_op(quantity=0)).reason is Reject.ZERO_QUANTITY


def test_gate_rejects_fee_drag():
    g = ProfitabilityGate({"max_fee_drag_percent": 40})
    assert g.evaluate(_op(fee_drag_pct=95)).reason is Reject.FEE_DRAG


def test_gate_rejects_thin_edge():
    g = ProfitabilityGate({"min_net_edge_usd": 5.0})
    assert g.evaluate(_op()).reason is Reject.BELOW_MIN_EDGE


def test_gate_rejects_thin_percentage():
    g = ProfitabilityGate({"min_net_edge_percent": 50.0})
    assert g.evaluate(_op()).reason is Reject.BELOW_MIN_EDGE_PCT


def test_gate_rejects_cost_above_payout_floor():
    d = ProfitabilityGate({}).evaluate(_op(gross_cost=11.0))
    assert d.reason is Reject.NEGATIVE_FLOOR


def test_stale_opportunity():
    old = _op()
    old.timestamp = datetime.now(timezone.utc) - timedelta(seconds=120)
    g = ProfitabilityGate({"max_opportunity_age_seconds": 20})
    assert g.evaluate(old).reason is Reject.STALE


def test_gate_rejects_excess_execution_risk():
    g = ProfitabilityGate({"max_execution_risk": "low"})
    assert g.evaluate(_op(execution_risk=ExecutionRisk.HIGH)).reason is Reject.EXECUTION_RISK


def test_gate_rejects_too_many_legs():
    g = ProfitabilityGate({"max_legs": 2})
    op = _op()
    op.legs = op.legs * 3
    assert g.evaluate(op).reason is Reject.TOO_MANY_LEGS


def test_gate_rejects_unguaranteed_when_configured():
    """require_guaranteed still blocks every forecast-derived edge. The reason
    code moved to FORECASTS_DISABLED when the gate forked, but the protection
    is the same one: no opinion-based trade without an explicit opt-in."""
    g = ProfitabilityGate({"require_guaranteed": True})
    d = g.evaluate(_op(guaranteed=False))
    assert not d.accepted
    assert d.reason is Reject.FORECASTS_DISABLED


def test_forecasts_are_off_unless_both_switches_are_set():
    """Two independent switches, and BOTH must be right. Flipping one by
    accident must not start opinion-based trading."""
    op = _op(guaranteed=False, confidence=0.9, probability_yes=0.72,
             implied_probability=0.61, ev_usd=1.0)
    for cfg in ({"require_guaranteed": True, "allow_forecast": True},
                {"require_guaranteed": False, "allow_forecast": False},
                {"require_guaranteed": True, "allow_forecast": False},
                {}):
        assert ProfitabilityGate(cfg).evaluate(op).reason is Reject.FORECASTS_DISABLED, cfg


def test_a_forecast_trade_never_passes_the_structural_floor_check():
    """A forecast opportunity has guaranteed_min_payout of 0. It must be
    routed to the forecast rules, never judged as though that zero were a
    real payout floor, and never accepted as guaranteed."""
    g = ProfitabilityGate({"require_guaranteed": False, "allow_forecast": True,
                           "min_confidence": 0.5, "min_probability_edge": 0.05,
                           "min_ev_usd": 0.05})
    op = _op(guaranteed=False, guaranteed_min_payout=0.0, gross_cost=6.10,
             confidence=0.80, probability_yes=0.72, implied_probability=0.61,
             ev_usd=1.10, net_edge=1.10, gross_edge=1.30, estimated_fees=0.20,
             fee_drag_pct=15.0, execution_risk=ExecutionRisk.NONE)
    d = g.evaluate(op)
    assert d.accepted, d
    assert "NOT guaranteed" in d.detail


def test_forecast_gate_rejects_thin_probability_edge():
    g = ProfitabilityGate({"require_guaranteed": False, "allow_forecast": True,
                           "min_probability_edge": 0.05})
    op = _op(guaranteed=False, guaranteed_min_payout=0.0, confidence=0.9,
             probability_yes=0.63, implied_probability=0.61, ev_usd=1.0,
             execution_risk=ExecutionRisk.NONE)
    assert g.evaluate(op).reason is Reject.BELOW_MIN_PROBABILITY_EDGE


def test_forecast_gate_rejects_low_confidence():
    g = ProfitabilityGate({"require_guaranteed": False, "allow_forecast": True,
                           "min_confidence": 0.60})
    op = _op(guaranteed=False, guaranteed_min_payout=0.0, confidence=0.31,
             probability_yes=0.72, implied_probability=0.61, ev_usd=1.0,
             execution_risk=ExecutionRisk.NONE)
    assert g.evaluate(op).reason is Reject.BELOW_MIN_CONFIDENCE


def test_forecast_gate_rejects_negative_expected_value():
    g = ProfitabilityGate({"require_guaranteed": False, "allow_forecast": True})
    op = _op(guaranteed=False, guaranteed_min_payout=0.0, confidence=0.9,
             probability_yes=0.72, implied_probability=0.61, ev_usd=-0.40,
             net_edge=-0.40, execution_risk=ExecutionRisk.NONE)
    assert g.evaluate(op).reason is Reject.NEGATIVE_EV


def test_turning_forecasts_on_does_not_weaken_structural_trades():
    """The floor check must still apply to structural trades when the
    forecast switches are on."""
    g = ProfitabilityGate({"require_guaranteed": False, "allow_forecast": True})
    assert g.evaluate(_op(gross_cost=11.0)).reason is Reject.NEGATIVE_FLOOR
    assert g.evaluate(_op()).accepted


def test_gate_thresholds_are_all_configurable():
    g = ProfitabilityGate({"min_net_edge_usd": 1.23, "min_net_edge_percent": 4.5,
                           "min_profit_per_trade_usd": 6.7,
                           "max_fee_drag_percent": 12.0, "max_legs": 3,
                           "max_opportunity_age_seconds": 99})
    assert g.min_net_edge_usd == 1.23 and g.min_net_edge_percent == 4.5
    assert g.min_profit_per_trade_usd == 6.7 and g.max_fee_drag_percent == 12.0
    assert g.max_legs == 3 and g.max_opportunity_age_seconds == 99


# ===================== the loopback test hook =====================
def test_loopback_hook_is_off_by_default():
    import os
    from unittest import mock
    env = {k: v for k, v in os.environ.items()
           if k != "KALSHIBOT_ALLOW_LOOPBACK_FOR_TESTS"}
    with mock.patch.dict(os.environ, env, clear=True):
        with pytest.raises(ProductionBlocked):
            assert_demo_url("http://127.0.0.1:9999/trade-api/v2")


def test_loopback_hook_allows_only_loopback():
    import os
    from unittest import mock
    with mock.patch.dict(os.environ,
                         {"KALSHIBOT_ALLOW_LOOPBACK_FOR_TESTS": "1"}):
        assert_demo_url("http://127.0.0.1:9999/trade-api/v2")
        assert_demo_url("http://localhost:9999/trade-api/v2")
        for public in ("https://example.com/x", "https://8.8.8.8/x"):
            with pytest.raises(ProductionBlocked):
                assert_demo_url(public)


def test_loopback_hook_can_never_unlock_production():
    """The most important test in this file. Even with the test flag on,
    every production host must still be refused."""
    import os
    from unittest import mock
    with mock.patch.dict(os.environ,
                         {"KALSHIBOT_ALLOW_LOOPBACK_FOR_TESTS": "1"}):
        for host in PRODUCTION_HOSTS:
            assert not is_demo_host(f"https://{host}/x"), host
            with pytest.raises(ProductionBlocked):
                assert_demo_url(f"https://{host}/trade-api/v2")
        with pytest.raises(ProductionBlocked):
            assert_trading_allowed("demo", "https://external-api.kalshi.com/x")


def test_rest_base_override_cannot_reach_production():
    """The override that lets tests point at a mock exchange must still
    refuse every production hostname."""
    import os
    from unittest import mock

    from kalshibot.config import Settings
    s = Settings(raw={}, project_root=Path("."), key_id="k",
                 private_key_path=Path("x"), kalshi_env="demo")
    with mock.patch.dict(os.environ, {
            "KALSHIBOT_ALLOW_LOOPBACK_FOR_TESTS": "1",
            "KALSHI_REST_BASE_OVERRIDE": "https://external-api.kalshi.com/trade-api/v2"}):
        with pytest.raises(ProductionBlocked):
            _ = s.rest_base


def test_gate_rejects_implausible_guaranteed_return():
    """A structure claiming a large risk-free return is a modelling error far
    more often than it is free money. The live board produced +271% "guaranteed"
    ladders that had a zero-payout world; this is the second line of defence."""
    g = ProfitabilityGate({"max_guaranteed_return_percent": 100.0})
    d = g.evaluate(_op(expected_return_pct=271.92))
    assert d.reason is Reject.IMPLAUSIBLE_RETURN
    assert not d.accepted


def test_implausible_check_does_not_touch_ordinary_arbitrage():
    g = ProfitabilityGate({"max_guaranteed_return_percent": 100.0})
    assert g.evaluate(_op()).accepted


def test_implausible_check_is_configurable_and_can_be_disabled():
    assert ProfitabilityGate({}).max_guaranteed_return_percent == 100.0
    g = ProfitabilityGate({"max_guaranteed_return_percent": 0})
    assert g.evaluate(_op(expected_return_pct=9999.0)).reason is not Reject.IMPLAUSIBLE_RETURN


# ===================== config integrity =====================
def test_duplicate_yaml_keys_are_refused():
    """config.yaml had two `execution:` blocks. YAML keeps only the last, so
    `execution.mode` was silently unreachable while looking correct in the
    file. A config that says one thing and does another must fail to load."""
    import tempfile

    from kalshibot.config import ConfigError, load_settings
    body = ("execution:\n  mode: demo\n\nexecution:\n"
            "  reprice_before_send: true\n")
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "config.yaml"
        path.write_text(body, encoding="utf-8")
        with pytest.raises(ConfigError) as err:
            load_settings(path)
    assert "duplicate key" in str(err.value)
    assert "execution" in str(err.value)


def test_shipped_config_has_no_duplicate_keys_and_resolves_mode():
    """The real config.yaml, loaded the way the bot loads it."""
    import yaml

    from kalshibot.config import _StrictLoader
    root = Path(__file__).resolve().parents[1]
    raw = yaml.load((root / "config.yaml").read_text(encoding="utf-8"),
                    Loader=_StrictLoader)
    assert raw["execution"]["mode"] == "demo"
    # the settings that were being discarded by the duplicate block
    for key in ("paper_fill_model", "max_unhedged_exposure_usd",
                "reprice_before_send"):
        assert key in raw["execution"], key


def test_unhedged_limit_is_coherent_with_the_size_cap():
    """A 25-contract structure can cost up to $25. An unhedged limit below
    that silently refuses every full-size trade, which looks like the bot
    finding nothing rather than a setting blocking it."""
    import yaml

    from kalshibot.config import _StrictLoader
    root = Path(__file__).resolve().parents[1]
    raw = yaml.load((root / "config.yaml").read_text(encoding="utf-8"),
                    Loader=_StrictLoader)
    cap = raw["limits"]["max_contracts_per_trade"]
    limit = raw["execution"]["max_unhedged_exposure_usd"]
    assert limit == 0 or limit >= cap * 0.99, (
        f"size cap {cap} contracts can leave up to ${cap * 0.99:.2f} unhedged, "
        f"above the ${limit:.2f} limit")


def test_the_forecast_launcher_flips_every_switch_it_needs_to():
    """START BOT (FORECAST ...).command passes --forecast. If that flag moved
    only one of the three switches, the bot would build the strategies and
    then silently reject everything they produced."""
    import yaml

    from arb import enable_forecasts
    from kalshibot.config import Settings, _StrictLoader
    root = Path(__file__).resolve().parents[1]
    raw = yaml.load((root / "config.yaml").read_text(encoding="utf-8"),
                    Loader=_StrictLoader)
    s = Settings(raw=raw, project_root=root, key_id="k",
                 private_key_path=Path("x"), kalshi_env="demo")

    assert s.get("forecast.enabled") is False, "shipped default must be off"
    assert s.get("profitability.allow_forecast") is False
    assert s.get("profitability.require_guaranteed") is True

    enable_forecasts(s)
    assert s.get("forecast.enabled") is True
    assert s.get("profitability.allow_forecast") is True
    assert s.get("profitability.require_guaranteed") is False

    # and the gate built from those settings must now accept a forecast
    g = ProfitabilityGate(s.section("profitability"))
    assert g.allow_forecast and not g.require_guaranteed


def test_config_file_on_disk_is_never_modified_by_the_flag():
    """--forecast is per-run. Closing the window must return the bot to
    structural arbitrage only."""
    import yaml

    from arb import enable_forecasts
    from kalshibot.config import Settings, _StrictLoader
    root = Path(__file__).resolve().parents[1]
    before = (root / "config.yaml").read_text(encoding="utf-8")
    raw = yaml.load(before, Loader=_StrictLoader)
    enable_forecasts(Settings(raw=raw, project_root=root, key_id="k",
                              private_key_path=Path("x"), kalshi_env="demo"))
    assert (root / "config.yaml").read_text(encoding="utf-8") == before


# ===================== the V2 order endpoint =====================
# Kalshi retired POST /portfolio/orders on 2026-09-16 with HTTP 410
# deprecated_v1_order_endpoint. The replacement is not a rename: V2 quotes ONE
# book - the YES book - so "buy NO" no longer exists as a concept. Buying NO
# at 57c is selling YES at 43c. Inverting that places the OPPOSITE trade at a
# price that still looks plausible in the log, which is the worst shape a bug
# can take here.

def test_buying_yes_is_a_bid_at_the_same_price():
    from kalshibot.client import KalshiClient
    assert KalshiClient.to_yes_book("buy", "yes", 40) == ("bid", 40)


def test_selling_yes_is_an_ask_at_the_same_price():
    from kalshibot.client import KalshiClient
    assert KalshiClient.to_yes_book("sell", "yes", 40) == ("ask", 40)


def test_buying_no_becomes_SELLING_yes_at_the_complement():
    from kalshibot.client import KalshiClient
    assert KalshiClient.to_yes_book("buy", "no", 57) == ("ask", 43)
    assert KalshiClient.to_yes_book("buy", "no", 2) == ("ask", 98)


def test_selling_no_becomes_BUYING_yes_at_the_complement():
    from kalshibot.client import KalshiClient
    assert KalshiClient.to_yes_book("sell", "no", 57) == ("bid", 43)


def test_the_translation_round_trips_for_every_price_and_side():
    """Whatever we send must describe the same position we meant."""
    from kalshibot.client import KalshiClient
    from kalshibot.clients import price_in_leg_terms
    for price in range(1, 100):
        for side in ("yes", "no"):
            book_side, yes_price = KalshiClient.to_yes_book("buy", side, price)
            assert price_in_leg_terms(side, yes_price) == price
            # buying YES is always a bid; buying NO is always an ask
            assert book_side == ("bid" if side == "yes" else "ask")


def test_the_translation_refuses_nonsense():
    from kalshibot.client import KalshiClient
    for bad in (("hold", "yes", 40), ("buy", "maybe", 40),
                ("buy", "yes", 0), ("buy", "yes", 100), ("buy", "no", -5)):
        with pytest.raises(ValueError):
            KalshiClient.to_yes_book(*bad)


def test_orders_go_to_the_v2_path_and_never_the_retired_one():
    from kalshibot.client import KalshiClient
    assert KalshiClient.ORDER_PATH_V2 == "/portfolio/events/orders"
    src = (Path(__file__).resolve().parents[1]
           / "kalshibot" / "client.py").read_text(encoding="utf-8")
    # Only the POST was retired. GET /portfolio/orders (list) and
    # GET /portfolio/orders/{id} are still current, so forbid the write, not
    # the path.
    assert '"POST", "/portfolio/orders"' not in src, (
        "POST to the retired order endpoint returns HTTP 410")
    assert 'self.ORDER_PATH_V2' in src


def test_the_v2_body_uses_fixed_point_dollars_not_cents():
    """43 cents is "0.4300". Sending 43 would be a 43-dollar order."""
    sent = {}

    class Capture(KalshiClientForBody):
        def _request(self, method, path, params=None, json_body=None, **kw):
            sent.update({"method": method, "path": path, "body": json_body})
            return {"order_id": "o1", "remaining_count": "0.00"}

    Capture("https://external-api.demo.kalshi.co/trade-api/v2").create_order(
        ticker="T", action="buy", side="no", count=17,
        limit_price_cents=57, client_order_id="c1")
    assert sent["path"] == "/portfolio/events/orders"
    body = sent["body"]
    assert body["side"] == "ask", "buying NO sells YES"
    assert body["price"] == "0.4300"
    assert body["count"] == "17"
    assert body["time_in_force"] and body["self_trade_prevention_type"]


# ===================== positions moved to fixed-point too =====================
# The bot reported "expected +25, exchange says +0" on trades that had filled.
# Kalshi moved the positions endpoint the same way it moved orders: position ->
# position_fp (fixed-point string), and cents -> _dollars. Reading only the old
# names made every position read as zero, so the bot could not see what it owned.

def test_a_v2_position_row_is_read_correctly():
    from kalshibot.models import Position
    p = Position.from_api({"ticker": "KXHIGHMIA-26SEP16-T93",
                           "position_fp": "25.00",
                           "market_exposure_dollars": "0.53",
                           "realized_pnl_dollars": "0",
                           "fees_paid_dollars": "0.01"})
    assert p.contracts == 25
    assert p.total_cost_cents == 53, "dollars must become cents, not stay dollars"


def test_a_negative_v2_position_is_a_no_position():
    from kalshibot.models import Position
    p = Position.from_api({"ticker": "KXHIGHCHI-26SEP17-B74.5",
                           "position_fp": "-11.00",
                           "market_exposure_dollars": "9.02"})
    assert p.contracts == -11 and p.side == "no"


def test_the_legacy_position_shape_still_reads():
    from kalshibot.models import Position
    p = Position.from_api({"ticker": "T", "position": 25,
                           "market_exposure": 53, "realized_pnl": 7})
    assert (p.contracts, p.total_cost_cents, p.realized_pnl_cents) == (25, 53, 7)


def test_dollar_and_cent_fields_are_never_confused():
    """A factor of 100 in an exposure number is a risk-limit failure."""
    from kalshibot.models import Position
    dollars = Position.from_api({"ticker": "T", "position_fp": "1",
                                 "market_exposure_dollars": "9.02"})
    cents = Position.from_api({"ticker": "T", "position": 1,
                               "market_exposure": 902})
    assert dollars.total_cost_cents == cents.total_cost_cents == 902


def test_an_unreadable_position_row_is_zero_and_not_a_crash():
    from kalshibot.models import Position
    assert Position.from_api({"ticker": "T", "surprise": 1}).contracts == 0


# ===================== the exchange closes overnight =====================
def test_a_closed_exchange_is_detected():
    from arb import exchange_is_open

    class Closed:
        def exchange_status(self):
            return {"exchange_active": True, "trading_active": False}

    open_now, why = exchange_is_open(Closed())
    assert not open_now and "trading_active" in why


def test_an_open_exchange_is_detected():
    from arb import exchange_is_open

    class Open:
        def exchange_status(self):
            return {"exchange_active": True, "trading_active": True}

    assert exchange_is_open(Open())[0]


def test_an_unreadable_status_fails_open():
    """A network blip must not idle the bot all day. Anything that actually
    reaches the exchange has its own guards."""
    from arb import exchange_is_open

    class Broken:
        def exchange_status(self):
            raise RuntimeError("connection reset")

    open_now, why = exchange_is_open(Broken())
    assert open_now and "unavailable" in why
