"""Rolling professor posting profiles from assignment, announcement, and change events."""
import json
from datetime import datetime

from db.db import execute_many, execute_query, fetch_all, fetch_one
from logs.error_handler import log_error

SCRIPT = "professor_profiler"
DAY_SHARE = 0.20


def _events():
    ev = []
    for r in fetch_all("SELECT id, course_id, COALESCE(canvas_posted_at, created_at) AS ts, syllabus_predicted_open, available_from "
                       "FROM assignments WHERE profiled = FALSE ORDER BY ts"):
        ev.append(("assignments", r))
    for r in fetch_all("SELECT id, course_id, COALESCE(posted_at, detected_at) AS ts FROM announcements WHERE profiled = FALSE ORDER BY ts"):
        ev.append(("announcements", r))
    for r in fetch_all("SELECT ac.id, a.course_id, ac.detected_at AS ts FROM assignment_changes ac "
                       "JOIN assignments a ON a.id = ac.assignment_id WHERE ac.profiled = FALSE ORDER BY ac.detected_at"):
        ev.append(("assignment_changes", r))
    ev.sort(key=lambda e: e[1]["ts"] or datetime.min)
    return ev


def _load_hist(prof):
    h = prof.get("post_histogram")
    if isinstance(h, str):
        try:
            h = json.loads(h)
        except ValueError:
            h = None
    h = h or {}
    h.setdefault("days", [0] * 7)
    h.setdefault("hours", [0] * 24)
    for k in ("early_n", "late_n", "adh_n"):
        h.setdefault(k, 0)
    return h


def _percentile_hour(hours, q):
    total = sum(hours)
    if total == 0:
        return None
    acc = 0
    for h, n in enumerate(hours):
        acc += n
        if acc >= q * total:
            return h
    return 23


def _rolling(old, old_n, new_sum, new_n):
    if old_n + new_n == 0:
        return old
    return ((old or 0.0) * old_n + new_sum) / (old_n + new_n)


def _apply(prof_id, batch):
    prof = fetch_one("SELECT * FROM professor_profiles WHERE id = %s", (prof_id,))
    if not prof:
        return
    hist = _load_hist(prof)
    for ts in batch["timestamps"]:
        hist["days"][ts.weekday()] += 1
        hist["hours"][ts.hour] += 1
    n_old, n_new = int(prof["observation_count"] or 0), len(batch["timestamps"])
    n = n_old + n_new
    early = [-x for x in batch["deltas"] if x < 0]
    late = [x for x in batch["deltas"] if x > 0]
    adherence = [max(0.0, 1 - abs(x) / 7) for x in batch["deltas"]]
    avg_early = _rolling(prof["avg_early_post_days"], hist["early_n"], sum(early), len(early))
    avg_late = _rolling(prof["avg_late_post_days"], hist["late_n"], sum(late), len(late))
    adh = _rolling(prof["syllabus_adherence_score"], hist["adh_n"], sum(adherence), len(adherence))
    hist["early_n"] += len(early)
    hist["late_n"] += len(late)
    hist["adh_n"] += len(adherence)
    days = [d for d in range(7) if n and hist["days"][d] >= DAY_SHARE * n]
    h_start, h_end = _percentile_hour(hist["hours"], 0.10), _percentile_hour(hist["hours"], 0.90)
    if h_end is not None:
        h_end = min(24, h_end + 1)
    execute_query(
        "UPDATE professor_profiles SET syllabus_adherence_score=%s, avg_early_post_days=%s, avg_late_post_days=%s, "
        "typical_post_days=%s, typical_post_hour_start=%s, typical_post_hour_end=%s, observation_count=%s, "
        "post_histogram=%s, last_updated=NOW() WHERE id=%s",
        (adh, avg_early, avg_late, json.dumps(days), h_start, h_end, n, json.dumps(hist), prof_id))
    execute_query("DELETE FROM hot_zones WHERE professor_id = %s AND source = 'profiler'", (prof_id,))
    if h_start is not None and days:
        execute_many("INSERT INTO hot_zones (professor_id, day_of_week, hour_start, hour_end, source) VALUES (%s,%s,%s,%s,'profiler')",
                     [(prof_id, d, h_start, h_end) for d in days])


def sweep():
    events = _events()
    if not events:
        return 0
    course_prof = {r["id"]: r["professor_id"] for r in fetch_all("SELECT id, professor_id FROM courses")}
    batches, done = {}, {"assignments": [], "announcements": [], "assignment_changes": []}
    for table, r in events:
        try:
            prof_id = course_prof.get(r["course_id"])
            if prof_id and r["ts"]:
                b = batches.setdefault(prof_id, {"timestamps": [], "deltas": []})
                b["timestamps"].append(r["ts"])
                if table == "assignments" and r.get("syllabus_predicted_open") and r.get("available_from"):
                    b["deltas"].append((r["available_from"] - r["syllabus_predicted_open"]).total_seconds() / 86400)
            done[table].append(r["id"])
        except Exception as e:
            log_error(SCRIPT, type(e).__name__, f"event {table} {r.get('id')}", str(e))
    for prof_id, batch in batches.items():
        try:
            _apply(prof_id, batch)
        except Exception as e:
            log_error(SCRIPT, type(e).__name__, f"apply professor {prof_id}", str(e))
    for table, ids in done.items():
        if ids:
            ph = ",".join(["%s"] * len(ids))
            execute_query(f"UPDATE {table} SET profiled = TRUE WHERE id IN ({ph})", ids)
    return len(events)
