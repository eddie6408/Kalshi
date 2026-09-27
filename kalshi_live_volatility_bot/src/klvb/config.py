"""Configuration loading.

Order of precedence (last wins):
  1. config/default.toml (checked in)
  2. config/local.toml or the file named by KLVB_CONFIG (git-ignored)
  3. a small set of environment variables (operational switches only)

Credentials are NEVER read here. Only `execution.live` / `execution.kalshi_auth`
read credential env vars, and only when LIVE (or the explicit demo validator)
is requested. That keeps PAPER/SHADOW physically unable to load a production key.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "default.toml"
LOCAL_CONFIG_PATH = PROJECT_ROOT / "config" / "local.toml"


class TradingMode(str, Enum):
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    LIVE = "LIVE"

    @property
    def simulated(self) -> bool:
        return self is not TradingMode.LIVE


class ConfigError(ValueError):
    pass


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _env_bool(value: str | None) -> bool | None:
    if value is None or value.strip() == "":
        return None
    v = value.strip().lower()
    if v in {"1", "true", "yes", "on"}:
        return True
    if v in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"not a boolean: {value!r}")


class Section:
    """Attribute access over a config dict (read-only by convention)."""

    def __init__(self, data: dict[str, Any], path: str = ""):
        self._data = data
        self._path = path

    def __getattr__(self, item: str) -> Any:
        if item.startswith("_"):
            raise AttributeError(item)
        try:
            v = self._data[item]
        except KeyError as e:
            raise AttributeError(f"missing config key {self._path}{item}") from e
        if isinstance(v, dict):
            return Section(v, f"{self._path}{item}.")
        return v

    def get(self, item: str, default: Any = None) -> Any:
        v = self._data.get(item, default)
        return Section(v, f"{self._path}{item}.") if isinstance(v, dict) else v

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)


@dataclass
class Config:
    raw: dict[str, Any]
    source_files: list[str]

    def __getattr__(self, item: str) -> Any:
        if item in ("raw", "source_files") or item.startswith("_"):
            raise AttributeError(item)
        return Section(self.raw, "").__getattr__(item)

    @property
    def mode(self) -> TradingMode:
        return TradingMode(self.raw["app"]["trading_mode"])

    @property
    def kill_switch_env(self) -> bool:
        return bool(self.raw["app"].get("kill_switch", False))

    @property
    def data_dir(self) -> Path:
        p = Path(self.raw["app"]["data_dir"])
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def log_dir(self) -> Path:
        p = Path(self.raw["app"]["log_dir"])
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def db_path(self) -> Path:
        return self.data_dir / "klvb.sqlite3"

    @property
    def kill_switch_file(self) -> Path:
        return self.data_dir / "KILL_SWITCH"

    def fingerprint(self) -> str:
        """Stable hash of the effective config (recorded with every run)."""
        blob = json.dumps(self.raw, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()[:16]

    def strategy_params(self, name: str) -> dict[str, Any]:
        return copy.deepcopy(self.raw["strategies"][name.lower()])


def load_config(path: str | os.PathLike | None = None, env: dict[str, str] | None = None,
                overrides: dict[str, Any] | None = None) -> Config:
    env = dict(os.environ) if env is None else env
    with open(DEFAULT_CONFIG_PATH, "rb") as f:
        raw = tomllib.load(f)
    sources = [str(DEFAULT_CONFIG_PATH)]

    local = Path(path) if path else (Path(env["KLVB_CONFIG"]) if env.get("KLVB_CONFIG") else LOCAL_CONFIG_PATH)
    if local.exists():
        with open(local, "rb") as f:
            raw = _deep_merge(raw, tomllib.load(f))
        sources.append(str(local))
    elif path:
        raise ConfigError(f"config file not found: {local}")

    if overrides:
        raw = _deep_merge(raw, overrides)

    # --- operational env overrides (never credentials) ---
    if env.get("TRADING_MODE"):
        raw["app"]["trading_mode"] = env["TRADING_MODE"].strip().upper()
    ks = _env_bool(env.get("KILL_SWITCH"))
    if ks is not None:
        raw["app"]["kill_switch"] = ks
    if env.get("KLVB_DATA_DIR"):
        raw["app"]["data_dir"] = env["KLVB_DATA_DIR"]
    if env.get("KLVB_LOG_DIR"):
        raw["app"]["log_dir"] = env["KLVB_LOG_DIR"]
    if env.get("KLVB_DASHBOARD_PORT"):
        raw["dashboard"]["port"] = int(env["KLVB_DASHBOARD_PORT"])

    cfg = Config(raw=raw, source_files=sources)
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    mode = cfg.raw["app"].get("trading_mode", "PAPER")
    if mode not in TradingMode.__members__:
        raise ConfigError(f"TRADING_MODE must be PAPER, SHADOW or LIVE (got {mode!r})")
    r = cfg.raw["risk"]
    for key in ("max_daily_loss", "max_risk_per_trade", "max_account_exposure", "max_position_contracts"):
        if not r.get(key) or r[key] <= 0:
            raise ConfigError(f"risk.{key} must be > 0")
    if r["on_daily_loss"] not in ("manage", "flatten"):
        raise ConfigError("risk.on_daily_loss must be 'manage' or 'flatten'")
    if cfg.raw["kalshi"]["max_requests_per_second"] > 15:
        raise ConfigError("kalshi.max_requests_per_second above 15 risks starving the other bot on this host")
    for name in cfg.raw["strategies"]["enabled"]:
        if name.lower() not in cfg.raw["strategies"]:
            raise ConfigError(f"strategy {name} enabled but has no [strategies.{name.lower()}] section")
        if cfg.raw["strategies"][name.lower()].get("entry_style", "taker") not in ("taker", "maker"):
            raise ConfigError(f"strategies.{name.lower()}.entry_style must be taker or maker")
    host = cfg.raw["dashboard"]["host"]
    if host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get("KLVB_DASHBOARD_TOKEN"):
        raise ConfigError("dashboard bound to a non-loopback host requires KLVB_DASHBOARD_TOKEN")
