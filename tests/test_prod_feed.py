"""The production market-data feed: the operator's key reads prices, nothing else.

Pinned: the socket opens only at the exact production market-data URL; only
subscription commands for orderbook_delta / ticker / trade may be sent; the
key signs only the handshake; a missing, exposed or placeholder key turns the
feed off; nothing outside prod_feed.py and arb.py knows where the key is; the
demo feed still refuses production; the production watch serves the live
book only while the socket is up and the book trusted.
"""
import os
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshibot.env_guard import (ProductionBlocked, assert_market_data_command,
                                 assert_prod_market_data_ws)
from kalshibot.market_state import MarketState
from kalshibot.prod_feed import ProductionMarketDataFeed, load_market_data_signer
from kalshibot.prodwatch import ProductionWatch
from kalshibot.wsfeed import FeedConfig, KalshiFeed

ROOT = Path(__file__).resolve().parents[1]
PROD_WS = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
KID = "0f0e0d0c-0b0a-4908-8706-050403020100"


def _secrets(key_id=KID, mode=0o600):
    d = Path(tempfile.mkdtemp())
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (d / "prod_marketdata.pem").write_bytes(k.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    os.chmod(d / "prod_marketdata.pem", mode)
    (d / "prod_marketdata.env").write_text(f"KALSHI_PROD_MD_KEY_ID={key_id}\n")
    return d


def test_the_socket_opens_only_at_the_production_market_data_url():
    assert_prod_market_data_ws(PROD_WS)
    for bad in ("wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
                "https://external-api.kalshi.com/trade-api/v2",
                "wss://evil.example.com/trade-api/ws/v2",
                "wss://external-api-ws.kalshi.com/trade-api/v2/portfolio/orders"):
        with pytest.raises(ProductionBlocked):
            assert_prod_market_data_ws(bad)


def test_only_market_data_subscriptions_may_be_sent():
    assert_market_data_command({"cmd": "subscribe", "params": {"channels": ["orderbook_delta"]}})
    assert_market_data_command({"cmd": "subscribe", "params": {"channels": ["ticker", "trade"]}})
    for bad in ({"cmd": "subscribe", "params": {"channels": ["fill"]}},
                {"cmd": "subscribe", "params": {"channels": ["orderbook_delta", "market_positions"]}},
                {"cmd": "subscribe", "params": {"channels": ["order_group_updates"]}},
                {"cmd": "create_order", "params": {}},
                {"cmd": "place_order"}, {}):
        with pytest.raises(ProductionBlocked):
            assert_market_data_command(bad)


def test_the_feed_refuses_a_private_channel_before_it_reaches_the_socket():
    sent = []
    f = ProductionMarketDataFeed(MarketState(None), None, FeedConfig(enabled=True, url=PROD_WS))
    f.ws = type("WS", (), {"send_text": lambda self, s: sent.append(s)})()
    f._send({"id": 1, "cmd": "subscribe", "params": {"channels": ["orderbook_delta"],
                                                      "market_tickers": ["X"]}})
    with pytest.raises(ProductionBlocked):
        f._send({"id": 2, "cmd": "subscribe", "params": {"channels": ["fill"]}})
    assert len(sent) == 1
    f._assert_url()                                            # production URL: allowed here
    with pytest.raises(ProductionBlocked):
        KalshiFeed(MarketState(None), None, FeedConfig(enabled=True, url=PROD_WS))._assert_url()


def test_the_key_signs_only_the_handshake():
    signer, why = load_market_data_signer(_secrets(), PROD_WS)
    assert signer is not None, why
    h = signer.sign("GET", "/trade-api/ws/v2")
    assert h["KALSHI-ACCESS-KEY"] == KID and h["KALSHI-ACCESS-SIGNATURE"]
    for method, path in (("POST", "/trade-api/v2/portfolio/orders"),
                         ("GET", "/trade-api/v2/portfolio/balance"),
                         ("DELETE", "/trade-api/ws/v2")):
        with pytest.raises(ProductionBlocked):
            signer.sign(method, path)
    assert not hasattr(signer, "key_id") and not hasattr(signer, "_key")


def test_a_missing_exposed_or_placeholder_key_turns_the_feed_off():
    assert load_market_data_signer(Path(tempfile.mkdtemp()))[0] is None
    s, why = load_market_data_signer(_secrets(mode=0o644))
    assert s is None and "chmod 600" in why
    s, why = load_market_data_signer(_secrets(key_id="PASTE-KEY-ID-HERE"))
    assert s is None and "not a key id" in why


def test_nothing_else_knows_where_the_key_is():
    allowed = {"kalshibot/prod_feed.py", "arb.py"}
    for p in list(ROOT.glob("kalshibot/**/*.py")) + list(ROOT.glob("scripts/**/*.py")) + [ROOT / "arb.py"]:
        rel = str(p.relative_to(ROOT))
        src = p.read_text()
        if "prod_marketdata" in src or "load_market_data_signer" in src:
            assert rel in allowed, rel
    for rel in ("kalshibot/clients.py", "kalshibot/arb_engine.py", "kalshibot/execution_manager.py",
                "kalshibot/position_manager.py", "kalshibot/auth.py", "kalshibot/config.py"):
        src = (ROOT / rel).read_text()
        assert "prod_feed" not in src and "/opt/kalshi/secrets" not in src, rel


def test_the_watch_serves_the_live_book_only_while_trusted_and_connected():
    live = MarketState(None)
    up = {"v": True}
    live.feed_live = lambda: up["v"]
    live.apply_snapshot("T", [(40, 10)], [(55, 5)])
    w = ProductionWatch(reader=None)
    w.live_state = live
    q = w.latest("T")
    assert q is not None and q.yes_bid == 40 and q.yes_ask == 45 and w.live_hits == 1
    up["v"] = False
    assert w.latest("T") is None                 # socket down: the polled book (none here)
    up["v"] = True
    live.invalidate("T", "gap")
    assert w.latest("T") is None                 # untrusted: not served
