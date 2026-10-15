"""API auth tests: sessions, rate limiting, revocation, token scope, access log. Run: python -m unittest tests.test_api_auth"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api"))

from fastapi.testclient import TestClient

from fastapi_app import create_app, hash_password

TOKEN = "machine-token-for-tests"
BEARER = {"Authorization": f"Bearer {TOKEN}"}
SALT_ME, SALT_DAD = "00" * 16, "11" * 16
DATA_ROUTES = ["/stress/today", "/stress/history", "/assignments/upcoming", "/schedule/today",
               "/commits/recent", "/oura/recent", "/errors/active", "/poll_metrics/summary", "/session"]


def entry(label, password, salt):
    return {"label": label, "salt": salt, "hash": hash_password(password, salt)}


class ApiAuthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = {"api_token": TOKEN, "sqlite_path": os.path.join(self.tmp.name, "t.sqlite"), "frontend_dist": self.tmp.name}
        self.client = TestClient(create_app(self.cfg), base_url="https://testserver")
        self.push([entry("me", "correct horse", SALT_ME), entry("dad", "battery staple", SALT_DAD)])

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def push(self, entries):
        r = self.client.post("/passwords", json={"passwords": entries}, headers=BEARER)
        self.assertEqual(r.status_code, 200)
        return r

    def login(self, password="correct horse"):
        return self.client.post("/login", json={"password": password})

    def test_login_success_sets_cookie_and_unlocks_data(self):
        self.assertEqual(self.client.get("/stress/today").status_code, 401)
        r = self.login()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"label": "me"})
        cookie = r.headers["set-cookie"]
        for part in ("mimir_session=", "HttpOnly", "Secure", "SameSite=Strict", "Path=/", f"Max-Age={30 * 86400}"):
            self.assertIn(part, cookie)
        for path in DATA_ROUTES:
            self.assertEqual(self.client.get(path).status_code, 200, path)
        self.assertEqual(self.client.get("/session").json(), {"label": "me"})
        self.assertEqual(self.client.get("/stress/today").json()["payload"], None)

    def test_wrong_password_401_and_empty_400(self):
        r = self.login("nope")
        self.assertEqual(r.status_code, 401)
        self.assertNotIn("set-cookie", r.headers)
        self.assertEqual(self.client.post("/login", json={}).status_code, 400)
        self.assertEqual(self.client.post("/login", json={"password": ""}).status_code, 400)

    def test_rate_limit_after_five_failures(self):
        for _ in range(5):
            self.assertEqual(self.login("bad").status_code, 401)
        self.assertEqual(self.login("bad").status_code, 429)
        r = self.login("correct horse")
        self.assertEqual(r.status_code, 429)
        self.assertNotIn("set-cookie", r.headers)
        self.assertEqual(self.client.get("/session").status_code, 401)

    def test_revocation_ends_session(self):
        self.login()
        self.assertEqual(self.client.get("/session").status_code, 200)
        self.push([entry("dad", "battery staple", SALT_DAD)])
        self.assertEqual(self.client.get("/session").status_code, 401)
        self.assertEqual(self.login().status_code, 401)

    def test_changed_password_ends_old_session(self):
        self.login()
        self.push([entry("me", "new password", "22" * 16)])
        self.assertEqual(self.client.get("/session").status_code, 401)
        self.assertEqual(self.login("new password").status_code, 200)

    def test_empty_list_revokes_everyone(self):
        self.login()
        self.assertEqual(self.push([]).json(), {"count": 0})
        self.assertEqual(self.client.get("/session").status_code, 401)

    def test_data_routes_refuse_machine_token(self):
        for path in DATA_ROUTES:
            self.assertEqual(self.client.get(path, headers=BEARER).status_code, 401, path)

    def test_machine_routes_refuse_session_without_token(self):
        self.login()
        self.assertEqual(self.client.post("/ingest", json={"snapshots": {"stress_today": {}}}).status_code, 401)
        self.assertEqual(self.client.post("/passwords", json={"passwords": []}).status_code, 401)
        self.assertEqual(self.client.get("/access_log").status_code, 401)
        self.assertEqual(self.client.get("/session").status_code, 200)

    def test_ingest_then_session_reads(self):
        r = self.client.post("/ingest", json={"snapshots": {"stress_today": {"ratio": 1}}}, headers=BEARER)
        self.assertEqual(r.json(), {"written": ["stress_today"]})
        self.assertEqual(self.client.post("/ingest", json={"snapshots": {"bogus": 1}}, headers=BEARER).status_code, 400)
        self.login()
        got = self.client.get("/stress/today").json()
        self.assertEqual(got["payload"], {"ratio": 1})
        self.assertFalse(got["stale"])
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_passwords_validation(self):
        good = entry("me", "x", SALT_ME)
        bad = [{"label": "", "salt": SALT_ME, "hash": good["hash"]},
               {"label": "a b", "salt": SALT_ME, "hash": good["hash"]},
               {"label": "x" * 65, "salt": SALT_ME, "hash": good["hash"]},
               {"label": "me", "salt": "00", "hash": good["hash"]},
               {"label": "me", "salt": SALT_ME, "hash": "zz" * 32},
               {"label": "me", "salt": SALT_ME}]
        for e in bad:
            self.assertEqual(self.client.post("/passwords", json={"passwords": [e]}, headers=BEARER).status_code, 400, e)
        self.assertEqual(self.client.post("/passwords", json={"passwords": [good, good]}, headers=BEARER).status_code, 400)
        self.assertEqual(self.client.post("/passwords", json={}, headers=BEARER).status_code, 400)

    def test_access_log_records_outcomes_without_secrets(self):
        self.login("wrong-secret-pw")
        self.login()
        for _ in range(4):
            self.login("bad")
        self.login("bad")
        r = self.client.get("/access_log", headers=BEARER)
        entries = r.json()["entries"]
        self.assertEqual([e["outcome"] for e in entries], ["failure", "success", "failure", "failure", "failure", "failure", "rate_limited"])
        self.assertEqual([e["label"] for e in entries][:2], [None, "me"])
        self.assertTrue(all(e["label"] is None for e in entries if e["outcome"] != "success"))
        self.assertEqual(set(entries[0]), {"ts", "ip", "outcome", "label"})
        text = r.text
        for secret in ("wrong-secret-pw", "correct horse", "bad", hash_password("correct horse", SALT_ME)):
            self.assertNotIn(secret, text)
        self.assertEqual(self.client.get("/access_log", params={"since": entries[-1]["ts"]}, headers=BEARER).json()["entries"], [])
        self.assertEqual(len(self.client.get("/access_log", params={"since": "2000-01-01T00:00:00+00:00"}, headers=BEARER).json()["entries"]), 7)

    def test_logout_ends_session(self):
        self.login()
        r = self.client.post("/logout")
        self.assertEqual(r.json(), {"ok": True})
        self.assertIn("Max-Age=0", r.headers["set-cookie"])
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/session").status_code, 401)

    def test_logout_invalidates_server_side(self):
        sid = self.login().cookies["mimir_session"]
        self.client.post("/logout")
        self.client.cookies.set("mimir_session", sid)
        self.assertEqual(self.client.get("/session").status_code, 401)

    def test_session_expires(self):
        self.cfg["session_days"] = 0
        client = TestClient(create_app(self.cfg), base_url="https://testserver")
        self.assertEqual(client.post("/login", json={"password": "correct horse"}).status_code, 200)
        self.assertEqual(client.get("/session").status_code, 401)


if __name__ == "__main__":
    unittest.main()
