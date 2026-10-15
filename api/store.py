"""SQLite store: latest snapshot per key, dashboard passwords, sessions, login failures and the access log."""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

_lock = threading.Lock()
LOG_LIMIT = 500
FAILURE_KEEP_SECONDS = 86400


class Store:
    def __init__(self, path):
        self.path = path
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS snapshots (key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS passwords (label TEXT PRIMARY KEY, salt TEXT NOT NULL, hash TEXT NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS sessions (sid_hash TEXT PRIMARY KEY, label TEXT NOT NULL, salt TEXT NOT NULL, expires_at INTEGER NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS login_failures (id INTEGER PRIMARY KEY AUTOINCREMENT, ip TEXT NOT NULL, ts REAL NOT NULL)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_login_failures_ip ON login_failures (ip, ts)")
            c.execute("CREATE TABLE IF NOT EXISTS access_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, ip TEXT NOT NULL, outcome TEXT NOT NULL, label TEXT)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_access_log_ts ON access_log (ts)")

    @contextmanager
    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def set_many(self, snapshots):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with _lock, self._conn() as c:
            c.executemany("INSERT INTO snapshots (key, payload, updated_at) VALUES (?, ?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at",
                          [(k, json.dumps(v), now) for k, v in snapshots.items()])
        return sorted(snapshots)

    def get(self, key):
        with self._conn() as c:
            row = c.execute("SELECT payload, updated_at FROM snapshots WHERE key = ?", (key,)).fetchone()
        return (json.loads(row["payload"]), row["updated_at"]) if row else None

    def get_all_updated_at(self):
        with self._conn() as c:
            return {r["key"]: r["updated_at"] for r in c.execute("SELECT key, updated_at FROM snapshots")}

    def replace_passwords(self, entries):
        with _lock, self._conn() as c:
            c.execute("DELETE FROM passwords")
            c.executemany("INSERT INTO passwords (label, salt, hash) VALUES (?, ?, ?)", [(e["label"], e["salt"], e["hash"]) for e in entries])

    def get_passwords(self):
        with self._conn() as c:
            return [{"label": r["label"], "salt": r["salt"], "hash": r["hash"]} for r in c.execute("SELECT label, salt, hash FROM passwords ORDER BY label")]

    def create_session(self, sid_hash, label, salt, expires_at_epoch):
        with _lock, self._conn() as c:
            c.execute("DELETE FROM sessions WHERE expires_at < CAST(strftime('%s', 'now') AS INTEGER)")
            c.execute("INSERT OR REPLACE INTO sessions (sid_hash, label, salt, expires_at) VALUES (?, ?, ?, ?)", (sid_hash, label, salt, int(expires_at_epoch)))

    def get_session(self, sid_hash):
        with self._conn() as c:
            row = c.execute("SELECT label, salt, expires_at FROM sessions WHERE sid_hash = ?", (sid_hash,)).fetchone()
        return {"label": row["label"], "salt": row["salt"], "expires_at": row["expires_at"]} if row else None

    def delete_session(self, sid_hash):
        with _lock, self._conn() as c:
            c.execute("DELETE FROM sessions WHERE sid_hash = ?", (sid_hash,))

    def record_failure(self, ip, now_epoch):
        with _lock, self._conn() as c:
            c.execute("DELETE FROM login_failures WHERE ts < ?", (now_epoch - FAILURE_KEEP_SECONDS,))
            c.execute("INSERT INTO login_failures (ip, ts) VALUES (?, ?)", (ip, now_epoch))

    def count_failures(self, ip, since_epoch):
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM login_failures WHERE ip = ? AND ts >= ?", (ip, since_epoch)).fetchone()[0]

    def log_attempt(self, ts_iso, ip, outcome, label):
        with _lock, self._conn() as c:
            c.execute("INSERT INTO access_log (ts, ip, outcome, label) VALUES (?, ?, ?, ?)", (ts_iso, ip, outcome, label))

    def access_log_since(self, since_iso):
        with self._conn() as c:
            rows = c.execute("SELECT ts, ip, outcome, label FROM access_log WHERE ts > ? ORDER BY ts, id LIMIT ?", (since_iso or "", LOG_LIMIT)).fetchall()
        return [{"ts": r["ts"], "ip": r["ip"], "outcome": r["outcome"], "label": r["label"]} for r in rows]
