"""Real-time PRODUCTION order books over Kalshi's WebSocket - market data only.

Why: the paper account, the market-vs-model checks, the arbitrage check and
the WX research quotes all read production prices by polling (every 180s per
market). Kalshi's socket pushes every book change, but it requires a signed
connection even for public data. The operator created a production API key
for this purpose (2026-09-29) with an explicit rule: market data only; the
execution client stays locked to demo; trading with this key needs their
explicit command and a re-confirmation.

How that rule is enforced here, structurally:

  1. The key is loaded ONLY by `load_market_data_signer`, from
     /opt/kalshi/secrets (outside the repo, mode 600). Nothing else in the
     codebase reads that path; a test pins it.
  2. The signer signs one thing: the socket handshake (GET /trade-api/ws/v2).
     It is not returned to, stored on, or reachable from any client.
  3. The socket may only be opened at the exact production market-data URL
     (env_guard.assert_prod_market_data_ws).
  4. Every message sent on it is checked: subscription commands for
     orderbook_delta / ticker / trade only
     (env_guard.assert_market_data_command). There is no order channel to
     send on and no REST client in this module.
  5. Books land in their OWN MarketState, never the demo state the trading
     engine reads - production depth cannot leak into a demo decision.
  6. assert_demo_url and every trading path are unchanged.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from .auth import Signer, load_private_key
from .env_guard import (PROD_MARKET_DATA_WS, assert_market_data_command,
                        assert_prod_market_data_ws)
from .wsfeed import FeedConfig, KalshiFeed

log = logging.getLogger(__name__)

DEFAULT_SECRETS_DIR = "/opt/kalshi/secrets"
KEY_FILE = "prod_marketdata.pem"
ID_FILE = "prod_marketdata.env"
ID_VAR = "KALSHI_PROD_MD_KEY_ID"
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class _HandshakeSigner:
    """Wraps the signer so the only thing it will sign is the socket
    handshake. Asked to sign anything else, it refuses."""

    def __init__(self, signer: Signer, path: str):
        self.__signer = signer
        self.__path = path

    def sign(self, method: str, full_path: str) -> dict[str, str]:
        if method.upper() != "GET" or full_path.split("?")[0] != self.__path:
            from .env_guard import ProductionBlocked
            raise ProductionBlocked(
                f"REFUSED: the production market-data key signs only the socket "
                f"handshake (GET {self.__path}), not {method} {full_path}.")
        return self.__signer.sign(method, full_path)


def load_market_data_signer(secrets_dir: str | Path = DEFAULT_SECRETS_DIR,
                            url: str = PROD_MARKET_DATA_WS[0]):
    """(handshake-only signer, None) or (None, why not). Never raises, never
    logs key material."""
    d = Path(secrets_dir)
    pem, env = d / KEY_FILE, d / ID_FILE
    if not pem.exists() or not env.exists():
        return None, f"no production market-data key in {d}"
    try:
        mode = pem.stat().st_mode & 0o077
        if mode:
            return None, f"{pem} is readable by group/others; chmod 600 it"
        key_id = ""
        for line in env.read_text().splitlines():
            if line.startswith(ID_VAR + "="):
                key_id = line.split("=", 1)[1].strip().strip('"').strip("'")
        if not _UUID.match(key_id):
            return None, f"{ID_VAR} in {env} is missing or not a key id"
        signer = Signer(key_id, load_private_key(str(pem)))
    except Exception as exc:  # noqa: BLE001
        return None, f"production market-data key unusable: {type(exc).__name__}"
    from urllib.parse import urlparse
    return _HandshakeSigner(signer, urlparse(url).path or "/trade-api/ws/v2"), None


class ProductionMarketDataFeed(KalshiFeed):
    """KalshiFeed pointed at production, fenced to market data."""

    def __init__(self, state, signer, cfg: FeedConfig):
        super().__init__(state, signer, cfg, env="production-market-data")

    def _assert_url(self) -> None:
        assert_prod_market_data_ws(self.cfg.url)

    def _send(self, payload: dict) -> None:
        assert_market_data_command(payload)
        super()._send(payload)
