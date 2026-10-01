"""Oura OAuth2 (authorization code flow). Tokens live in the local oura_auth table (row id = 1), never in config, logs,
API responses, or AWS snapshots. The poller and the GUI are separate processes, so refreshing takes a row lock."""
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode

import requests

from core import notifier
from core.config import get
from db.db import execute_query, fetch_one, get_connection
from logs.error_handler import log_error

SCRIPT = "oura_auth"
TIMEOUT = 15
STATE_TTL_MIN = 10
REFRESH_MARGIN = timedelta(minutes=5)
DEFAULT_REDIRECT = "http://localhost:5000/oauth/oura/callback"
DEFAULT_SCOPES = ["daily"]
_pat_warned = False


class NotConnected(Exception):
    """No usable Oura connection: never authorized, or access was revoked. The poller skips quietly."""


class TemporaryError(Exception):
    """Network or server trouble. The connection state is unchanged; the next poll retries."""


class OAuthError(Exception):
    """The user-facing reason a code exchange failed. Never contains secrets."""


def _url(cfg, key, default):
    return get(cfg, f"oura.{key}", default) or default


def redirect_uri(cfg):
    return get(cfg, "oura.redirect_uri") or DEFAULT_REDIRECT


def scopes(cfg):
    raw = get(cfg, "oura.scopes", DEFAULT_SCOPES) or DEFAULT_SCOPES
    return raw.split() if isinstance(raw, str) else list(raw)


def configured(cfg):
    return bool(get(cfg, "oura.client_id") and get(cfg, "oura.client_secret"))


# ------------------------------------------------------------------ pending states
def new_state():
    state = secrets.token_urlsafe(32)
    execute_query("DELETE FROM oura_oauth_state WHERE expires_at < NOW()")
    execute_query("INSERT INTO oura_oauth_state (state, expires_at) VALUES (%s, %s)",
                  (state, datetime.now() + timedelta(minutes=STATE_TTL_MIN)))
    return state


def consume_state(state):
    """True once for a known, unexpired state; it is deleted either way so a code can never be replayed."""
    if not state:
        return False
    row = fetch_one("SELECT expires_at FROM oura_oauth_state WHERE state = %s", (state,))
    if not row:
        return False
    execute_query("DELETE FROM oura_oauth_state WHERE state = %s", (state,))
    return row["expires_at"] >= datetime.now()


def build_authorize_url(cfg, state):
    q = {"response_type": "code", "client_id": get(cfg, "oura.client_id"), "redirect_uri": redirect_uri(cfg),
         "scope": " ".join(scopes(cfg)), "state": state}
    return _url(cfg, "authorize_url", "https://cloud.ouraring.com/oauth/authorize") + "?" + urlencode(q)


