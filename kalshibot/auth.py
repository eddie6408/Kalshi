"""Kalshi request signing.

Kalshi authenticates every private request with three headers:

    KALSHI-ACCESS-KEY        your Key ID (a UUID)
    KALSHI-ACCESS-TIMESTAMP  current time in MILLISECONDS
    KALSHI-ACCESS-SIGNATURE  base64( RSA-PSS-SHA256( timestamp + METHOD + path ) )

Two details people get wrong and then spend an afternoon debugging:

  1. The signed `path` is the FULL path from the API root, i.e. it starts with
     "/trade-api/v2/..." - NOT just "/portfolio/orders".
  2. Query parameters are EXCLUDED from the signed string. If you request
     /trade-api/v2/portfolio/orders?limit=5 you sign /trade-api/v2/portfolio/orders
"""
from __future__ import annotations

import base64
import time
from pathlib import Path

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class AuthError(RuntimeError):
    pass


def load_private_key(path: str | Path) -> rsa.RSAPrivateKey:
    path = Path(path).expanduser()
    if not path.exists():
        raise AuthError(f"Private key file not found: {path}")
    data = path.read_bytes()
    try:
        key = serialization.load_pem_private_key(data, password=None, backend=default_backend())
    except Exception as exc:  # noqa: BLE001
        raise AuthError(
            f"Could not read {path} as an unencrypted PEM private key. "
            "Make sure you saved the whole block including the "
            "'-----BEGIN RSA PRIVATE KEY-----' line."
        ) from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise AuthError("Kalshi requires an RSA private key.")
    return key


class Signer:
    """Turns (method, path) into the three Kalshi auth headers."""

    def __init__(self, key_id: str, private_key: rsa.RSAPrivateKey):
        if not key_id:
            raise AuthError("Empty Kalshi Key ID.")
        self.key_id = key_id
        self._key = private_key

    @classmethod
    def from_settings(cls, settings) -> "Signer":
        return cls(settings.key_id, load_private_key(settings.private_key_path))

    def sign(self, method: str, full_path: str) -> dict[str, str]:
        # Defensive: strip any query string that slipped through.
        full_path = full_path.split("?", 1)[0]
        timestamp_ms = str(int(time.time() * 1000))
        message = f"{timestamp_ms}{method.upper()}{full_path}".encode("utf-8")
        signature = self._key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
