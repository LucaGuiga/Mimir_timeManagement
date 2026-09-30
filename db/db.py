"""MySQL access. Never logs; the error handler depends on this module."""
import os
import re
import sys
import threading

import mysql.connector
from mysql.connector import errors as mysql_errors
from mysql.connector import pooling

from core.config import get, load_config, repo_root

_pool = None
_pool_lock = threading.Lock()
POOL_SIZE = 5
SCHEMA_PATH = os.path.join(repo_root(), "db", "schema.sql")
_LOST = (mysql_errors.OperationalError, mysql_errors.InterfaceError)


class DBError(Exception):
    pass


def _pool_instance():
    global _pool
    with _pool_lock:
        if _pool is None:
            cfg = load_config()
            try:
                _pool = pooling.MySQLConnectionPool(
                    pool_name="mimir", pool_size=POOL_SIZE, pool_reset_session=True,
                    host=get(cfg, "mysql.host", "127.0.0.1"),
                    port=int(get(cfg, "mysql.port", 3306)),
                    user=get(cfg, "mysql.user"), password=get(cfg, "mysql.password"),
                    database=get(cfg, "mysql.db"), charset="utf8mb4",
                    autocommit=False, connection_timeout=10,
                )
            except mysql_errors.Error as e:
                raise DBError(f"cannot create connection pool: {e}") from e
        return _pool


class get_connection:
    """Context manager: commits on clean exit, rolls back on exception."""

    def __init__(self):
        self.conn = None

    def __enter__(self):
        try:
            self.conn = _pool_instance().get_connection()
            if not self.conn.is_connected():
                self.conn.reconnect(attempts=1, delay=0)
        except mysql_errors.Error as e:
            raise DBError(f"cannot get connection: {e}") from e
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        except mysql_errors.Error:
            pass
        finally:
            try:
                self.conn.close()
            except mysql_errors.Error:
                pass
        return False


def _run(fn):
    """Run fn(conn) with one retry on a lost connection."""
    for attempt in (1, 2):
        try:
            with get_connection() as conn:
                return fn(conn)
        except DBError:
            raise
        except _LOST as e:
            if attempt == 2:
                raise DBError(f"connection lost: {e}") from e
        except mysql_errors.Error as e:
            raise DBError(str(e)) from e


def execute_query(sql, params=None):
    def fn(conn):
        cur = conn.cursor()
        try:
            cur.execute(sql, params or ())
            return cur.lastrowid if cur.lastrowid else cur.rowcount
        finally:
            cur.close()
    return _run(fn)


def execute_many(sql, seq_of_params):
    seq = list(seq_of_params)
    if not seq:
        return 0

    def fn(conn):
        cur = conn.cursor()
        try:
            cur.executemany(sql, seq)
            return cur.rowcount
        finally:
            cur.close()
    return _run(fn)


def fetch_all(sql, params=None):
    def fn(conn):
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(sql, params or ())
            return cur.fetchall()
        finally:
            cur.close()
    return _run(fn)


def fetch_one(sql, params=None):
    def fn(conn):
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(sql, params or ())
            row = cur.fetchone()
            cur.fetchall()
            return row
        finally:
            cur.close()
    return _run(fn)


def _split_statements(text):
    out, buf = [], []
    for line in text.splitlines():
        if line.strip().startswith("--"):
            continue
        buf.append(line)
        if line.rstrip().endswith(";"):
            stmt = "\n".join(buf).strip()
            if stmt.rstrip(";").strip():
                out.append(stmt)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        out.append(tail)
    return out


_ADD_COL = re.compile(r"^\s*ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(\w+)\s+(.*?);?\s*$", re.I | re.S)
_ADD_IDX = re.compile(r"^\s*ALTER\s+TABLE\s+(\w+)\s+ADD\s+(UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+(\w+)\s*(.*?);?\s*$", re.I | re.S)
_ADD_FK = re.compile(r"^\s*ALTER\s+TABLE\s+(\w+)\s+ADD\s+FOREIGN\s+KEY\s+IF\s+NOT\s+EXISTS\s+(\w+)\s*(.*?);?\s*$", re.I | re.S)


def execute_compat(cur, stmt):
    """Runs one statement. MariaDB's ADD COLUMN/INDEX/FOREIGN KEY IF NOT EXISTS is rewritten as an information_schema
    check plus a plain ALTER so the same SQL files work on MySQL 8."""
    def exists(sql, *params):
        cur.execute(sql, params)
        return cur.fetchone()[0] > 0
    m = _ADD_COL.match(stmt)
    if m:
        t, c, rest = m.groups()
        if exists("SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s", t, c):
            return
        stmt = f"ALTER TABLE {t} ADD COLUMN {c} {rest}"
    else:
        m = _ADD_IDX.match(stmt)
        if m:
            t, uniq, n, rest = m.groups()
            if exists("SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema = DATABASE() AND table_name = %s AND index_name = %s", t, n):
                return
            stmt = f"ALTER TABLE {t} ADD {uniq or ''}INDEX {n} {rest}"
        else:
            m = _ADD_FK.match(stmt)
            if m:
                t, n, rest = m.groups()
                if exists("SELECT COUNT(*) FROM information_schema.table_constraints WHERE table_schema = DATABASE() AND table_name = %s AND constraint_name = %s", t, n):
                    return
                stmt = f"ALTER TABLE {t} ADD CONSTRAINT {n} FOREIGN KEY {rest}"
    cur.execute(stmt)


def apply_schema(path=SCHEMA_PATH):
    with open(path, encoding="utf-8") as f:
        statements = _split_statements(f.read())

    def fn(conn):
        cur = conn.cursor()
        try:
            for stmt in statements:
                execute_compat(cur, stmt)
            return len(statements)
        finally:
            cur.close()
    return _run(fn)


def table_count():
    row = fetch_one("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema = DATABASE()")
    return row["n"] if row else 0


def _main(argv):
    if "--init" in argv:
        print(f"applied {apply_schema()} statements")
    if "--check" in argv:
        row = fetch_one("SELECT VERSION() AS v, DATABASE() AS d")
        print(f"connected to {row['d']} (MySQL {row['v']}), {table_count()} tables")
    if not ({"--init", "--check"} & set(argv)):
        print("usage: python -m db.db [--init] [--check]")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
