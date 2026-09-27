"""Configuration: config/default.toml, merged with config/local.toml, plus .env.

The trading mode comes ONLY from the TRADING_MODE environment variable and
defaults to PAPER. Nothing in the code ever writes it, so the bot cannot
promote itself to LIVE (spec sections 14, 53, 60).
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

MODES = ("PAPER", "SHADOW", "LIVE")


class ConfigError(Exception):
    pass


class Section:
    """Read-only attribute view over a nested dict."""

    def __init__(self, data: dict, path: str = ""):
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_path", path)

    def __getattr__(self, name: str) -> Any:
        try:
            v = self._data[name]
        except KeyError:
            raise AttributeError(f"config key {self._path}{name} missing") from None
        return Section(v, f"{self._path}{name}.") if isinstance(v, dict) else v

    def __setattr__(self, name, value):
        raise AttributeError("config is read-only")

    def __getitem__(self, name: str) -> Any:
        return self.__getattr__(name)

    def __contains__(self, name: str) -> bool:
        return name in self._data

    def get(self, name: str, default: Any = None) -> Any:
        return self.__getattr__(name) if name in self._data else default

    def keys(self):
        return self._data.keys()

    def to_dict(self) -> dict:
        return copy.deepcopy(self._data)


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env parser (KEY=VALUE, # comments, optional quotes).

    Values are NOT exported into os.environ; the caller decides. Real
    environment variables win over the file.
    """
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[7:].strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k] = v
    return out


class Config:
    def __init__(self, data: dict, env: dict[str, str], root: Path = ROOT):
        self.root = root
        self.data = data
        self.env = env
        self.s = Section(data)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.s, name)

    # ---- mode -------------------------------------------------------------
    @property
    def mode(self) -> str:
        raw = (self.env.get("TRADING_MODE") or "PAPER").strip().upper()
        if raw not in MODES:
            raise ConfigError(f"TRADING_MODE={raw!r} is not one of {MODES}")
        return raw

    @property
    def kill_switch_env(self) -> bool:
        return (self.env.get("KILL_SWITCH") or "").strip().lower() in ("1", "true", "yes", "on")

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.root / p

    # ---- versioning -------------------------------------------------------
    def config_hash(self) -> str:
        blob = json.dumps(self.data, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()[:12]

    def strategy_hash(self, name: str) -> str:
        blob = json.dumps(self.data["strategies"][name], sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:8]

    def strategy_version(self, name: str) -> str:
        """Version label + parameter hash, so edited parameters never pool
        their results with the old ones (spec section 64)."""
        st = self.data["strategies"][name]
        return f"{st['version']}+{self.strategy_hash(name)}"


def git_commit(root: Path = ROOT) -> str:
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        commit = out.stdout.strip() or "nogit"
        dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True, timeout=5).stdout.strip()
        return commit + ("-dirty" if dirty else "")
    except Exception:
        return "nogit"


def load_config(root: Path = ROOT, overrides: dict | None = None,
                env: dict[str, str] | None = None) -> Config:
    default = root / "config" / "default.toml"
    if not default.exists():
        raise ConfigError(f"missing {default}")
    data = tomllib.loads(default.read_text())
    local = root / "config" / "local.toml"
    if local.exists():
        data = _merge(data, tomllib.loads(local.read_text()))
    if overrides:
        data = _merge(data, overrides)
    if env is None:
        env = load_dotenv(root / ".env")
        env.update({k: v for k, v in os.environ.items()})
    return Config(data, env, root)
