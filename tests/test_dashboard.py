"""The live dashboard: its snapshot, its server's guards, the bot's control
inbox and the Mac helper that runs the service buttons.

Pinned: a snapshot of a real journal builds every section without error and
reads money the same way the bot does; a stale demo balance read does not
move the displayed cash; the server refuses any action without the token or
from a non-localhost Host, and has no way to touch the service itself; the
bot acts only on requests it knows; the Mac helper runs exactly
`systemctl <verb> kalshibot` over the root alias and records it.
"""
import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "mac"))

import pytest
import dashboard_api as api
import dashboard_server as srv
import dashboard_local as mac
from kalshibot.control import ControlInbox, write_status
from kalshibot.journal import Journal


def _journal(tmp) -> Path:
    db = Path(tmp) / "j.db"
    j = Journal(db)
    j.log_leg_fill(ticker="KXHIGHNY-1", side="yes", action="buy", filled=10, price_cents=40)
    j.log_leg_fill(ticker="KXHIGHNY-1", side="yes", action="sell", filled=10, price_cents=55)
    j.log_leg_fill(ticker="KXNFL-1", side="no", action="buy", filled=5, price_cents=30)
    for v in (489.67, 489.67, 454.35, 489.67, 489.67):
        j.log_balance(v)
    j.conn.commit()
    return db


# ------------------------------------------------------------- snapshot
def test_a_snapshot_builds_every_section_from_a_real_journal():
    with tempfile.TemporaryDirectory() as tmp:
        s = api.snapshot(_journal(tmp), None)
        assert s["errors"] == {}, s["errors"]
        for sec in ("core", "account", "pnl", "trades", "weather", "decisions",
                    "model_vs_market", "paper", "maker", "risk", "system"):
            assert s[sec] is not None, sec
        assert abs(s["pnl"]["realised"] - 1.5) < 1e-9          # 10 x (55 - 40)c
        assert s["pnl"]["closed"] == 1 and s["pnl"]["open"] == 1
        assert {b["strategy"] for b in s["pnl"]["by_strategy"]} == {"Weather"}
        json.dumps(s, default=str)                              # serialisable


def test_a_lone_stale_balance_read_does_not_move_the_cash():
    with tempfile.TemporaryDirectory() as tmp:
        acc = api.snapshot(_journal(tmp), None)["account"]
        assert acc["cash"] == 489.67 and acc["flicker"] is True


def test_one_broken_section_does_not_take_the_page_down():
    with tempfile.TemporaryDirectory() as tmp:
        orig = api.maker
        api.maker = lambda conn: 1 / 0
        try:
            s = api.snapshot(_journal(tmp), None)
        finally:
            api.maker = orig
        assert s["maker"] is None and "ZeroDivisionError" in s["errors"]["maker"]
        assert s["pnl"] is not None


# ------------------------------------------------------------- control inbox
def test_the_bot_acts_only_on_requests_it_knows():
    with tempfile.TemporaryDirectory() as tmp:
        inbox = ControlInbox(tmp)
        inbox.request("refresh", by="test")
        (Path(tmp) / "place_order.request").write_text("{}")
        pending = inbox.pending()
        assert [n for n, _ in pending] == ["refresh"]
        assert inbox.last_done("place_order")["ok"] is False
        assert not (Path(tmp) / "place_order.request").exists()
        inbox.done("refresh", True, "ok")
        assert inbox.pending() == [] and inbox.last_done("refresh")["ok"] is True
        with pytest.raises(ValueError):
            inbox.request("stop")


def test_status_is_written_atomically():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "status.json"
        write_status(p, {"a": 1})
        write_status(p, {"a": 2})
        assert json.loads(p.read_text()) == {"a": 2}
        assert [x.name for x in Path(tmp).iterdir()] == ["status.json"]


# ------------------------------------------------------------- the server
def _serve(handler_cls):
    s = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, f"http://127.0.0.1:{s.server_address[1]}"


def _req(url, method="GET", token=None, body=None, host=None):
    r = urllib.request.Request(url, method=method,
                               data=json.dumps(body or {}).encode() if method == "POST" else None)
    if token:
        r.add_header("X-Dashboard-Token", token)
    if host:
        r.add_header("Host", host)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_the_server_guards_every_action():
    with tempfile.TemporaryDirectory() as tmp:
        old = srv.CONTROL
        srv.CONTROL = Path(tmp) / "control"
        try:
            state = srv.State(_journal(tmp), "tok-123", exchange=False)
            state.inbox = ControlInbox(srv.CONTROL)
            s, base = _serve(srv.make_handler(state))
            code, page = _req(base + "/")
            assert code == 200 and b"tok-123" in page
            assert _req(base + "/api/snapshot")[0] == 200
            assert _req(base + "/api/control/refresh", "POST")[0] == 403
            assert _req(base + "/api/control/refresh", "POST", token="wrong")[0] == 403
            assert _req(base + "/api/snapshot", host="evil.example:80")[0] == 403
            assert _req(base + "/api/control/refresh", "POST", token="tok-123",
                        host="evil.example")[0] == 403
            code, _ = _req(base + "/api/control/refresh", "POST", token="tok-123")
            assert code == 200 and (srv.CONTROL / "refresh.request").exists()
            assert _req(base + "/api/service/restart", "POST", token="tok-123")[0] == 501
            code, body = _req(base + "/api/control/reset-breaker", "POST", token="tok-123",
                              body={"note": ""})
            assert code == 400
            audit = (srv.CONTROL / "audit.jsonl").read_text().splitlines()
            assert json.loads(audit[0])["action"] == "refresh"
            s.shutdown()
        finally:
            srv.CONTROL = old


