#!/usr/bin/env python3
"""Mac side of the dashboard: one local address, service control over SSH.

Started by ~/trading/"DASHBOARD (VPS).command"; not run on the droplet.

    browser  ->  http://localhost:8790  (this, on the Mac)
                   |- GET  /, /api/snapshot, POST /api/control/*
                   |     -> SSH tunnel -> dashboard_server.py on the droplet
                   '- POST /api/service/{start,stop,restart,reset}
                         -> ssh <root alias> systemctl ... kalshibot

The droplet server runs unprivileged and cannot start or stop the service;
the Mac already holds the root key for the droplet (the START BOT launcher
uses it), so the service buttons are executed from here - which also means
they only work from this Mac. Standard library only.

The same token and localhost rules as the droplet server apply, and every
service action is recorded in the droplet's audit log.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERBS = {"start", "stop", "restart"}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


def host_ok(host: str | None) -> bool:
    if not host:
        return False
    name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
    return name in LOCAL_HOSTS


class Upstream:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.token = token

    def call(self, method: str, path: str, body: bytes | None = None,
             timeout: float = 60.0) -> tuple[int, bytes, str]:
        req = urllib.request.Request(self.base + path, data=body, method=method)
        req.add_header("X-Dashboard-Token", self.token)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read(), r.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers.get("Content-Type", "application/json")
        except (urllib.error.URLError, OSError) as e:
            msg = {"ok": False, "error": f"droplet dashboard unreachable: {e}"}
            return 502, json.dumps(msg).encode(), "application/json"


def run_ssh(root_alias: str, args: list[str], timeout: int = 90) -> tuple[bool, str]:
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                            root_alias, *args], capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"


def make_handler(up: Upstream, token: str, root_alias: str, unit: str, runner=run_ssh):
    class Handler(BaseHTTPRequestHandler):
        server_version = "KalshiDashboardMac/1.0"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj).encode())

        def _read(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if 0 < n <= 10_000 else b""

        def do_GET(self):  # noqa: N802
            if not host_ok(self.headers.get("Host")):
                return self._json(403, {"ok": False, "error": "localhost only"})
            if self.path.split("?")[0] == "/api/capabilities":
                return self._json(200, {"service_control": True, "unit": unit,
                                        "where": "mac"})
            code, body, ctype = up.call("GET", self.path, timeout=90)
            self._send(code, body, ctype)

        def do_POST(self):  # noqa: N802
            if not host_ok(self.headers.get("Host")):
                return self._json(403, {"ok": False, "error": "localhost only"})
            if not secrets.compare_digest(self.headers.get("X-Dashboard-Token") or "", token):
                return self._json(403, {"ok": False, "error": "missing or wrong dashboard token"})
            path = self.path.split("?")[0]
            raw = self._read()
            if not path.startswith("/api/service/"):
                code, body, ctype = up.call("POST", path, raw or b"{}")
                return self._send(code, body, ctype)
            verb = path.rsplit("/", 1)[-1]
            try:
                req = json.loads(raw or b"{}")
            except ValueError:
                req = {}
            detail = []
            if verb == "reset":
                # Reset = acknowledge the losing run (with a note), then restart:
                # a restart alone re-trips the breaker.
                code, body, _ = up.call("POST", "/api/control/reset-breaker",
                                        json.dumps({"note": req.get("note", ""),
                                                    "by": "mac"}).encode())
                res = json.loads(body or b"{}")
                if code != 200 or not res.get("ok"):
                    return self._json(code if code != 200 else 500, res)
                detail.append(res.get("detail", ""))
                verb = "restart"
            if verb not in VERBS:
                return self._json(404, {"ok": False, "error": "unknown action"})
            ok, out = runner(root_alias, ["systemctl", verb, unit])
            _, state = runner(root_alias, ["systemctl", "is-active", unit], 20)
            detail.append(out or f"systemctl {verb} {unit}")
            detail.append(f"service is now: {state}")
            text = "\n".join(d for d in detail if d)
            up.call("POST", "/api/control/audit", json.dumps(
                {"action": f"service-{path.rsplit('/', 1)[-1]}", "ok": ok,
                 "detail": text, "by": "mac"}).encode())
            self._json(200 if ok else 500, {"ok": ok, "detail": text, "state": state})

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--upstream", default="http://127.0.0.1:18790")
    ap.add_argument("--root-alias", default="droplet")
    ap.add_argument("--unit", default="kalshibot")
    args = ap.parse_args()
    token = os.environ.get("KALSHI_DASHBOARD_TOKEN", "")
    if not token:
        print("KALSHI_DASHBOARD_TOKEN is not set; start this from the launcher.")
        return 2
    srv = ThreadingHTTPServer(("127.0.0.1", args.port),
                              make_handler(Upstream(args.upstream, token), token,
                                           args.root_alias, args.unit))
    print(f"dashboard: http://localhost:{args.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
