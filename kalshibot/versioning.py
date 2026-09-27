"""Which code, which configuration, which model produced a decision.

Research brief section 24: every prediction and decision must identify the
versions behind it, and a result that cannot be reproduced from the journal
is a data-integrity defect. This module answers two of those questions once
per process:

    code_version()    the git commit the bot is running, "+dirty" if the
                      working tree differs from it
    config_version()  a short hash of config.yaml's exact bytes

Model and parameter versions are declared by the code that owns them (each
estimator and strategy); the fee schedule version comes from the fee model.
Computed lazily and cached: a version must describe the process that is
running, so it is read once and never changes underneath a decision.
"""
from __future__ import annotations

import hashlib
import subprocess
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNKNOWN = "unknown"


@lru_cache(maxsize=None)
def code_version(root: str | None = None) -> str:
    base = Path(root) if root else ROOT
    try:
        commit = subprocess.run(
            ["git", "-C", str(base), "rev-parse", "--short=12", "HEAD"],
            capture_output=True, text=True, timeout=5).stdout.strip()
        if not commit:
            return UNKNOWN
        dirty = subprocess.run(
            ["git", "-C", str(base), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True, timeout=5).stdout.strip()
        return commit + ("+dirty" if dirty else "")
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN


@lru_cache(maxsize=None)
def config_version(path: str | None = None) -> str:
    p = Path(path) if path else ROOT / "config.yaml"
    try:
        return "cfg-" + hashlib.sha256(p.read_bytes()).hexdigest()[:12]
    except OSError:
        return UNKNOWN


def versions() -> dict:
    return {"code_version": code_version(), "config_version": config_version()}
