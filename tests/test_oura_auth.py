"""Oura OAuth tests. No network: requests and Telegram are mocked.

They need a THROWAWAY MySQL/MariaDB database (the schema is applied to it and its oura tables are wiped), so they never
touch your real mimir database. Set these, then run:
    MIMIR_TEST_DB=mimir_test MIMIR_TEST_USER=root MIMIR_TEST_PASSWORD=... python -m unittest tests.test_oura_auth -v
Optional: MIMIR_TEST_HOST (default 127.0.0.1), MIMIR_TEST_PORT (default 3306). Without MIMIR_TEST_DB the tests are skipped."""
import json
import os
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = os.environ.get("MIMIR_TEST_DB")
CFG = {"_path": "x", "oura": {"client_id": "cid", "client_secret": "SECRET_CLIENT_SHHH", "scopes": "daily"},
       "supervisor": {"gui_port": 5000}, "paths": {"run_dir": "run"},
       "mysql": {"host": os.environ.get("MIMIR_TEST_HOST", "127.0.0.1"), "port": int(os.environ.get("MIMIR_TEST_PORT", "3306")),
                 "user": os.environ.get("MIMIR_TEST_USER", "root"), "password": os.environ.get("MIMIR_TEST_PASSWORD", ""), "db": TEST_DB}}
ACCESS, REFRESH, ACCESS2, REFRESH2 = "ACCESS_TOKEN_aaa111", "REFRESH_TOKEN_bbb222", "ACCESS_TOKEN_ccc333", "REFRESH_TOKEN_ddd444"
CODE = "AUTH_CODE_eee555"


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers, self.text = status, body, headers or {}, json.dumps(body)

    def json(self):
        return self._body


def tokens(access=ACCESS, refresh=REFRESH, expires_in=86400, scope="daily"):
    return Resp(200, {"access_token": access, "refresh_token": refresh, "expires_in": expires_in, "scope": scope})