def test_pausing_a_strategy_writes_the_file_the_engine_reads():
    with tempfile.TemporaryDirectory() as tmp:
        old = srv.CONTROL
        srv.CONTROL = Path(tmp) / "control"
        try:
            state = srv.State(_journal(tmp), "tok", exchange=False)
            s, base = _serve(srv.make_handler(state))
            code, _ = _req(base + "/api/control/pause", "POST", token="tok",
                           body={"strategy": "inflation_nowcast", "paused": True})
            assert code == 200
            assert api.paused(srv.CONTROL) == {"inflation_nowcast": True}
            _req(base + "/api/control/pause", "POST", token="tok",
                 body={"strategy": "weather_temperature", "paused": True})
            _req(base + "/api/control/pause", "POST", token="tok",
                 body={"strategy": "inflation_nowcast", "paused": False})
            assert api.paused(srv.CONTROL) == {"inflation_nowcast": False,
                                                "weather_temperature": True}
            assert _req(base + "/api/control/pause", "POST", token="tok",
                        body={"strategy": "rm -rf", "paused": True})[0] == 400
            assert _req(base + "/api/control/pause", "POST",
                        body={"strategy": "arbitrage", "paused": True})[0] == 403
            # and the engine sees exactly that
            from kalshibot.arb_engine import ArbitrageEngine
            e = ArbitrageEngine.__new__(ArbitrageEngine)

            class S:
                def path(self, dotted, default):
                    return srv.CONTROL
            e.s = S()
            assert e.paused_strategies() == {"weather_temperature"}
            s.shutdown()
        finally:
            srv.CONTROL = old


def test_inflation_tickers_are_their_own_strategy():
    assert api.strategy_of("KXCPI-26DEC-T0.3") == "Inflation"
    assert api.strategy_of("KXPCECORE-26AUG-T0.2") == "Inflation"
    assert api.strategy_of("KXHIGHNY-26SEP29-T70") == "Weather"
    assert api.strategy_of("KXNFLTOTAL-26SEP28-45") == "Arbitrage"
    assert api.strike_of("KXCPI-26DEC-T-0.1") == -0.1 and api.strike_of("X-B70.5") is None


def test_host_checks():
    assert srv.host_ok("localhost:8790") and srv.host_ok("127.0.0.1:18790")
    assert srv.host_ok("[::1]:8790")
    assert not srv.host_ok("evil.example:8790") and not srv.host_ok(None)
    assert not srv.host_ok("localhost.evil.example")


# ------------------------------------------------------------- the Mac helper
class FakeUpstream(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *a):
        pass

    def _reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._reply(200, {"ok": True, "path": self.path})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        FakeUpstream.calls.append((self.path, body, self.headers.get("X-Dashboard-Token")))
        if self.path == "/api/control/reset-breaker" and not body.get("note"):
            return self._reply(400, {"ok": False, "error": "note required"})
        self._reply(200, {"ok": True, "detail": "done"})


def test_the_mac_helper_runs_exactly_systemctl_over_the_root_alias():
    FakeUpstream.calls = []
    up, up_base = _serve(FakeUpstream)
    ran = []

    def runner(alias, args, timeout=90):
        ran.append((alias, args))
        return True, "active" if args[1] == "is-active" else ""
    h, base = _serve(mac.make_handler(mac.Upstream(up_base, "tok"), "tok", "droplet",
                                      "kalshibot", runner=runner))
    try:
        assert json.loads(_req(base + "/api/capabilities")[1])["service_control"] is True
        assert _req(base + "/api/service/restart", "POST")[0] == 403
        code, body = _req(base + "/api/service/restart", "POST", token="tok")
        assert code == 200 and ran[0] == ("droplet", ["systemctl", "restart", "kalshibot"])
        assert json.loads(body)["state"] == "active"
        assert any(c[0] == "/api/control/audit" for c in FakeUpstream.calls)
        assert _req(base + "/api/service/rm-rf", "POST", token="tok")[0] == 404
        # reset needs a note, and restarts only once the breaker reset worked
        ran.clear()
        assert _req(base + "/api/service/reset", "POST", token="tok", body={"note": ""})[0] == 400
        assert ran == []
        code, _ = _req(base + "/api/service/reset", "POST", token="tok",
                       body={"note": "reviewed the losses"})
        assert code == 200 and ran[0][1] == ["systemctl", "restart", "kalshibot"]
        # everything else is passed through with the token
        assert json.loads(_req(base + "/api/snapshot")[1])["path"] == "/api/snapshot"
        _req(base + "/api/control/refresh", "POST", token="tok")
        assert FakeUpstream.calls[-1][0] == "/api/control/refresh"
        assert FakeUpstream.calls[-1][2] == "tok"
    finally:
        h.shutdown()
        up.shutdown()


def test_the_page_carries_the_token_placeholder_and_every_panel():
    html = (ROOT / "scripts" / "dashboard_app.html").read_text()
    assert "__DASHBOARD_TOKEN__" in html
    for pid in ("overview", "trades", "weather", "research", "risk", "system"):
        assert f'id="page-{pid}"' in html
    assert "X-Dashboard-Token" in html
