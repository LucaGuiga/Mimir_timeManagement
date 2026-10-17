"""Rolling professor posting profiles from assignment, announcement, and change events."""
import json
from datetime import datetime

from db.db import execute_many, execute_query, fetch_all, fetch_one
from logs.error_handler import log_error

SCRIPT = "professor_profiler"
DAY_SHARE = 0.20


def _events():
    ev = []
    # When Canvas gives no posted time, the time Mimir first saw the assignment stands in for it, except for the course's
    # first import (everything already posted when the course was added), which says nothing about when the professor posts.
    for r in fetch_all("SELECT a.id, a.course_id, CASE WHEN a.canvas_posted_at IS NOT NULL THEN a.canvas_posted_at "
                       "WHEN a.created_at > f.first_seen + INTERVAL 1 HOUR THEN a.created_at END AS ts, a.syllabus_predicted_open, a.available_from "
                       "FROM assignments a JOIN (SELECT course_id, MIN(created_at) AS first_seen FROM assignments GROUP BY course_id) f "
                       "ON f.course_id = a.course_id WHERE a.profiled = FALSE ORDER BY ts"):
        ev.append(("assignments", r))
    first = "(SELECT course_id, MIN(created_at) AS first_seen FROM assignments GROUP BY course_id) f"   # same first import rule
    for r in fetch_all("SELECT n.id, n.course_id, CASE WHEN n.posted_at IS NOT NULL THEN n.posted_at "
                       "WHEN n.detected_at > f.first_seen + INTERVAL 1 HOUR THEN n.detected_at END AS ts "
                       f"FROM announcements n LEFT JOIN {first} ON f.course_id = n.course_id WHERE n.profiled = FALSE ORDER BY ts"):
        ev.append(("announcements", r))
    for r in fetch_all("SELECT ac.id, a.course_id, CASE WHEN ac.detected_at > f.first_seen + INTERVAL 1 HOUR THEN ac.detected_at END AS ts "
                       f"FROM assignment_changes ac JOIN assignments a ON a.id = ac.assignment_id LEFT JOIN {first} ON f.course_id = a.course_id "
                       "WHERE ac.profiled = FALSE ORDER BY ac.detected_at"):
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
    days = [d for d in range(7) if n and hist["days"][d] >= DAY_SHARE * n]
    h_start, h_end = _percentile_hour(hist["hours"], 0.10), _percentile_hour(hist["hours"], 0.90)
    if h_end is not None:
        h_end = min(24, h_end + 1)
    execute_query(
        "UPDATE professor_profiles SET typical_post_days=%s, typical_post_hour_start=%s, typical_post_hour_end=%s, observation_count=%s, "
        "post_histogram=%s, last_updated=NOW() WHERE id=%s",
        (json.dumps(days), h_start, h_end, n, json.dumps(hist), prof_id))
    execute_query("DELETE FROM hot_zones WHERE professor_id = %s AND source = 'profiler'", (prof_id,))
    if h_start is not None and days:
        execute_many("INSERT INTO hot_zones (professor_id, day_of_week, hour_start, hour_end, source) VALUES (%s,%s,%s,%s,'profiler')",
                     [(prof_id, d, h_start, h_end) for d in days])


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def syllabus_stats(rows):
    """Accuracy against the syllabus from matched assignments, as profile columns. A delta is Canvas minus syllabus
    in days, so positive means later than the syllabus said."""
    days = lambda a, b: (a - b).total_seconds() / 86400
    opens = [days(r["available_from"], r["syllabus_predicted_open"]) for r in rows if r["available_from"] and r["syllabus_predicted_open"]]
    dues = [days(r["due_at"], r["syllabus_predicted_due"]) for r in rows if r["due_at"] and r["syllabus_predicted_due"]]
    both = opens + dues
    return {"syllabus_adherence_score": _mean([max(0.0, 1 - abs(x) / 7) for x in both]),
            "avg_early_post_days": _mean([-x for x in opens if x < 0]),
            "avg_late_post_days": _mean([x for x in opens if x > 0]),
            "avg_due_shift_days": _mean(dues),
            "due_on_time_share": _mean([1.0 if abs(x) <= 1 else 0.0 for x in dues]),
            "syllabus_matched_count": len(rows)}


def refresh_syllabus_stats():
    """Recomputes every profile's syllabus accuracy from all matched assignments, so a match made later still counts."""
    for p in fetch_all("SELECT DISTINCT professor_id AS id FROM courses WHERE professor_id IS NOT NULL"):
        rows = fetch_all("SELECT a.available_from, a.syllabus_predicted_open, a.due_at, a.syllabus_predicted_due FROM assignments a "
                         "JOIN courses c ON c.id = a.course_id WHERE c.professor_id = %s "
                         "AND (a.syllabus_predicted_open IS NOT NULL OR a.syllabus_predicted_due IS NOT NULL)", (p["id"],))
        st = syllabus_stats(rows)
        execute_query("UPDATE professor_profiles SET " + ", ".join(f"{k}=%s" for k in st) + " WHERE id=%s", list(st.values()) + [p["id"]])


def sweep():
    try:
        refresh_syllabus_stats()
    except Exception as e:
        log_error(SCRIPT, type(e).__name__, "syllabus stats", str(e))
    events = _events()
    if not events:
        return 0
    course_prof = {r["id"]: r["professor_id"] for r in fetch_all("SELECT id, professor_id FROM courses")}
    batches, done = {}, {"assignments": [], "announcements": [], "assignment_changes": []}
    for table, r in events:
        try:
            prof_id = course_prof.get(r["course_id"])
            if not prof_id:
                continue   # no professor linked yet: keep it for when the syllabus names one
            if r["ts"]:
                batches.setdefault(prof_id, {"timestamps": []})["timestamps"].append(r["ts"])
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
