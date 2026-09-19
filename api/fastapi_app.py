"""Athena snapshot receiver and dashboard host. Run: uvicorn fastapi_app:app --host ... --port ..."""
import os
import secrets
from datetime import datetime, timedelta, timezone

import yaml
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from store import Store

HERE = os.path.dirname(os.path.abspath(__file__))
KEYS = ("stress_today", "stress_history", "assignments_upcoming", "schedule_today",
        "commits_recent", "oura_recent", "errors_active", "poll_metrics_summary")
STALE_AFTER = timedelta(minutes=5)


def load_config(path=None):
    with open(path or os.path.join(HERE, "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("host", "0.0.0.0")
    cfg.setdefault("port", 8000)
    cfg.setdefault("sqlite_path", os.path.join(HERE, "snapshots.sqlite"))
    cfg.setdefault("allowed_origins", [])
    if not cfg.get("api_token"):
        raise RuntimeError("api/config.yaml: api_token is empty")
    return cfg


def create_app(cfg=None):
    cfg = cfg or load_config()
    store = Store(cfg["sqlite_path"])
    app = FastAPI(title="Athena API", docs_url=None, redoc_url=None)
    app.add_middleware(CORSMiddleware, allow_origins=list(cfg["allowed_origins"]), allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])

    def auth(request: Request):
        header = request.headers.get("authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else ""
        if not token or not secrets.compare_digest(token.encode(), str(cfg["api_token"]).encode()):
            raise HTTPException(status_code=401, detail="invalid bearer token")

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

    @app.get("/stress/today", dependencies=[Depends(auth)])
    async def stress_today():
        return wrap("stress_today")

    @app.get("/stress/history", dependencies=[Depends(auth)])
    async def stress_history(days: int = Query(30, ge=1, le=365)):
        return wrap("stress_history", lambda rows: rows[-days:])

    @app.get("/assignments/upcoming", dependencies=[Depends(auth)])
    async def assignments_upcoming():
        return wrap("assignments_upcoming")

    @app.get("/schedule/today", dependencies=[Depends(auth)])
    async def schedule_today():
        return wrap("schedule_today")

    @app.get("/commits/recent", dependencies=[Depends(auth)])
    async def commits_recent():
        return wrap("commits_recent")

    @app.get("/oura/recent", dependencies=[Depends(auth)])
    async def oura_recent(days: int = Query(7, ge=1, le=14)):
        return wrap("oura_recent", lambda rows: rows[:days])

    @app.get("/errors/active", dependencies=[Depends(auth)])
    async def errors_active():
        return wrap("errors_active")

    @app.get("/poll_metrics/summary", dependencies=[Depends(auth)])
    async def poll_metrics_summary():
        return wrap("poll_metrics_summary")

    @app.get("/health")
    async def health():
        stamps = store.get_all_updated_at()
        return {"status": "ok", "keys": len(stamps), "oldest_updated_at": min(stamps.values()) if stamps else None}

    dist = os.path.join(os.path.dirname(HERE), "frontend", "dist")
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
