"""Kalshi RSA-PSS request signing.

Credentials are read ONLY from environment variables naming a key id and a PEM
file path, and ONLY when a caller explicitly asks for a credential scope:

    scope="prod"  -> KALSHI_PROD_API_KEY_ID / KALSHI_PROD_PRIVATE_KEY_PATH  (LIVE trading only)
    scope="demo"  -> KALSHI_DEMO_API_KEY_ID / KALSHI_DEMO_PRIVATE_KEY_PATH  (execution validator only)
    scope="data"  -> KALSHI_DATA_API_KEY_ID / KALSHI_DATA_PRIVATE_KEY_PATH  (optional websocket feed)

The key material is never logged, never returned by status(), never shown on
the dashboard. Missing or invalid credentials raise CredentialError; there is
no fallback to another scope or account.
"""
from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass
from pathlib import Path

SCOPES = {
    "prod": ("KALSHI_PROD_API_KEY_ID", "KALSHI_PROD_PRIVATE_KEY_PATH"),
    "demo": ("KALSHI_DEMO_API_KEY_ID", "KALSHI_DEMO_PRIVATE_KEY_PATH"),
    "data": ("KALSHI_DATA_API_KEY_ID", "KALSHI_DATA_PRIVATE_KEY_PATH"),
}


class CredentialError(RuntimeError):
    pass


def credentials_present(scope: str, env: dict[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    kid, path = SCOPES[scope]
    return bool(env.get(kid)) and bool(env.get(path)) and Path(env[path]).is_file()


@dataclass
class KalshiSigner:
    key_id: str
    _private_key: object

    def __repr__(self) -> str:  # never leak key material
        return f"KalshiSigner(key_id=***{self.key_id[-4:] if self.key_id else ''})"

    @classmethod
    def from_env(cls, scope: str, env: dict[str, str] | None = None) -> "KalshiSigner":
        env = os.environ if env is None else env
        if scope not in SCOPES:
            raise CredentialError(f"unknown credential scope {scope!r}")
        kid_var, path_var = SCOPES[scope]
        key_id = env.get(kid_var, "").strip()
        path = env.get(path_var, "").strip()
        if not key_id or not path:
            raise CredentialError(f"{scope} credentials not configured ({kid_var}/{path_var} unset)")
        p = Path(path)
        if not p.is_file():
            raise CredentialError(f"{path_var} does not point to a readable file")
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
        except ImportError as e:  # pragma: no cover
            raise CredentialError("install the 'live' extra (cryptography) to sign requests") from e
        try:
            key = serialization.load_pem_private_key(p.read_bytes(), password=None)
        except Exception as e:  # noqa: BLE001
            raise CredentialError(f"{path_var}: not a valid unencrypted PEM private key") from e
        if not isinstance(key, RSAPrivateKey):
            raise CredentialError("Kalshi keys must be RSA")
        return cls(key_id=key_id, _private_key=key)

    def headers(self, method: str, path: str, ts_ms: int | None = None) -> dict[str, str]:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        ts = str(ts_ms if ts_ms is not None else int(time.time() * 1000))
        msg = (ts + method.upper() + path.split("?")[0]).encode()
        sig = self._private_key.sign(  # type: ignore[attr-defined]
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    def __call__(self, method: str, path: str) -> dict[str, str]:
        return self.headers(method, path)
