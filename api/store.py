"""SQLite store for the latest snapshot per key."""
import json
import sqlite3
import threading
from datetime import datetime, timezone

_lock = threading.Lock()


class Store:
    def __init__(self, path):
        self.path = path
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS snapshots (key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL)")

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

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
