"""Mimir snapshot receiver and dashboard host. Run: uvicorn fastapi_app:app --host ... --port ..."""
import hashlib
import hmac
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

import yaml
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from store import Store

HERE = os.path.dirname(os.path.abspath(__file__))
KEYS = ("stress_today", "stress_history", "assignments_upcoming", "schedule_today",
        "commits_recent", "oura_recent", "errors_active", "poll_metrics_summary")
STALE_AFTER = timedelta(minutes=5)
COOKIE = "mimir_session"
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN = 2 ** 14, 8, 1, 32  # must match the local password CLI
LABEL_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
SALT_RE = re.compile(r"[0-9a-fA-F]{32}")
HASH_RE = re.compile(r"[0-9a-fA-F]{64}")


def _defaults(cfg):
    cfg.setdefault("host", "0.0.0.0")
    cfg.setdefault("port", 8000)
    cfg.setdefault("sqlite_path", os.path.join(HERE, "snapshots.sqlite"))
    cfg.setdefault("allowed_origins", [])
    cfg.setdefault("session_days", 30)
    cfg.setdefault("login_max_failures", 5)
    cfg.setdefault("login_window_minutes", 15)
    cfg.setdefault("dynamo_table", "")
    cfg.setdefault("frontend_dist", "")
    return cfg


def load_config(path=None):
    with open(path or os.path.join(HERE, "config.yaml"), encoding="utf-8") as f:
        cfg = _defaults(yaml.safe_load(f) or {})
    if not cfg.get("api_token"):
        raise RuntimeError("api/config.yaml: api_token is empty")
    return cfg


def load_config_from_env():
    """Lambda config: MIMIR_API_TOKEN (required), MIMIR_DYNAMO_TABLE, MIMIR_SESSION_DAYS, MIMIR_LOGIN_MAX_FAILURES, MIMIR_LOGIN_WINDOW_MINUTES, MIMIR_FRONTEND_DIST."""
    env = os.environ
    if not env.get("MIMIR_API_TOKEN"):
        raise RuntimeError("MIMIR_API_TOKEN is not set")
    return _defaults({
        "api_token": env["MIMIR_API_TOKEN"],
        "dynamo_table": env.get("MIMIR_DYNAMO_TABLE", ""),
        "session_days": int(env.get("MIMIR_SESSION_DAYS", 30)),
        "login_max_failures": int(env.get("MIMIR_LOGIN_MAX_FAILURES", 5)),
        "login_window_minutes": int(env.get("MIMIR_LOGIN_WINDOW_MINUTES", 15)),
        "frontend_dist": env.get("MIMIR_FRONTEND_DIST", ""),
    })


def hash_password(password, salt_hex):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN).hex()


def _sid_hash(sid):
    return hashlib.sha256(sid.encode()).hexdigest()