# ------------------------------------------------------------------ token endpoint
def _token_request(cfg, data):
    """POST to the token endpoint. Returns (status_code, json). Network trouble raises TemporaryError."""
    data = dict(data, client_id=get(cfg, "oura.client_id"), client_secret=get(cfg, "oura.client_secret"))
    try:
        r = requests.post(_url(cfg, "token_url", "https://api.ouraring.com/oauth/token"), data=data, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise TemporaryError(f"could not reach Oura ({type(e).__name__})") from e
    try:
        body = r.json()
    except ValueError:
        body = {}
    return r.status_code, body if isinstance(body, dict) else {}


def _reason(body, status):
    """Short error text from an Oura error body. Only the error fields are used, never the whole response."""
    txt = f"{body.get('error', '')} {body.get('error_description', '')}".strip() or f"http {status}"
    return txt[:200]


def _store(cur, body, old_refresh=None, connected=False):
    expires = datetime.now() + timedelta(seconds=int(body.get("expires_in") or 86400))
    refresh = body.get("refresh_token") or old_refresh      # keep the old one if Oura sent no new one
    cur.execute(
        "INSERT INTO oura_auth (id, access_token, refresh_token, expires_at, scopes, connected_at, state, last_error) "
        "VALUES (1, %s, %s, %s, %s, NOW(), 'connected', NULL) "
        "ON DUPLICATE KEY UPDATE access_token = VALUES(access_token), refresh_token = VALUES(refresh_token), "
        "expires_at = VALUES(expires_at), scopes = COALESCE(VALUES(scopes), scopes), state = 'connected', last_error = NULL"
        + (", connected_at = NOW()" if connected else ""),
        (body["access_token"], refresh, expires, body.get("scope") or None))


def exchange_code(cfg, code):
    """Swap an authorization code for tokens and store them. Raises OAuthError or TemporaryError."""
    status, body = _token_request(cfg, {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(cfg)})
    if status >= 400 or not body.get("access_token"):
        raise OAuthError(f"Oura rejected the authorization ({_reason(body, status)}).")
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            if not body.get("scope"):
                body["scope"] = " ".join(scopes(cfg))
            _store(cur, body, connected=True)
        finally:
            cur.close()


def _notify_revoked(reason):
    """Runs once, right after the refresh transaction that marked the row revoked (later callers see revoked and stop)."""
    log_error(SCRIPT, "OuraRevoked", "refresh", reason, "critical")
    notifier.send_warning("Oura is disconnected, open /setup/oura to reconnect")


def refresh(cfg, bad_token=None):
    """Refresh under a row lock so two processes never spend the same single use refresh token.
    bad_token: the access token that just got a 401; if the stored one differs, someone already refreshed."""
    revoked = None
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("SELECT access_token, refresh_token, expires_at, state FROM oura_auth WHERE id = 1 FOR UPDATE")
            row = cur.fetchone()
            if not row or row["state"] == "revoked" or not row["refresh_token"]:
                raise NotConnected("Oura is not connected")
            fresh = row["expires_at"] and row["expires_at"] - datetime.now() > REFRESH_MARGIN
            if bad_token is None and fresh:
                return row["access_token"]              # another process refreshed while we waited for the lock
            if bad_token is not None and row["access_token"] != bad_token:
                return row["access_token"]
            status, body = _token_request(cfg, {"grant_type": "refresh_token", "refresh_token": row["refresh_token"]})
            if status == 401 or (status == 400 and body.get("error") in ("invalid_grant", "invalid_token")):
                revoked = f"refresh refused: {_reason(body, status)}"
                cur.execute("UPDATE oura_auth SET state = 'revoked', last_error = %s, access_token = NULL, refresh_token = NULL WHERE id = 1",
                            (revoked[:500],))
            elif status >= 500:
                raise TemporaryError(f"Oura token endpoint returned {status}")
            elif status >= 400 or not body.get("access_token"):
                cur.execute("UPDATE oura_auth SET last_error = %s WHERE id = 1", (_reason(body, status),))
                raise TemporaryError(f"Oura refresh failed ({_reason(body, status)})")
            else:
                _store(cur, body, old_refresh=row["refresh_token"])
                return body["access_token"]
        finally:
            cur.close()
    if revoked is not None:
        _notify_revoked(revoked)
        raise NotConnected("Oura access was revoked")


def get_access_token(cfg):
    """A valid access token, refreshing first if it expires within 5 minutes. Raises NotConnected or TemporaryError."""
    global _pat_warned
    row = fetch_one("SELECT access_token, expires_at, state FROM oura_auth WHERE id = 1")
    if not row:
        pat = get(cfg, "oura.pat")
        if pat:
            if not _pat_warned:
                _pat_warned = True
                log_error(SCRIPT, "PatFallback", "get_access_token", "using deprecated oura.pat; connect Oura at /setup/oura")
            return pat
        raise NotConnected("Oura is not connected")
    if row["state"] == "revoked" or not row["access_token"]:
        raise NotConnected("Oura access was revoked")
    if row["expires_at"] and row["expires_at"] - datetime.now() > REFRESH_MARGIN:
        return row["access_token"]
    return refresh(cfg)


def status(cfg):
    """Everything the GUI may show. Never includes a token."""
    row = fetch_one("SELECT state, scopes, connected_at, expires_at, last_error, access_token IS NOT NULL AS has_tokens "
                    "FROM oura_auth WHERE id = 1")
    wanted = set(scopes(cfg))
    if not row:
        return {"state": "not_connected", "scopes": [], "connected_at": None, "last_error": None, "needs_scopes": False}
    have = set((row["scopes"] or "").split())
    return {"state": row["state"] if row["has_tokens"] or row["state"] != "connected" else "not_connected",
            "scopes": sorted(have), "connected_at": row["connected_at"], "last_error": row["last_error"],
            "needs_scopes": bool(have) and not wanted <= have}


def revoke(cfg, delete_data=False):
    """Best effort revoke at Oura, then delete the local token row. Returns a short note for the GUI."""
    note = "Local tokens deleted."
    row = fetch_one("SELECT access_token FROM oura_auth WHERE id = 1")
    if row and row["access_token"]:
        try:
            r = requests.get(_url(cfg, "revoke_url", "https://api.ouraring.com/oauth/revoke"),
                             params={"access_token": row["access_token"]}, timeout=TIMEOUT)
            note = "Access revoked at Oura and local tokens deleted." if r.status_code < 400 else \
                "Oura did not confirm the revoke (you can also remove access in the Oura app). Local tokens deleted."
        except requests.RequestException:
            note = "Could not reach Oura to revoke (you can remove access in the Oura app). Local tokens deleted."
    execute_query("DELETE FROM oura_auth WHERE id = 1")
    execute_query("DELETE FROM oura_oauth_state")
    if delete_data:
        execute_query("DELETE FROM oura_intraday")
        execute_query("DELETE FROM oura_daily")
        note += " Stored Oura data deleted."
    return note
