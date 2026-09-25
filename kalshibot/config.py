"""Configuration loading. Secrets come from .env, behaviour from config.yaml."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*_a, **_kw):
        return False


# Official Kalshi endpoints (docs.kalshi.com -> Getting Started -> API Environments)
ENDPOINTS = {
    "prod": {
        "rest": "https://external-api.kalshi.com/trade-api/v2",
        "ws": "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
    },
    "demo": {
        "rest": "https://external-api.demo.kalshi.co/trade-api/v2",
        "ws": "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
    },
}
# Legacy alias that still resolves for many accounts. Used only as a fallback
# if the primary production host cannot be reached.
PROD_REST_FALLBACK = "https://api.elections.kalshi.com/trade-api/v2"


class ConfigError(RuntimeError):
    pass


class _StrictLoader(yaml.SafeLoader):
    """A YAML loader that refuses duplicate keys.

    Standard YAML silently keeps the LAST of two identical keys. config.yaml
    had two `execution:` blocks; the second won, so `execution.mode` was never
    read and every guard that depended on it was unreachable while looking
    perfectly correct in the file. A config that says one thing and does
    another is worse than a config that fails to load.
    """


def _no_duplicate_keys(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            mark = key_node.start_mark
            raise ConfigError(
                f"duplicate key {key!r} in config.yaml at line {mark.line + 1}. "
                "YAML would silently keep only the last one, so the earlier "
                "block would be ignored. Merge them into a single block."
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys)


@dataclass
class Settings:
    raw: dict[str, Any]
    project_root: Path
    key_id: str
    private_key_path: Path
    kalshi_env: str
    anthropic_api_key: str | None = None

    # ---- convenience accessors -------------------------------------
    def section(self, name: str) -> dict[str, Any]:
        return self.raw.get(name, {}) or {}

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def mode(self) -> str:
        return str(self.get("execution.mode", "paper")).lower()

    @property
    def rest_base(self) -> str:
        # An override is honoured ONLY if env_guard still considers it a demo
        # host, which refuses every production hostname unconditionally. This
        # exists so integration tests can point at a mock exchange on
        # loopback; it cannot be used to reach real money.
        override = os.getenv("KALSHI_REST_BASE_OVERRIDE")
        if override:
            from .env_guard import assert_demo_url
            assert_demo_url(override, "KALSHI_REST_BASE_OVERRIDE")
            return override.rstrip("/")
        return ENDPOINTS[self.kalshi_env]["rest"]

    @property
    def ws_base(self) -> str:
        return ENDPOINTS[self.kalshi_env]["ws"]

    def path(self, dotted: str, default: str) -> Path:
        p = Path(str(self.get(dotted, default))).expanduser()
        return p if p.is_absolute() else self.project_root / p


def load_settings(config_path: str | Path = "config.yaml") -> Settings:
    root = Path(config_path).resolve().parent
    load_dotenv(root / ".env")

    with open(config_path, "r", encoding="utf-8") as fh:
        raw = yaml.load(fh, Loader=_StrictLoader) or {}

    env = (os.getenv("KALSHI_ENV") or "demo").strip().lower()
    if env not in ENDPOINTS:
        raise ConfigError(f"KALSHI_ENV must be 'demo' or 'prod', got {env!r}")

    key_id = (os.getenv("KALSHI_KEY_ID") or "").strip()
    key_path_raw = (os.getenv("KALSHI_PRIVATE_KEY_PATH") or "").strip()

    # A relative key path is resolved against the project folder, so the same
    # .env works on your Mac, on a VPS, and anywhere else the folder is copied.
    if key_path_raw:
        key_path = Path(key_path_raw).expanduser()
        if not key_path.is_absolute():
            key_path = (root / key_path).resolve()
    else:
        key_path = Path("")

    settings = Settings(
        raw=raw,
        project_root=root,
        key_id=key_id,
        private_key_path=key_path,
        kalshi_env=env,
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
    )
    _validate(settings)
    return settings


def _validate(s: Settings) -> None:
    mode = s.mode
    if mode not in ("paper", "demo", "live"):
        raise ConfigError(f"execution.mode must be paper|demo|live, got {mode!r}")

    # Live-money guards come first, so the message you get is the one that
    # matters most.
    if mode == "live":
        if not s.get("safety.i_understand_this_trades_real_money", False):
            raise ConfigError(
                "execution.mode is 'live' but safety.i_understand_this_trades_real_money "
                "is false. Refusing to trade real money."
            )
        if s.kalshi_env != "prod":
            raise ConfigError(
                "execution.mode 'live' requires KALSHI_ENV=prod in .env. "
                "A demo key cannot trade real money, and vice versa."
            )

    # Credentials are only strictly required once we are sending orders.
    needs_creds = mode in ("demo", "live")
    if needs_creds or s.key_id:
        if not s.key_id:
            raise ConfigError("KALSHI_KEY_ID is not set in .env")
        if not s.private_key_path or not s.private_key_path.exists():
            raise ConfigError(
                f"Private key not found at {s.private_key_path!r}. "
                "Set KALSHI_PRIVATE_KEY_PATH in .env to the .pem file from Kalshi."
            )

    r = s.section("risk")
    if r.get("max_cost_per_trade_usd", 0) > r.get("max_exposure_per_market_usd", 0):
        raise ConfigError("risk.max_cost_per_trade_usd exceeds max_exposure_per_market_usd")
    if r.get("max_exposure_per_market_usd", 0) > r.get("max_total_open_exposure_usd", 0):
        raise ConfigError("risk.max_exposure_per_market_usd exceeds max_total_open_exposure_usd")
    kf = r.get("kelly_fraction", 0.25)
    if not 0 < kf <= 1:
        raise ConfigError("risk.kelly_fraction must be in (0, 1]")