@unittest.skipUnless(TEST_DB, "set MIMIR_TEST_DB to a throwaway database to run these tests")
class OuraAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import core.config as C
        C._cache[os.path.abspath(C.default_config_path())] = CFG
        import db.db as D
        D.apply_schema()
        from core import oura_auth
        cls.D, cls.auth = D, oura_auth

    def setUp(self):
        for t in ("oura_auth", "oura_oauth_state", "oura_daily", "oura_intraday", "error_log"):
            self.D.execute_query(f"DELETE FROM {t}")
        self.posts = []
        self.warnings = []
        self.logged = []
        self.patches = [
            mock.patch("core.oura_auth.notifier.send_warning", side_effect=lambda t: self.warnings.append(t) or True),
            mock.patch("core.oura_auth.log_error", side_effect=lambda *a, **k: self.logged.append(a)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def connect(self, expires_in=86400, access=ACCESS, refresh=REFRESH):
        with mock.patch("requests.post", return_value=tokens(access, refresh, expires_in)):
            self.auth.exchange_code(CFG, CODE)

    def expire(self, minutes=1):
        self.D.execute_query("UPDATE oura_auth SET expires_at = %s", (datetime.now() + timedelta(minutes=minutes),))

    def row(self):
        return self.D.fetch_one("SELECT * FROM oura_auth WHERE id = 1")

    # ---------------------------------------------------------------- state
    def test_state_is_single_use(self):
        s = self.auth.new_state()
        self.assertTrue(self.auth.consume_state(s))
        self.assertFalse(self.auth.consume_state(s))

    def test_state_unknown_missing_expired(self):
        self.assertFalse(self.auth.consume_state("nope"))
        self.assertFalse(self.auth.consume_state(None))
        self.assertFalse(self.auth.consume_state(""))
        s = self.auth.new_state()
        self.D.execute_query("UPDATE oura_oauth_state SET expires_at = %s", (datetime.now() - timedelta(minutes=1),))
        self.assertFalse(self.auth.consume_state(s))

    def test_authorize_url(self):
        u = self.auth.build_authorize_url(CFG, "STATE1")
        self.assertIn("response_type=code", u)
        self.assertIn("state=STATE1", u)
        self.assertIn("scope=daily", u)
        self.assertIn("redirect_uri=http%3A%2F%2Flocalhost%3A5000%2Foauth%2Foura%2Fcallback", u)
        self.assertNotIn("SECRET_CLIENT_SHHH", u)

    # -------------------------------------------------------------- refresh
    def test_fresh_token_no_http(self):
        self.connect()
        with mock.patch("requests.post") as post:
            self.assertEqual(self.auth.get_access_token(CFG), ACCESS)
            post.assert_not_called()

    def test_refresh_success_with_rotated_refresh_token(self):
        self.connect()
        self.expire()
        with mock.patch("requests.post", return_value=tokens(ACCESS2, REFRESH2)) as post:
            self.assertEqual(self.auth.get_access_token(CFG), ACCESS2)
            sent = post.call_args.kwargs["data"]
            self.assertEqual(sent["grant_type"], "refresh_token")
            self.assertEqual(sent["refresh_token"], REFRESH)
        r = self.row()
        self.assertEqual((r["access_token"], r["refresh_token"], r["state"]), (ACCESS2, REFRESH2, "connected"))

    def test_refresh_without_new_refresh_token_keeps_old(self):
        self.connect()
        self.expire()
        body = Resp(200, {"access_token": ACCESS2, "expires_in": 3600})
        with mock.patch("requests.post", return_value=body):
            self.auth.get_access_token(CFG)
        self.assertEqual(self.row()["refresh_token"], REFRESH)

    def test_invalid_grant_marks_revoked_and_warns_once(self):
        self.connect()
        self.expire()
        bad = Resp(400, {"error": "invalid_grant"})
        with mock.patch("requests.post", return_value=bad):
            with self.assertRaises(self.auth.NotConnected):
                self.auth.get_access_token(CFG)
        self.assertEqual(self.row()["state"], "revoked")
        self.assertEqual(len(self.warnings), 1)
        self.assertIn("/setup/oura", self.warnings[0])
        with mock.patch("requests.post") as post:               # later polls stop quietly, no HTTP, no second warning
            for _ in range(3):
                with self.assertRaises(self.auth.NotConnected):
                    self.auth.get_access_token(CFG)
            post.assert_not_called()
        self.assertEqual(len(self.warnings), 1)

    def test_401_on_refresh_marks_revoked(self):
        self.connect()
        self.expire()
        with mock.patch("requests.post", return_value=Resp(401, {})):
            with self.assertRaises(self.auth.NotConnected):
                self.auth.get_access_token(CFG)
        self.assertEqual(self.row()["state"], "revoked")

    def test_network_error_is_not_revocation(self):
        import requests
        self.connect()
        self.expire()
        with mock.patch("requests.post", side_effect=requests.ConnectionError("down")):
            with self.assertRaises(self.auth.TemporaryError):
                self.auth.get_access_token(CFG)
        self.assertEqual(self.row()["state"], "connected")
        self.assertEqual(self.warnings, [])
        with mock.patch("requests.post", return_value=tokens(ACCESS2, REFRESH2)):   # next poll recovers
            self.assertEqual(self.auth.get_access_token(CFG), ACCESS2)

    def test_server_error_is_not_revocation(self):
        self.connect()
        self.expire()
        with mock.patch("requests.post", return_value=Resp(503, {})):
            with self.assertRaises(self.auth.TemporaryError):
                self.auth.get_access_token(CFG)
        self.assertEqual(self.row()["state"], "connected")

    def test_concurrent_refresh_makes_one_http_call(self):
        self.connect()
        self.expire()
        calls, results = [], []

        def slow_post(*a, **k):
            calls.append(1)
            time.sleep(0.5)                                    # long enough for the second caller to be waiting on the lock
            return tokens(ACCESS2, REFRESH2)

        with mock.patch("requests.post", side_effect=slow_post):
            ts = [threading.Thread(target=lambda: results.append(self.auth.get_access_token(CFG))) for _ in range(2)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(20)
        self.assertEqual(len(calls), 1)
        self.assertEqual(results, [ACCESS2, ACCESS2])

    def test_forced_refresh_skipped_if_someone_else_refreshed(self):
        self.connect(access=ACCESS2, refresh=REFRESH2)
        with mock.patch("requests.post") as post:
            self.assertEqual(self.auth.refresh(CFG, bad_token=ACCESS), ACCESS2)   # our 401'd token is stale
            post.assert_not_called()

    def test_status_never_includes_tokens(self):
        self.connect()
        self.assertNotIn(ACCESS, json.dumps(self.auth.status(CFG), default=str))
        self.assertNotIn(REFRESH, json.dumps(self.auth.status(CFG), default=str))

    def test_scope_change_asks_for_reconnect(self):
        self.connect()
        cfg2 = {**CFG, "oura": {**CFG["oura"], "scopes": "daily heartrate"}}
        self.assertTrue(self.auth.status(cfg2)["needs_scopes"])
        self.assertFalse(self.auth.status(CFG)["needs_scopes"])

    def test_disconnect_keeps_data_by_default(self):
        self.connect()
        self.D.execute_query("INSERT INTO oura_daily (date, data_source, missing) VALUES (CURDATE(), 'api', FALSE)")
        with mock.patch("requests.get", return_value=Resp(200, {})):
            self.auth.revoke(CFG)
        self.assertIsNone(self.row())
        self.assertEqual(self.D.fetch_one("SELECT COUNT(*) n FROM oura_daily")["n"], 1)
        self.connect()
        with mock.patch("requests.get", return_value=Resp(200, {})):
            self.auth.revoke(CFG, delete_data=True)
        self.assertEqual(self.D.fetch_one("SELECT COUNT(*) n FROM oura_daily")["n"], 0)

    # --------------------------------------------------------------- poller
    def test_poller_skips_quietly_when_not_connected(self):
        from pollers import oura
        with mock.patch("requests.get") as get:
            self.assertEqual(oura.run_cycle(CFG), "auth_failed")
            get.assert_not_called()

    def test_poller_401_then_refresh_then_retry(self):
        from pollers import oura
        self.connect()
        seen = []

        def fake_get(url, headers=None, params=None, timeout=None):
            seen.append(headers["Authorization"])
            return Resp(401, {}) if headers["Authorization"] == f"Bearer {ACCESS}" else Resp(200, {"data": []})

        with mock.patch("requests.get", side_effect=fake_get), mock.patch("requests.post", return_value=tokens(ACCESS2, REFRESH2)):
            oura.run_cycle(CFG)
        self.assertEqual(seen[0], f"Bearer {ACCESS}")
        self.assertIn(f"Bearer {ACCESS2}", seen)

    def test_poller_429_skips_cycle(self):
        from pollers import oura
        self.connect()
        with mock.patch("requests.get", return_value=Resp(429, {}, {"Retry-After": "60"})):
            self.assertEqual(oura.run_cycle(CFG), "backoff")


@unittest.skipUnless(TEST_DB, "set MIMIR_TEST_DB to a throwaway database to run these tests")
class OuraGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import core.config as C
        C._cache[os.path.abspath(C.default_config_path())] = CFG
        import db.db as D
        D.apply_schema()
        from core import oura_auth
        from gui.app import create_app
        cls.D, cls.auth = D, oura_auth
        cls.app = create_app(CFG)

    def setUp(self):
        for t in ("oura_auth", "oura_oauth_state", "error_log"):
            self.D.execute_query(f"DELETE FROM {t}")
        self.c = self.app.test_client()
        self.loc = {"REMOTE_ADDR": "127.0.0.1"}
        self.polls = []
        self.p = mock.patch("gui.app.oura_poller.run_cycle", side_effect=lambda cfg: self.polls.append(1))
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def cb(self, query, **kw):
        return self.c.get("/oauth/oura/callback?" + query, environ_base=self.loc, base_url="http://localhost:5000", follow_redirects=True, **kw)

    def flashes(self, r):
        return r.get_data(as_text=True)

    def test_callback_missing_state(self):
        with mock.patch("requests.post") as post:
            r = self.cb(f"code={CODE}")
            post.assert_not_called()
        self.assertIn("unknown, expired, or was already used", self.flashes(r))

    def test_callback_unknown_state(self):
        with mock.patch("requests.post") as post:
            r = self.cb(f"code={CODE}&state=forged")
            post.assert_not_called()
        self.assertIn("unknown, expired, or was already used", self.flashes(r))

    def test_callback_expired_state(self):
        s = self.auth.new_state()
        self.D.execute_query("UPDATE oura_oauth_state SET expires_at = %s", (datetime.now() - timedelta(minutes=1),))
        with mock.patch("requests.post") as post:
            r = self.cb(f"code={CODE}&state={s}")
            post.assert_not_called()
        self.assertIn("expired", self.flashes(r))

    def test_callback_access_denied(self):
        s = self.auth.new_state()
        with mock.patch("requests.post") as post:
            r = self.cb(f"error=access_denied&state={s}")
            post.assert_not_called()
        self.assertIn("You cancelled the connection", self.flashes(r))
        self.assertIn("Connect Oura", self.flashes(r))

    def test_callback_other_error(self):
        s = self.auth.new_state()
        r = self.cb(f"error=server_error&error_description=Oura+is+sad&state={s}")
        self.assertIn("Oura returned an error: Oura is sad", self.flashes(r))

    def test_callback_missing_code(self):
        s = self.auth.new_state()
        with mock.patch("requests.post") as post:
            r = self.cb(f"state={s}")
            post.assert_not_called()
        self.assertIn("did not send an authorization code", self.flashes(r))

    def test_callback_exchange_failure_hides_secrets(self):
        s = self.auth.new_state()
        with mock.patch("requests.post", return_value=Resp(400, {"error": "invalid_grant", "error_description": "code expired"})):
            r = self.cb(f"code={CODE}&state={s}")
        html = self.flashes(r)
        self.assertIn("Oura rejected the authorization (invalid_grant code expired)", html)
        for secret in (CODE, "SECRET_CLIENT_SHHH"):
            self.assertNotIn(secret, html)

    def test_callback_success_then_reload_is_harmless(self):
        s = self.auth.new_state()
        with mock.patch("requests.post", return_value=tokens()):
            r = self.cb(f"code={CODE}&state={s}")
        self.assertIn("Oura is connected", self.flashes(r))
        self.assertIn("connected", self.flashes(r))
        time.sleep(0.3)
        self.assertEqual(self.polls, [1])                      # first poll kicked off
        self.assertEqual(self.D.fetch_one("SELECT state FROM oura_auth WHERE id=1")["state"], "connected")
        with mock.patch("requests.post") as post:              # the browser reloads the callback URL
            r2 = self.cb(f"code={CODE}&state={s}")
            post.assert_not_called()
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(self.D.fetch_one("SELECT state FROM oura_auth WHERE id=1")["state"], "connected")
        for secret in (ACCESS, REFRESH, CODE, "SECRET_CLIENT_SHHH"):
            self.assertNotIn(secret, self.flashes(r) + self.flashes(r2))

    def test_non_loopback_is_refused(self):
        for path, method in (("/oauth/oura/start", "get"), (f"/oauth/oura/callback?state=x&code=y", "get"), ("/setup/oura/disconnect", "post")):
            r = getattr(self.c, method)(path, environ_base={"REMOTE_ADDR": "192.168.1.50"}, base_url="http://localhost:5000")
            self.assertEqual(r.status_code, 403, path)

    def test_start_refused_off_localhost_host(self):
        r = self.c.get("/oauth/oura/start", environ_base=self.loc, base_url="http://192.168.1.10:5000", follow_redirects=True)
        self.assertIn("Open this page as http://localhost:5000", r.get_data(as_text=True))
        self.assertEqual(self.D.fetch_one("SELECT COUNT(*) n FROM oura_oauth_state")["n"], 0)

    def test_start_redirects_to_oura_with_state(self):
        r = self.c.get("/oauth/oura/start", environ_base=self.loc, base_url="http://localhost:5000")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].startswith("https://cloud.ouraring.com/oauth/authorize?"))
        self.assertEqual(self.D.fetch_one("SELECT COUNT(*) n FROM oura_oauth_state")["n"], 1)

    def test_state_survives_gui_restart(self):
        from gui.app import create_app
        self.c.get("/oauth/oura/start", environ_base=self.loc, base_url="http://localhost:5000")
        state = self.D.fetch_one("SELECT state FROM oura_oauth_state")["state"]
        restarted = create_app(CFG).test_client()              # new process would also get a new secret key
        with mock.patch("requests.post", return_value=tokens()):
            restarted.get(f"/oauth/oura/callback?code={CODE}&state={state}", environ_base=self.loc, base_url="http://localhost:5000")
        self.assertEqual(self.D.fetch_one("SELECT state FROM oura_auth WHERE id=1")["state"], "connected")

    def test_port_mismatch_warning_and_no_flow(self):
        cfg = {**CFG, "supervisor": {"gui_port": 5050}}
        from gui.app import create_app
        c = create_app(cfg).test_client()
        r = c.get("/setup/oura", environ_base=self.loc, base_url="http://localhost:5050")
        html = r.get_data(as_text=True)
        self.assertIn("They must match the address registered with Oura exactly", html)   # warning banner shown
        self.assertIn("http://localhost:5050/oauth/oura/callback", html)
        r = c.get("/oauth/oura/start", environ_base=self.loc, base_url="http://localhost:5050", follow_redirects=True)
        self.assertIn("does not match the running GUI port", r.get_data(as_text=True))

    def test_disconnect_needs_confirmation_and_keeps_data(self):
        with mock.patch("requests.post", return_value=tokens()):
            self.auth.exchange_code(CFG, CODE)
        self.D.execute_query("INSERT INTO oura_daily (date, data_source, missing) VALUES (CURDATE(), 'api', FALSE) ON DUPLICATE KEY UPDATE missing=FALSE")
        r = self.c.post("/setup/oura/disconnect", data={}, environ_base=self.loc, base_url="http://localhost:5000", follow_redirects=True)
        self.assertIn("Tick the confirmation box", r.get_data(as_text=True))
        self.assertIsNotNone(self.D.fetch_one("SELECT id FROM oura_auth"))
        with mock.patch("requests.get", return_value=Resp(200, {})):
            self.c.post("/setup/oura/disconnect", data={"confirm": "yes"}, environ_base=self.loc, base_url="http://localhost:5000")
        self.assertIsNone(self.D.fetch_one("SELECT id FROM oura_auth"))
        self.assertEqual(self.D.fetch_one("SELECT COUNT(*) n FROM oura_daily")["n"], 1)


@unittest.skipUnless(TEST_DB, "set MIMIR_TEST_DB to a throwaway database to run these tests")
class NoSecretLeakTests(unittest.TestCase):
    """Runs the whole flow (connect, refresh, revoke, poller error paths) and greps every place a secret could land."""

    def test_no_secret_in_logs_db_or_aws_snapshot(self):
        import core.config as C
        C._cache[os.path.abspath(C.default_config_path())] = CFG
        import db.db as D
        D.apply_schema()
        from core import aws_push, oura_auth
        from pollers import oura
        for t in ("oura_auth", "oura_oauth_state", "error_log"):
            D.execute_query(f"DELETE FROM {t}")
        fallback = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "fallback.log")
        before = os.path.getsize(fallback) if os.path.exists(fallback) else 0
        with mock.patch("core.oura_auth.notifier.send_warning", return_value=True), mock.patch("core.notifier.send_warning", return_value=True):
            with mock.patch("requests.post", return_value=tokens()):
                oura_auth.exchange_code(CFG, CODE)
            D.execute_query("UPDATE oura_auth SET expires_at = %s", (datetime.now() + timedelta(minutes=1),))
            with mock.patch("requests.post", return_value=Resp(400, {"error": "invalid_grant"})):
                with self.assertRaises(oura_auth.NotConnected):
                    oura_auth.get_access_token(CFG)
            oura.run_cycle(CFG)
        with mock.patch("requests.post", return_value=tokens()):
            oura_auth.exchange_code(CFG, CODE)
        with mock.patch("requests.get", return_value=Resp(500, {})):
            try:
                oura.run_cycle(CFG)
            except Exception:
                pass
        snap = json.dumps(aws_push.build_snapshots(CFG), default=str)
        log_rows = json.dumps(D.fetch_all("SELECT * FROM error_log"), default=str)
        new_fallback = ""
        if os.path.exists(fallback):
            with open(fallback, encoding="utf-8") as f:
                new_fallback = f.read()[before:]
        for secret in (ACCESS, REFRESH, ACCESS2, REFRESH2, CODE, "SECRET_CLIENT_SHHH"):
            self.assertNotIn(secret, snap, "aws snapshot")
            self.assertNotIn(secret, log_rows, "error_log table")
            self.assertNotIn(secret, new_fallback, "logs/fallback.log")


if __name__ == "__main__":
    unittest.main()
