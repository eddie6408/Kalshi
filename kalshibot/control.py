"""The bot's side of the dashboard: a status file out, operator requests in.

    write_status(path, payload)   atomic JSON the dashboard reads (heartbeat)
    ControlInbox(dir)             requests the dashboard drops as files

The dashboard never talks to the bot's threads and never imports its state.
It reads `data/control/status.json`, written once a heartbeat, and asks for
things by writing `data/control/<name>.request`; the bot's control worker
picks the request up, does it, and answers in `<name>.done`. Files, because
they survive either side restarting and need no port, socket or privilege.

Only requests named in ALLOWED are acted on. None of them can place, size or
cancel an order, change a limit or a mode: the most any of them does is make
the bot re-read the market.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

#: request name -> what it does, for the dashboard and the log
ALLOWED = {
    "refresh": "distrust every cached book, reconnect the market feed and "
               "re-read production prices on the next pass",
}


def write_status(path: str | Path, payload: dict) -> None:
    """Write JSON atomically: a reader sees the old file or the new one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".status-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, default=str)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class ControlInbox:
    def __init__(self, directory: str | Path, clock=time.time):
        self.dir = Path(directory)
        self.clock = clock

    def request(self, name: str, by: str = "dashboard", note: str = "") -> Path:
        """Used by the dashboard: ask the bot for `name`."""
        if name not in ALLOWED:
            raise ValueError(f"unknown request {name!r}")
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.dir / f"{name}.request"
        write_status(p, {"name": name, "at": self.clock(), "by": by, "note": note[:200]})
        return p

    def pending(self) -> list[tuple[str, dict]]:
        """Used by the bot: requests waiting, oldest first. Unknown names are
        answered as refused and removed, never acted on."""
        if not self.dir.exists():
            return []
        out = []
        for p in sorted(self.dir.glob("*.request"), key=lambda p: p.stat().st_mtime):
            name = p.stem
            try:
                body = json.loads(p.read_text(encoding="utf-8") or "{}")
            except (OSError, ValueError):
                body = {}
            if name not in ALLOWED:
                self.done(name, False, "refused: not a known request")
                continue
            out.append((name, body))
        return out

    def done(self, name: str, ok: bool, detail: str) -> None:
        write_status(self.dir / f"{name}.done",
                     {"name": name, "ok": ok, "detail": detail[:500], "at": self.clock()})
        try:
            (self.dir / f"{name}.request").unlink()
        except FileNotFoundError:
            pass

    def last_done(self, name: str) -> dict | None:
        try:
            return json.loads((self.dir / f"{name}.done").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
