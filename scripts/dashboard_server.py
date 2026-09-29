#!/usr/bin/env python3
"""The live dashboard: a small read-only web server on the droplet.

    .venv/bin/python scripts/dashboard_server.py            # foreground
    .venv/bin/python scripts/dashboard_server.py --daemon   # (re)start in background

Listens on 127.0.0.1 only; reach it through an SSH tunnel (the Mac launcher
"DASHBOARD (VPS).command" does all of this). It serves the page and a JSON
snapshot (scripts/dashboard_api.py), and accepts exactly two actions, both of
which run as this unprivileged user:

    POST /api/control/refresh         ask the bot to re-read market data
    POST /api/control/reset-breaker   scripts/reset_breaker.py --note ...

Starting, stopping and restarting the service need root, which this process
does not have and is not given: those buttons are served by the Mac-side
helper (scripts/mac/dashboard_local.py), which runs them over the root SSH
key that already lives on the Mac. Opened any other way, the page shows them
disabled.

Every POST must carry the per-start token (X-Dashboard-Token, embedded in the
page) and a localhost Host header - so another web page in the same browser
cannot trigger an action, and DNS rebinding cannot reach this port. Every
action is appended to data/control/audit.jsonl.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import dashboard_api as api  # noqa: E402
from kalshibot.control import ALLOWED, ControlInbox  # noqa: E402

CONTROL = ROOT / "data" / "control"
PAGE = ROOT / "scripts" / "dashboard_app.html"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}
SNAPSHOT_TTL = 5.0


def audit(action: str, ok: bool, detail: str, by: str = "dashboard") -> None:
    CONTROL.mkdir(parents=True, exist_ok=True)
    with open(CONTROL / "audit.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": time.time(), "action": action, "ok": ok,
                             "detail": str(detail)[:500], "by": by}) + "\n")


class State:
    def __init__(self, db: Path, token: str, exchange: bool = True):
        self.db = db
        self.token = token
        self.inbox = ControlInbox(CONTROL)
        self.exchange = api.Exchange() if exchange else None
        self._snap: tuple[float, bytes] | None = None
        self._lock = threading.Lock()

    def snapshot(self, force: bool = False) -> bytes:
        with self._lock:
            if not force and self._snap and time.time() - self._snap[0] < SNAPSHOT_TTL:
                return self._snap[1]
            body = json.dumps(api.snapshot(self.db, self.exchange), default=str).encode()
            self._snap = (time.time(), body)
            return body


def host_ok(host: str | None) -> bool:
    if not host:
        return False
    name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
    return name in LOCAL_HOSTS


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        server_version = "KalshiDashboard/1.0"

        def log_message(self, fmt, *args):  # quiet; errors still go to stderr
            pass

        # ---- plumbing ------------------------------------------------
        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, default=str).encode())

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 10_000:
                return {}
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return {}

        def _guard(self, write: bool) -> bool:
            if not host_ok(self.headers.get("Host")):
                self._json(403, {"ok": False, "error": "localhost only"})
                return False
            if write:
                given = self.headers.get("X-Dashboard-Token") or ""
                if not secrets.compare_digest(given, state.token):
                    self._json(403, {"ok": False, "error": "missing or wrong dashboard token"})
                    return False
            return True

        # ---- routes --------------------------------------------------
        def do_GET(self):  # noqa: N802
            if not self._guard(write=False):
                return
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                html = PAGE.read_text(encoding="utf-8").replace(
                    "__DASHBOARD_TOKEN__", state.token)
                self._send(200, html.encode(), "text/html; charset=utf-8")
            elif path == "/api/snapshot":
                force = "force=1" in (self.path.split("?", 1) + [""])[1]
                try:
                    self._send(200, state.snapshot(force))
                except Exception as exc:  # noqa: BLE001
                    self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            elif path == "/api/capabilities":
                self._json(200, {"service_control": False, "requests": ALLOWED,
                                 "where": "droplet"})
            elif path == "/healthz":
                self._json(200, {"ok": True})
            else:
                self._json(404, {"ok": False, "error": "not found"})

        def do_POST(self):  # noqa: N802
            if not self._guard(write=True):
                return
            path = self.path.split("?")[0]
            body = self._body()
            by = str(body.get("by") or "dashboard")[:40]
            if path == "/api/control/refresh":
                state.inbox.request("refresh", by=by)
                audit("refresh", True, "requested", by)
                self._json(200, {"ok": True, "detail": "refresh requested; the bot "
                                 "picks it up within a few seconds"})
            elif path == "/api/control/reset-breaker":
                note = str(body.get("note") or "").strip()
                if len(note) < 5:
                    self._json(400, {"ok": False, "error": "a note (what was reviewed) is required"})
                    return
                r = subprocess.run(
                    [sys.executable, str(ROOT / "scripts" / "reset_breaker.py"),
                     "--note", f"{note} (dashboard, {by})"],
                    capture_output=True, text=True, timeout=60, cwd=ROOT)
                ok = r.returncode == 0
                audit("reset-breaker", ok, (r.stdout + r.stderr).strip(), by)
                self._json(200 if ok else 500, {"ok": ok, "detail": (r.stdout + r.stderr).strip()})
            elif path == "/api/control/pause":
                # Pause or resume ONE strategy's new entries. The bot keeps
                # pricing, recording and managing what it holds; it reads
                # this file every few seconds (ArbitrageEngine.paused_strategies).
                name = str(body.get("strategy") or "")
                if name not in api.STRATEGIES.values():
                    self._json(400, {"ok": False, "error": f"unknown strategy {name!r}"})
                    return
                want = bool(body.get("paused"))
                current = api.paused(CONTROL)
                current[name] = want
                from kalshibot.control import write_status
                write_status(CONTROL / "paused.json", {"paused": current, "at": time.time(),
                                                       "by": by})
                state.snapshot(force=True)
                audit("pause" if want else "resume", True, name, by)
                self._json(200, {"ok": True, "detail": f"{name} "
                                 f"{'paused - no new entries' if want else 'resumed'}"})
            elif path == "/api/control/audit":
                # The Mac helper records what it did with the service here, so
                # there is one audit trail.
                audit(str(body.get("action") or "?")[:40], bool(body.get("ok")),
                      str(body.get("detail") or ""), by)
                self._json(200, {"ok": True})
            elif path.startswith("/api/service/"):
                self._json(501, {"ok": False, "error": "start/stop/restart are available "
                                 "only through the Mac launcher (they need root, which "
                                 "this server does not have)"})
            else:
                self._json(404, {"ok": False, "error": "not found"})

    return Handler


def token_file() -> str:
    CONTROL.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    p = CONTROL / "dashboard.token"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(tok)
    return tok


def daemonize(args) -> int:
    """Stop a running copy (so new code is picked up) and start in the background."""
    pid_file = CONTROL / "dashboard.pid"
    try:
        old = int(pid_file.read_text())
        os.kill(old, signal.SIGTERM)
        for _ in range(30):
            time.sleep(0.1)
            os.kill(old, 0)
    except (OSError, ValueError):
        pass
    (ROOT / "logs").mkdir(exist_ok=True)
    log = open(ROOT / "logs" / "dashboard.log", "a")
    p = subprocess.Popen([sys.executable, __file__, "--port", str(args.port)],
                         stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                         cwd=ROOT, start_new_session=True)
    for _ in range(50):
        time.sleep(0.1)
        if (CONTROL / "dashboard.pid").exists() and \
                (CONTROL / "dashboard.pid").read_text().strip() == str(p.pid):
            print(f"dashboard server running, pid {p.pid}, 127.0.0.1:{args.port}")
            return 0
    print("dashboard server did not start; see logs/dashboard.log")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--no-exchange", action="store_true", help="do not read Kalshi")
    args = ap.parse_args()
    if args.daemon:
        return daemonize(args)
    # The journal path straight from config.yaml: the dashboard must come up
    # even when the trading credentials in .env are missing or broken.
    raw = str(((api.load_config().get("journal") or {}).get("database_path"))
              or "data/journal.db")
    db = Path(raw) if Path(raw).is_absolute() else ROOT / raw
    state = State(db, token_file(), exchange=not args.no_exchange)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(state))
    (CONTROL / "dashboard.pid").write_text(str(os.getpid()))
    print(f"dashboard on http://127.0.0.1:{args.port} (tunnel it; localhost only)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
