"""Dashboard passwords. `python -m core.dashboard_passwords add <label> | remove <label> | list`. Only salted scrypt hashes are stored or sent."""
import getpass
import hashlib
import os
import re
import sys

import requests

from core.config import get, load_config
from db.db import execute_query, fetch_all
from logs.error_handler import log_error

SCRIPT = "dashboard_passwords"
TIMEOUT = 10
LABEL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def hash_password(password):
    salt = os.urandom(16)
    return salt.hex(), hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32).hex()


def push_passwords(cfg=None):
    """Sends the full list to AWS, which replaces its own list. An empty list revokes everyone."""
    cfg = cfg or load_config()
    if not get(cfg, "aws.enabled", True):
        return False
    url = (get(cfg, "aws.api_url") or "").rstrip("/")
    if not url:
        log_error(SCRIPT, "ConfigMissing", "push_passwords", "aws.api_url not configured")
        return False
    try:
        rows = fetch_all("SELECT label, salt, hash FROM dashboard_passwords ORDER BY label")
        r = requests.post(f"{url}/passwords", json={"passwords": rows}, timeout=TIMEOUT,
                          headers={"Authorization": f"Bearer {get(cfg, 'aws.api_token', '')}"})
        if r.status_code >= 300:
            raise RuntimeError(f"http {r.status_code}: {r.text[:200]}")
        return True
    except Exception as e:
        log_error(SCRIPT, type(e).__name__, "push_passwords", str(e))
        return False


def _add(label):
    if not LABEL_RE.match(label):
        print("label must be 1-64 characters of letters, digits, underscore, dot or dash", file=sys.stderr)
        return 1
    pw = getpass.getpass("Password: ")
    if not pw:
        print("password cannot be empty", file=sys.stderr)
        return 1
    if getpass.getpass("Again: ") != pw:
        print("passwords do not match", file=sys.stderr)
        return 1
    salt, digest = hash_password(pw)
    execute_query("REPLACE INTO dashboard_passwords (label, salt, hash, created_at) VALUES (%s, %s, %s, NOW())", (label, salt, digest))
    print(f"saved {label}")
    return _push_report()


def _remove(label):
    n = execute_query("DELETE FROM dashboard_passwords WHERE label = %s", (label,))
    print(f"removed {label}" if n else f"no such label: {label}")
    return _push_report()


def _push_report():
    if push_passwords():
        print("pushed to AWS")
        return 0
    print("push to AWS failed or is disabled; see the error log. Run `add`/`remove` again or restart Mimir to retry.", file=sys.stderr)
    return 1


def _list():
    rows = fetch_all("SELECT label, created_at FROM dashboard_passwords ORDER BY label")
    for r in rows:
        print(f"{r['label']}\t{r['created_at']}")
    if not rows:
        print("no dashboard passwords")
    return 0


def main(argv):
    if len(argv) == 2 and argv[0] == "add":
        return _add(argv[1])
    if len(argv) == 2 and argv[0] == "remove":
        return _remove(argv[1])
    if len(argv) == 1 and argv[0] == "list":
        return _list()
    print("usage: python -m core.dashboard_passwords add <label> | remove <label> | list")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
