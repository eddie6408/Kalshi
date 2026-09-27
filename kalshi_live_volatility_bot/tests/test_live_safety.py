"""LIVE-mode authorization, credential handling, live execution against a mock Kalshi API,
duplicate-order protection, unknown-state fail-closed, live reconciliation, log redaction."""
import json

import httpx
import pytest

from klvb.config import ConfigError, TradingMode, load_config
from klvb.engine.fees import FeeModel
from klvb.execution.factory import LiveModeUnavailable, build_execution
from klvb.execution.kalshi_auth import CredentialError, KalshiSigner, credentials_present
from klvb.execution.live import KalshiLiveExecution
from klvb.execution.paper import PaperExecution, ShadowExecution
from klvb.models import Direction, MarketMeta, Order, OrderStatus, Position
from klvb.monitoring.logs import redact
from klvb.portfolio.reconcile import reconcile_live
from klvb.readiness import AUTH_PHRASE, evaluate


@pytest.fixture
def pem(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    p = tmp_path / "k.pem"
    p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    return p


def test_paper_is_default_and_never_loads_credentials(cfg, db, fees, pem):
    assert cfg.mode is TradingMode.PAPER
    env = {"KALSHI_PROD_API_KEY_ID": "abc", "KALSHI_PROD_PRIVATE_KEY_PATH": str(pem)}
    ex = build_execution(cfg, fees, None, db, env)
    assert isinstance(ex, PaperExecution) and not ex.live
    shadow = load_config(env={"TRADING_MODE": "SHADOW", "KLVB_DATA_DIR": str(pem.parent)})
    ex2 = build_execution(shadow, fees, None, db, env)
    assert isinstance(ex2, ShadowExecution) and not ex2.live


def test_live_refused_without_readiness(tmp_path, db, fees, pem):
    live = load_config(env={"TRADING_MODE": "LIVE", "KLVB_DATA_DIR": str(tmp_path)})
    env = {"KALSHI_PROD_API_KEY_ID": "abc", "KALSHI_PROD_PRIVATE_KEY_PATH": str(pem),
           "LIVE_TRADING_AUTHORIZED_BY": "someone", "LIVE_TRADING_ACKNOWLEDGEMENT": AUTH_PHRASE}
    with pytest.raises(LiveModeUnavailable) as e:
        build_execution(live, fees, None, db, env)
    assert "PAPER DATA SUFFICIENT" in str(e.value)


def test_live_allowed_only_when_every_check_passes(tmp_path, db, fees, pem, monkeypatch):
    live = load_config(env={"TRADING_MODE": "LIVE", "KLVB_DATA_DIR": str(tmp_path)})
    env = {"KALSHI_PROD_API_KEY_ID": "abc", "KALSHI_PROD_PRIVATE_KEY_PATH": str(pem)}
    import klvb.readiness as rd
    from klvb.readiness import ReadinessReport
    monkeypatch.setattr(rd, "evaluate", lambda *a, **k: ReadinessReport({"ALL": (True, "ok")}))
    ex = build_execution(live, fees, None, db, env)
    assert isinstance(ex, KalshiLiveExecution) and ex.live
    # missing credentials -> refused even when readiness passes; no fallback
    with pytest.raises(LiveModeUnavailable):
        build_execution(live, fees, None, db, {})


def test_readiness_checklist_all_fail_on_fresh_install(cfg, db):
    rep = evaluate(cfg, db, env={})
    assert not rep.ready
    assert len(rep.checks) == 12   # the spec-52 checklist
    assert not rep.checks["HUMAN AUTHORIZATION"][0]
    ok = evaluate(cfg, db, env={"LIVE_TRADING_AUTHORIZED_BY": "x", "LIVE_TRADING_ACKNOWLEDGEMENT": AUTH_PHRASE})
    assert ok.checks["HUMAN AUTHORIZATION"][0] and not ok.ready


def test_invalid_mode_rejected():
    with pytest.raises(ConfigError):
        load_config(env={"TRADING_MODE": "YOLO"})


def test_signer_never_leaks_and_rejects_missing(pem):
    with pytest.raises(CredentialError):
        KalshiSigner.from_env("prod", {})
    s = KalshiSigner.from_env("demo", {"KALSHI_DEMO_API_KEY_ID": "key-12345678", "KALSHI_DEMO_PRIVATE_KEY_PATH": str(pem)})
    assert "key-1234" not in repr(s)
    h = s.headers("GET", "/trade-api/v2/portfolio/balance", ts_ms=1)
    assert set(h) == {"KALSHI-ACCESS-KEY", "KALSHI-ACCESS-SIGNATURE", "KALSHI-ACCESS-TIMESTAMP"}
    assert credentials_present("demo", {"KALSHI_DEMO_API_KEY_ID": "k", "KALSHI_DEMO_PRIVATE_KEY_PATH": str(pem)})
    assert not credentials_present("prod", {})


def test_log_redaction():
    text = redact('{"KALSHI-ACCESS-SIGNATURE": "abcd", "api_key": "sekret"} -----BEGIN PRIVATE KEY-----xx-----END PRIVATE KEY-----')
    assert "abcd" not in text and "sekret" not in text and "BEGIN PRIVATE KEY" not in text


class MockKalshi:
    """Minimal in-memory Kalshi portfolio API."""

    def __init__(self, fail_submit=False):
        self.orders: dict[str, dict] = {}
        self.fills: list[dict] = []
        self.positions: list[dict] = []
        self.fail_submit = fail_submit
        self.posts = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/trade-api/v2", "")
        assert "KALSHI-ACCESS-SIGNATURE" in request.headers
        if request.method == "POST" and path == "/portfolio/orders":
            self.posts += 1
            body = json.loads(request.content)
            if any(o["client_order_id"] == body["client_order_id"] for o in self.orders.values()):
                return httpx.Response(409, json={"error": "duplicate client_order_id"})
            oid = f"x{len(self.orders) + 1}"
            filled = min(body["count"], 4)
            o = {"order_id": oid, "client_order_id": body["client_order_id"], "ticker": body["ticker"],
                 "status": "canceled" if filled < body["count"] else "executed", "fill_count": filled,
                 "initial_count": body["count"], "taker_fees_dollars": "0.0700"}
            self.orders[oid] = o
            if filled:
                self.fills.append({"fill_id": f"f{oid}", "order_id": oid, "side": body["side"],
                                   "action": body["action"], "count": filled, "yes_price_fixed": "0.5000",
                                   "no_price_fixed": "0.5000", "is_taker": True,
                                   "created_time": "2026-09-27T00:00:00Z"})
            if self.fail_submit:
                raise httpx.ReadTimeout("simulated timeout after the exchange accepted the order")
            return httpx.Response(201, json={"order": o})
        if path == "/portfolio/fills":
            oid = request.url.params.get("order_id")
            return httpx.Response(200, json={"fills": [f for f in self.fills if not oid or f["order_id"] == oid]})
        if path == "/portfolio/orders" and request.method == "GET":
            st = request.url.params.get("status")
            return httpx.Response(200, json={"orders": [o for o in self.orders.values() if not st or o["status"] == st]})
        if path == "/portfolio/balance":
            return httpx.Response(200, json={"balance": 10000})
        if path == "/portfolio/positions":
            return httpx.Response(200, json={"market_positions": self.positions})
        return httpx.Response(404, json={})


def live_exec(cfg, pem, mock):
    signer = KalshiSigner.from_env("demo", {"KALSHI_DEMO_API_KEY_ID": "k", "KALSHI_DEMO_PRIVATE_KEY_PATH": str(pem)})
    return KalshiLiveExecution(cfg.live, FeeModel(), signer, "https://demo.test/trade-api/v2", env_name="DEMO",
                               transport=httpx.MockTransport(mock.handler), max_rps=1000)


def order(count=10, coid=None):
    o = Order(env="DEMO", ticker="M", side="yes", action="buy", count=count, limit_price=50.0, purpose="ENTRY")
    if coid:
        o.client_order_id = coid
    return o


async def test_live_submit_partial_fill_and_fees(cfg, pem):
    mock = MockKalshi()
    ex = live_exec(cfg, pem, mock)
    o = order(10)
    evs = await ex.submit(o, MarketMeta(ticker="M"), 0)
    assert o.exchange_order_id == "x1" and o.status is OrderStatus.CANCELED
    assert o.filled == 4 and evs[0].fills[0].exchange_fill_id == "fx1"
    assert o.fees == 7.0
    await ex.close()


async def test_duplicate_client_order_id_is_not_double_submitted(cfg, pem):
    mock = MockKalshi()
    ex = live_exec(cfg, pem, mock)
    o1 = order(2)
    await ex.submit(o1, MarketMeta(ticker="M"), 0)
    dup = order(2, coid=o1.client_order_id)
    await ex.submit(dup, MarketMeta(ticker="M"), 0)
    assert dup.status is OrderStatus.UNKNOWN          # resolved later, never blindly retried
    assert len(mock.orders) == 1
    await ex.close()


async def test_timeout_leaves_unknown_and_resolver_finds_order(cfg, pem):
    mock = MockKalshi(fail_submit=True)
    ex = live_exec(cfg, pem, mock)
    o = order(3)
    await ex.submit(o, MarketMeta(ticker="M"), 0)
    assert o.status is OrderStatus.UNKNOWN and mock.posts == 1   # no automatic retry
    ev = await ex.resolve_unknown(o, MarketMeta(ticker="M"), 1)
    assert o.status is OrderStatus.FILLED and o.exchange_order_id == "x1" and o.filled == 3
    assert ev.fills and mock.posts == 1
    await ex.close()


async def test_live_reconciliation_detects_mismatch_and_foreign_orders(cfg, pem, db):
    mock = MockKalshi()
    mock.positions = [{"ticker": "M", "position": 5}]
    mock.orders["zz"] = {"order_id": "zz", "ticker": "Q", "status": "resting", "client_order_id": "other"}
    ex = live_exec(cfg, pem, mock)
    res = await reconcile_live(ex, [], db, "DEMO")
    assert not res.ok
    assert any("POSITION MISMATCH M" in i for i in res.issues)
    assert any("UNKNOWN RESTING ORDER zz" in i for i in res.issues)
    held = Position(env="DEMO", ticker="M", event_ticker="E", side="yes", direction=Direction.UP, strategy="DIP",
                    strategy_version="V", signal_id=None, target=1, stop=1, max_hold_seconds=1, qty=5)
    mock.orders.clear()
    assert (await reconcile_live(ex, [held], db, "DEMO")).ok
    await ex.close()
