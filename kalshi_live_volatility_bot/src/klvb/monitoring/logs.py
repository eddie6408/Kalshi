"""Structured JSON logging (one JSON object per line) with secret redaction."""
from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
import time
from pathlib import Path

_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"(KALSHI-ACCESS-(?:KEY|SIGNATURE)['\"]?\s*[:=]\s*['\"]?)[^'\",\s}]+", re.I),
    re.compile(r"((?:api[_-]?key|secret|token|password|private[_-]?key)[_a-z]*['\"]?\s*[:=]\s*['\"]?)[^'\",\s}]+", re.I),
]


def redact(text: str) -> str:
    for p in _SECRET_PATTERNS:
        text = p.sub(lambda m: (m.group(1) if m.groups() else "") + "[REDACTED]", text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": round(record.created, 3), "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
               "level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        extra = getattr(record, "data", None)
        if extra:
            out["data"] = extra
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(out, default=str))


def setup_logging(log_dir: Path, level: str = "INFO", to_stdout: bool = True) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    fh = logging.handlers.RotatingFileHandler(log_dir / "klvb.jsonl", maxBytes=50_000_000, backupCount=10)
    fh.setFormatter(JsonFormatter())
    root.addHandler(fh)
    if to_stdout:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(JsonFormatter())
        root.addHandler(sh)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def event(logger: logging.Logger, msg: str, **data) -> None:
    """Structured event log: logger.info with a machine-readable payload."""
    logger.info(msg, extra={"data": data})
