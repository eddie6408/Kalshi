"""Live-mode authorization: PAPER is the default, LIVE is never automatic,
credentials are never loaded outside LIVE, and every readiness item gates."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from lvt import live_gate
from lvt.config import ConfigError, git_commit
from lvt.db import open_state_db
from lvt.logsetup import scrub
from tests.helpers import cfg


class TestModes(unittest.TestCase):
    def test_default_is_paper(self):
        self.assertEqual(cfg(env={}).mode, "PAPER")

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ConfigError):
            _ = cfg(env={"TRADING_MODE": "YOLO"}).mode

    def test_no_code_path_sets_mode(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent / "lvt"
        for f in root.rglob("*.py"):
            src = f.read_text()
            self.assertNotIn('env["TRADING_MODE"]', src, f)
            self.assertNotIn("environ['TRADING_MODE']", src, f)


class TestGate(unittest.TestCase):
    def setUp(self):
        self.db = open_state_db(Path(tempfile.mkdtemp()) / "s.db")

    def test_empty_database_is_not_ready(self):
        ok, items = live_gate.live_allowed(cfg(env={"TRADING_MODE": "LIVE"}), self.db)
        self.assertFalse(ok)
        self.assertEqual({i.name for i in items}, set(live_gate.ITEMS))
        self.assertIn("LIVE MODE UNAVAILABLE", live_gate.format_report(items))

    def test_missing_credentials_reported_without_values(self):
        c = cfg(env={"TRADING_MODE": "LIVE", "KALSHI_PROD_KEY_ID": "abc-secret-id"})
        ok, detail = live_gate.credentials_present(c)
        self.assertFalse(ok)
        self.assertNotIn("abc-secret-id", detail)

    def test_authorization_bound_to_commit_and_config(self):
        c = cfg()
        self.db.insert("live_authorizations", {"ts": time.time(), "authorized_by": "t", "git_commit": "other",
                                               "config_hash": c.config_hash()})
        item = [i for i in live_gate.readiness(c, self.db) if i.name == "HUMAN_AUTHORIZATION"][0]
        self.assertFalse(item.passed)

    def test_all_items_pass_only_with_full_evidence(self):
        c = cfg(env={"TRADING_MODE": "LIVE"})
        commit = git_commit(c.root)
        with mock.patch.object(live_gate, "credentials_present", return_value=(True, "ok")), \
                mock.patch.object(live_gate, "git_commit", return_value="abc123"):
            now = time.time()
            for i in range(250):
                self.db.insert("positions", {"id": f"p{i}", "env": "PAPER", "status": "CLOSED", "event_ticker": f"E{i % 30}",
                                             "opened_ts": now - 20 * 86400 + i, "closed_ts": now - 86400 + i,
                                             "realized_pnl_usd": 0.1})
            for i in range(60):
                self.db.insert("positions", {"id": f"s{i}", "env": "SHADOW", "status": "CLOSED", "event_ticker": "E",
                                             "opened_ts": now - 8 * 86400 + i, "closed_ts": now - 3600 + i,
                                             "realized_pnl_usd": 0.1})
            self.db.insert("backtest_runs", {"run_id": "b", "kind": "backtest", "segment": "all", "ts": now,
                                             "metrics": {"trades": 300}})
            self.db.insert("backtest_runs", {"run_id": "o", "kind": "oos", "segment": "test", "ts": now,
                                             "metrics": {"trades": 80, "expectancy_usd": 0.05}})
            for item in list(live_gate.SELFTEST_ITEMS) + ["EXECUTION_TESTED"]:
                self.db.insert("readiness_evidence", {"item": item, "ts": now, "git_commit": "abc123", "passed": 1})
            ok, items = live_gate.live_allowed(c, self.db)
            self.assertFalse(ok)                                            # no human authorization yet
            self.db.insert("live_authorizations", {"ts": now, "authorized_by": "human", "git_commit": "abc123",
                                                   "config_hash": c.config_hash()})
            ok, items = live_gate.live_allowed(c, self.db)
            self.assertTrue(ok, live_gate.format_report(items))
            live_gate.revoke(self.db)
            self.assertFalse(live_gate.live_allowed(c, self.db)[0])
        del commit

    def test_negative_expectancy_blocks(self):
        c = cfg()
        self.db.insert("backtest_runs", {"run_id": "o", "kind": "oos", "segment": "test", "ts": time.time(),
                                         "metrics": {"trades": 80, "expectancy_usd": -0.02}})
        item = [i for i in live_gate.readiness(c, self.db) if i.name == "NET_EXPECTANCY_POSITIVE"][0]
        self.assertFalse(item.passed)


class TestNoCredentialsOutsideLive(unittest.TestCase):
    def test_paper_runner_never_builds_live_venue(self):
        from lvt import runner
        c = cfg(env={"TRADING_MODE": "PAPER", "KALSHI_PROD_KEY_ID": "id", "KALSHI_PROD_PRIVATE_KEY_PATH": __file__})
        with mock.patch.object(runner, "build_live_venue", side_effect=AssertionError("live venue built")), \
                mock.patch.object(runner, "open_state_db", side_effect=lambda p: open_state_db(Path(tempfile.mkdtemp()) / "s.db")), \
                mock.patch.object(runner, "open_tape_db", side_effect=lambda p: open_state_db(Path(tempfile.mkdtemp()) / "t.db")):
            r = runner.LiveRunner(c)
            v = r.build_venue()
            self.assertFalse(v.is_real)
            self.assertTrue(r.db.query("SELECT * FROM alerts WHERE code='PRODUCTION_CREDENTIALS_DETECTED'"))

    def test_live_refused_exits_78(self):
        from lvt import runner
        c = cfg(env={"TRADING_MODE": "LIVE"})
        with mock.patch.object(runner, "open_state_db", side_effect=lambda p: open_state_db(Path(tempfile.mkdtemp()) / "s.db")), \
                mock.patch.object(runner, "open_tape_db", side_effect=lambda p: open_state_db(Path(tempfile.mkdtemp()) / "t.db")):
            r = runner.LiveRunner(c)
            with self.assertRaises(SystemExit) as ctx:
                r.build_venue()
            self.assertEqual(ctx.exception.code, live_gate.EXIT_LIVE_REFUSED)

    def test_public_http_cannot_sign(self):
        from lvt.data.http import PublicHttp
        h = PublicHttp("https://example.invalid", 1, 1, 1)
        self.assertFalse(any("KALSHI-ACCESS" in k for k in h._session.headers))
        self.assertFalse(hasattr(h, "signer"))

    def test_signer_repr_and_log_scrub(self):
        from lvt.execution.kalshi_live import CredentialError, Signer
        with self.assertRaises(CredentialError):
            Signer("", "")
        self.assertNotIn("SECRET", scrub("KALSHI_PROD_KEY_ID=SECRET"))
        self.assertIn("[REDACTED]", scrub("-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----"))


if __name__ == "__main__":
    unittest.main()