def create_app(cfg=None):
    cfg = cfg or load_config()
    cfg = _defaults(dict(cfg))
    days, max_failures, window = int(cfg["session_days"]), int(cfg["login_max_failures"]), int(cfg["login_window_minutes"]) * 60
    if cfg["dynamo_table"]:
        from dynamo_store import DynamoStore
        store = DynamoStore(cfg["dynamo_table"], failure_ttl_seconds=window)
    else:
        store = Store(cfg["sqlite_path"])
    app = FastAPI(title="Mimir API", docs_url=None, redoc_url=None)
    app.add_middleware(CORSMiddleware, allow_origins=list(cfg["allowed_origins"]), allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])

    def auth(request: Request):
        header = request.headers.get("authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else ""
        if not token or not secrets.compare_digest(token.encode(), str(cfg["api_token"]).encode()):
            raise HTTPException(status_code=401, detail="invalid bearer token")

    def session_label(request: Request):
        sid = request.cookies.get(COOKIE, "")
        sess = store.get_session(_sid_hash(sid)) if sid else None
        if sess and sess["expires_at"] > time.time():
            pw = next((p for p in store.get_passwords() if p["label"] == sess["label"]), None)
            if pw and hmac.compare_digest(pw["salt"].encode(), sess["salt"].encode()):
                return sess["label"]
        if sess:
            store.delete_session(_sid_hash(sid))
        raise HTTPException(status_code=401, detail="login required")

    def client_ip(request: Request):
        return request.client.host if request.client else "unknown"

    def log(ip, outcome, label=None):
        store.log_attempt(datetime.now(timezone.utc).isoformat(timespec="seconds"), ip, outcome, label)

    def wrap(key, slicer=None):
        hit = store.get(key)
        if not hit:
            return {"payload": None, "updated_at": None, "stale": True}
        payload, updated_at = hit
        age = datetime.now(timezone.utc) - datetime.fromisoformat(updated_at)
        return {"payload": slicer(payload) if slicer and payload is not None else payload, "updated_at": updated_at, "stale": age > STALE_AFTER}

    @app.post("/ingest", dependencies=[Depends(auth)])
    async def ingest(body: dict):
        snaps = body.get("snapshots") if isinstance(body, dict) else None
        if not isinstance(snaps, dict) or not snaps:
            raise HTTPException(status_code=400, detail="body must be {\"snapshots\": {key: payload}}")
        unknown = sorted(set(snaps) - set(KEYS))
        if unknown:
            raise HTTPException(status_code=400, detail=f"unknown snapshot keys: {', '.join(unknown)}")
        return {"written": store.set_many(snaps)}

    @app.post("/passwords", dependencies=[Depends(auth)])
    async def passwords(body: dict):
        items = body.get("passwords") if isinstance(body, dict) else None
        if not isinstance(items, list):
            raise HTTPException(status_code=400, detail="body must be {\"passwords\": [{label, salt, hash}]}")
        entries = []
        for e in items:
            ok = isinstance(e, dict) and all(isinstance(e.get(k), str) for k in ("label", "salt", "hash"))
            if not ok or not LABEL_RE.fullmatch(e["label"]) or not SALT_RE.fullmatch(e["salt"]) or not HASH_RE.fullmatch(e["hash"]):
                raise HTTPException(status_code=400, detail="each password needs label (1-64 of A-Za-z0-9_.-), salt (32 hex) and hash (64 hex)")
            entries.append({"label": e["label"], "salt": e["salt"].lower(), "hash": e["hash"].lower()})
        if len({e["label"] for e in entries}) != len(entries):
            raise HTTPException(status_code=400, detail="duplicate labels")
        store.replace_passwords(entries)
        return {"count": len(entries)}

    @app.get("/access_log", dependencies=[Depends(auth)])
    async def access_log(since: str = ""):
        return {"entries": store.access_log_since(since)}

    @app.post("/login")
    async def login(request: Request, body: dict):
        ip, now = client_ip(request), time.time()
        if store.count_failures(ip, now - window) >= max_failures:
            log(ip, "rate_limited")
            return JSONResponse({"detail": "too many attempts, try again later"}, status_code=429)
        password = body.get("password") if isinstance(body, dict) else None
        if not isinstance(password, str) or not password:
            raise HTTPException(status_code=400, detail="password required")
        match = None
        for p in store.get_passwords():  # every entry, no early exit
            if hmac.compare_digest(hash_password(password, p["salt"]).encode(), p["hash"].encode()) and match is None:
                match = p
        if not match:
            store.record_failure(ip, now)
            log(ip, "failure")
            return JSONResponse({"detail": "wrong password"}, status_code=401)
        sid = secrets.token_urlsafe(32)
        store.create_session(_sid_hash(sid), match["label"], match["salt"], int(now) + days * 86400)
        log(ip, "success", match["label"])
        resp = JSONResponse({"label": match["label"]})
        resp.headers.append("set-cookie", f"{COOKIE}={sid}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age={days * 86400}")
        return resp

    @app.post("/logout")
    async def logout(request: Request):
        sid = request.cookies.get(COOKIE, "")
        if sid:
            store.delete_session(_sid_hash(sid))
        resp = JSONResponse({"ok": True})
        resp.headers.append("set-cookie", f"{COOKIE}=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0")
        return resp

    @app.get("/session")
    async def session(label: str = Depends(session_label)):
        return {"label": label}

    @app.get("/stress/today", dependencies=[Depends(session_label)])
    async def stress_today():
        return wrap("stress_today")

    @app.get("/stress/history", dependencies=[Depends(session_label)])
    async def stress_history(days: int = Query(30, ge=1, le=365)):
        return wrap("stress_history", lambda rows: rows[-days:])

    @app.get("/assignments/upcoming", dependencies=[Depends(session_label)])
    async def assignments_upcoming():
        return wrap("assignments_upcoming")

    @app.get("/schedule/today", dependencies=[Depends(session_label)])
    async def schedule_today():
        return wrap("schedule_today")

    @app.get("/commits/recent", dependencies=[Depends(session_label)])
    async def commits_recent():
        return wrap("commits_recent")

    @app.get("/oura/recent", dependencies=[Depends(session_label)])
    async def oura_recent(days: int = Query(7, ge=1, le=14)):
        return wrap("oura_recent", lambda rows: rows[:days])

    @app.get("/errors/active", dependencies=[Depends(session_label)])
    async def errors_active():
        return wrap("errors_active")

    @app.get("/poll_metrics/summary", dependencies=[Depends(session_label)])
    async def poll_metrics_summary():
        return wrap("poll_metrics_summary")

    @app.get("/health")
    async def health():
        stamps = store.get_all_updated_at()
        return {"status": "ok", "keys": len(stamps), "oldest_updated_at": min(stamps.values()) if stamps else None}

    dist = cfg["frontend_dist"] or os.path.join(os.path.dirname(HERE), "frontend", "dist")
    if os.path.isdir(dist):
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    return app


app = None
try:
    app = create_app()
except (FileNotFoundError, RuntimeError):
    pass  # uvicorn import without a config; create_app(cfg) still works for tests

if __name__ == "__main__":
    import uvicorn
    cfg = load_config()
    uvicorn.run("fastapi_app:app", host=cfg["host"], port=int(cfg["port"]))
