"""Structured logging: JSON lines to logs/lvt.jsonl (rotated), readable text
to logs/lvt.log, and warnings+ to stderr (journald)."""
from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from pathlib import Path

# Anything that looks like key material is scrubbed before it reaches a log.
_SECRET = re.compile(r"(-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----|"
                     r"KALSHI-ACCESS-SIGNATURE['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9+/=]+|"
                     r"KALSHI_PROD_KEY_ID\s*=\s*\S+)", re.S)


def scrub(s: str) -> str:
    return _SECRET.sub("[REDACTED]", s)


class JsonFormatter(logging.Formatter):
    def format(self, r: logging.LogRecord) -> str:
        d = {"ts": round(r.created, 3), "level": r.levelname, "logger": r.name, "msg": scrub(r.getMessage())}
        if r.exc_info:
            d["exc"] = scrub(self.formatException(r.exc_info))
        return json.dumps(d)


class ScrubbingFormatter(logging.Formatter):
    def format(self, r: logging.LogRecord) -> str:
        return scrub(super().format(r))


def setup(log_dir: Path, level: str = "INFO", console_level: str = "WARNING") -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    jh = logging.handlers.RotatingFileHandler(log_dir / "lvt.jsonl", maxBytes=50_000_000, backupCount=5)
    jh.setFormatter(JsonFormatter())
    th = logging.handlers.RotatingFileHandler(log_dir / "lvt.log", maxBytes=50_000_000, backupCount=5)
    th.setFormatter(ScrubbingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(console_level)
    ch.setFormatter(ScrubbingFormatter("%(levelname)s %(name)s: %(message)s"))
    for h in (jh, th, ch):
        root.addHandler(h)
    logging.getLogger("urllib3").setLevel("WARNING")
