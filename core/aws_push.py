"""Snapshot push to the FastAPI app on AWS."""
from datetime import date, datetime, timedelta
from decimal import Decimal

import requests

from core import progress, stress
from core.config import get, load_config
from db.db import fetch_all, fetch_one
from logs.error_handler import log_error

SCRIPT = "aws_push"
TIMEOUT = 10


def _plain(v):
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, timedelta):
        return f"{v.seconds // 3600:02d}:{(v.seconds % 3600) // 60:02d}:{v.seconds % 60:02d}"
    if isinstance(v, Decimal):
        return float(v)
    return v


def build_snapshots(cfg=None):
    cfg = cfg or load_config()
    today, now = date.today(), datetime.now()
    snap = {}
    s = stress.latest_for(today)
    snap["stress_today"] = None if not s else {k: s[k] for k in ("date", "ratio", "band", "t_available", "t_awake",
                                                                  "deadline_term", "sleep_penalty", "hours_slept", "calculated_at")}
    snap["stress_history"] = fetch_all(
        "SELECT s.date, s.ratio FROM stress_scores s JOIN (SELECT date, MAX(calculated_at) AS m FROM stress_scores "
        "WHERE date >= %s GROUP BY date) l ON l.date = s.date AND l.m = s.calculated_at ORDER BY s.date", (today - timedelta(days=90),))
    upcoming = []
    for a in fetch_all("SELECT a.id, a.title, a.assignment_type, a.due_at, a.status, c.canvas_course_name AS course_name FROM assignments a "
                       "JOIN courses c ON c.id = a.course_id WHERE a.status IN ('pending','open') AND a.due_at BETWEEN %s AND %s "
                       "ORDER BY a.due_at", (now - timedelta(days=1), now + timedelta(days=14))):
        cs = progress.commit_summary_for_assignment(a["id"])
        upcoming.append({"course_name": a["course_name"], "title": a["title"], "assignment_type": a["assignment_type"],
                         "due_at": a["due_at"], "days_remaining": round((a["due_at"] - now).total_seconds() / 86400, 2),
                         "status": a["status"], "commit_count": cs["commit_count"], "last_commit_at": cs["last_commit_at"]})
    snap["assignments_upcoming"] = upcoming
    snap["schedule_today"] = fetch_all("SELECT label, time_category, start_time, end_time, moveable, skipped, skip_reason "
                                       "FROM schedule_blocks WHERE date = %s ORDER BY start_time", (today,))
    snap["commits_recent"] = fetch_all(
        "SELECT c.canvas_course_name AS course_name, a.title AS assignment_title, g.file_path, g.size_delta, g.file_size_bytes, "
        "g.commit_message, g.commit_timestamp, g.no_commit FROM github_commits g JOIN courses c ON c.id = g.course_id "
        "LEFT JOIN assignments a ON a.id = g.assignment_id ORDER BY g.id DESC LIMIT 50")
    snap["oura_recent"] = fetch_all("SELECT date, sleep_score, readiness_score, hrv_avg, resting_hr, total_sleep_seconds, missing, data_source "
                                    "FROM oura_daily ORDER BY date DESC LIMIT 14")
    crit = fetch_one("SELECT COUNT(*) AS n FROM error_log WHERE acknowledged = FALSE AND severity = 'critical'")
    latest = fetch_one("SELECT timestamp, script_name, error_type, raw_message FROM error_log WHERE acknowledged = FALSE "
                       "ORDER BY timestamp DESC LIMIT 1")
    snap["errors_active"] = {"critical_count": int(crit["n"] if crit else 0), "latest": latest}
    status = {r["api_name"]: r for r in fetch_all("SELECT api_name, state, last_cycle_at FROM poller_status")}
    avg = {r["api_name"]: r["avg_us"] for r in fetch_all(
        "SELECT api_name, AVG(response_time_us) AS avg_us FROM poll_metrics WHERE timestamp >= %s GROUP BY api_name", (now - timedelta(hours=1),))}
    snap["poll_metrics_summary"] = {api: {"last_poll": (status.get(api) or {}).get("last_cycle_at"),
                                          "avg_response_us": None if avg.get(api) is None else int(avg[api]),
                                          "state": (status.get(api) or {}).get("state")} for api in ("canvas", "github", "oura")}
    for k in ("schedule_today", "commits_recent", "oura_recent"):
        for r in snap[k]:
            for f in ("moveable", "skipped", "no_commit", "missing"):
                if f in r:
                    r[f] = bool(r[f])
    return _plain(snap)


def push_all(cfg=None):
    cfg = cfg or load_config()
    if not get(cfg, "aws.enabled", True):
        return False
    url = (get(cfg, "aws.api_url") or "").rstrip("/")
    if not url:
        log_error(SCRIPT, "ConfigMissing", "push_all", "aws.api_url not configured")
        return False
    try:
        body = {"snapshots": build_snapshots(cfg)}
    except Exception as e:
        log_error(SCRIPT, type(e).__name__, "build_snapshots", str(e))
        return False
    try:
        r = requests.post(f"{url}/ingest", json=body, timeout=TIMEOUT,
                          headers={"Authorization": f"Bearer {get(cfg, 'aws.api_token', '')}"})
        if r.status_code >= 300:
            raise RuntimeError(f"http {r.status_code}: {r.text[:200]}")
        return True
    except Exception as e:
        log_error(SCRIPT, type(e).__name__, "push_all", str(e))
        return False
