"""Canvas poller cycle. Writes courses, professor_profiles, assignments, assignment_changes, announcements."""
import re
import time
from datetime import date, datetime, timezone

import requests

from core import notifier
from core.config import get
from db.db import execute_query, fetch_all, fetch_one
from logs.error_handler import log_error

SCRIPT = "canvas"
TIMEOUT = 15
_notified_unmapped = set()
_group_cache = {}        # (canvas_course_id, date) -> {group_id: name}
_syllabus_checked = {}   # canvas_course_id -> date
syllabus_sources = {}    # canvas_course_id -> {"syllabus_body": str|None, "files": [...]}
_warned_rate_this_cycle = False


class AuthFailure(Exception):
    pass


class _RateLimited(Exception):
    def __init__(self, seconds):
        super().__init__(f"rate limited, retry after {seconds}s")
        self.seconds = seconds


def _to_local(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
    except ValueError:
        return None


def _metric(endpoint, t0, status):
    try:
        execute_query("INSERT INTO poll_metrics (api_name, endpoint, response_time_us, http_status) VALUES (%s,%s,%s,%s)",
                      ("canvas", endpoint[:512], (time.perf_counter_ns() - t0) // 1000, status))
    except Exception as e:
        log_error(SCRIPT, type(e).__name__, "poll_metrics insert", str(e))


def _request(cfg, path_or_url, params=None):
    global _warned_rate_this_cycle
    base = get(cfg, "canvas.base_url", "https://canvas.ucsc.edu").rstrip("/")
    url = path_or_url if path_or_url.startswith("http") else base + path_or_url
    headers = {"Authorization": f"Bearer {get(cfg, 'canvas.token')}"}
    t0 = time.perf_counter_ns()
    try:
        r = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)
    except requests.RequestException as e:
        _metric(url.split("?")[0].replace(base, ""), t0, 0)
        raise RuntimeError(f"canvas request failed: {e}") from e
    _metric(r.url.split("?")[0].replace(base, ""), t0, r.status_code)
    if r.status_code == 401:
        raise AuthFailure(f"canvas 401 on {url}")
    if r.status_code == 403 and r.headers.get("Retry-After"):
        raise _RateLimited(float(r.headers["Retry-After"]))
    remaining = r.headers.get("X-Rate-Limit-Remaining")
    if remaining is not None and not _warned_rate_this_cycle:
        try:
            if float(remaining) < float(get(cfg, "canvas.rate_limit_warn_threshold", 100)):
                _warned_rate_this_cycle = True
                log_error(SCRIPT, "RateLimitLow", "request", f"X-Rate-Limit-Remaining={remaining}")
        except ValueError:
            pass
    if r.status_code >= 400:
        raise RuntimeError(f"canvas {r.status_code} on {r.url}: {r.text[:200]}")
    return r


def _paged(cfg, path, params=None):
    out, r = [], _request(cfg, path, params)
    out.extend(r.json())
    while r.links.get("next"):
        r = _request(cfg, r.links["next"]["url"])
        out.extend(r.json())
    return out


def _upsert_professor(name):
    return execute_query(
        "INSERT INTO professor_profiles (professor_name, observation_count) VALUES (%s, 0) "
        "ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)", (name[:255],))


def _sync_courses(cfg):
    canvas_courses = _paged(cfg, "/api/v1/courses",
                            {"enrollment_state": "active", "include[]": "teachers", "per_page": 100})
    quarter = get(cfg, "quarter_label", "")
    seen_ids, rows = [], []
    for c in canvas_courses:
        if not c.get("id") or c.get("access_restricted_by_date"):
            continue
        cid, name = int(c["id"]), (c.get("name") or c.get("course_code") or str(c["id"]))[:255]
        existing = fetch_one("SELECT id, mapped FROM courses WHERE canvas_course_id = %s", (cid,))
        prof_id = None
        teachers = c.get("teachers") or []
        if teachers and teachers[0].get("display_name"):
            try:
                prof_id = _upsert_professor(teachers[0]["display_name"])
            except Exception as e:
                log_error(SCRIPT, type(e).__name__, "professor upsert", str(e))
        execute_query(
            "INSERT INTO courses (canvas_course_id, canvas_course_name, professor_id, quarter, active) "
            "VALUES (%s,%s,%s,%s,TRUE) ON DUPLICATE KEY UPDATE canvas_course_name=VALUES(canvas_course_name), "
            "professor_id=COALESCE(VALUES(professor_id), professor_id), quarter=VALUES(quarter), active=TRUE",
            (cid, name, prof_id, quarter))
        seen_ids.append(cid)
        if existing is None and cid not in _notified_unmapped:
            _notified_unmapped.add(cid)
            notifier.send_warning(f"New Canvas course '{name}' is not mapped to a repo. Open the GUI course mapping page.")
    if seen_ids:
        ph = ",".join(["%s"] * len(seen_ids))
        execute_query(f"UPDATE courses SET active = FALSE WHERE active = TRUE AND canvas_course_id NOT IN ({ph})", seen_ids)
    rows = fetch_all("SELECT id, canvas_course_id, canvas_course_name FROM courses WHERE active = TRUE")
    return rows


def _group_names(cfg, canvas_course_id):
    key = (canvas_course_id, date.today())
    if key not in _group_cache:
        for k in [k for k in _group_cache if k[0] == canvas_course_id]:
            del _group_cache[k]
        groups = _paged(cfg, f"/api/v1/courses/{canvas_course_id}/assignment_groups", {"per_page": 100})
        _group_cache[key] = {g["id"]: (g.get("name") or "") for g in groups}
    return _group_cache[key]


def classify(cfg, group_name, title):
    keywords = get(cfg, "assignment_type_keywords", {}) or {}
    for text in ((group_name or "").lower(), (title or "").lower()):
        for atype, subs in keywords.items():
            if any(s in text for s in (subs or [])):
                return atype
    return "other"


def _number(title):
    m = re.search(r"\d+", title or "")
    return int(m.group()) if m else None


def _status(a):
    ws = ((a.get("submission") or {}).get("workflow_state") or "").lower()
    if ws == "graded":
        return "graded"
    if ws == "submitted":
        return "submitted"
    unlock = _to_local(a.get("unlock_at"))
    return "open" if unlock is None or unlock <= datetime.now() else "pending"


def _sync_assignments(cfg, course):
    cid = course["canvas_course_id"]
    groups = _group_names(cfg, cid)
    items = _paged(cfg, f"/api/v1/courses/{cid}/assignments", {"include[]": "submission", "per_page": 100})
    stored = {r["canvas_assignment_id"]: r for r in fetch_all(
        "SELECT id, canvas_assignment_id, title, available_from, due_at FROM assignments WHERE course_id = %s", (course["id"],))}
    for a in items:
        try:
            aid = int(a["id"])
            title = (a.get("name") or "")[:512]
            due, avail = _to_local(a.get("due_at")), _to_local(a.get("unlock_at"))
            atype = classify(cfg, groups.get(a.get("assignment_group_id")), title)
            status = _status(a)
            old = stored.get(aid)
            if old is None:
                execute_query(
                    "INSERT INTO assignments (course_id, canvas_assignment_id, title, assignment_type, assignment_number, "
                    "available_from, due_at, status, canvas_posted_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (course["id"], aid, title, atype, _number(title), avail, due, status, _to_local(a.get("created_at"))))
                continue
            for field, o, n in (("due_at", old["due_at"], due), ("available_from", old["available_from"], avail), ("title", old["title"], title)):
                if o != n:
                    execute_query(
                        "INSERT INTO assignment_changes (assignment_id, field_changed, old_value, new_value) VALUES (%s,%s,%s,%s)",
                        (old["id"], field, None if o is None else str(o)[:512], None if n is None else str(n)[:512]))
            execute_query(
                "UPDATE assignments SET title=%s, assignment_type=%s, assignment_number=%s, available_from=%s, due_at=%s, status=%s WHERE id=%s",
                (title, atype, _number(title), avail, due, status, old["id"]))
        except Exception as e:
            log_error(SCRIPT, type(e).__name__, f"assignment sync course {cid}", str(e))


def _sync_announcements(cfg, course):
    cid = course["canvas_course_id"]
    items = _paged(cfg, f"/api/v1/courses/{cid}/discussion_topics", {"only_announcements": "true", "per_page": 50})
    known = {r["canvas_announcement_id"] for r in fetch_all(
        "SELECT canvas_announcement_id FROM announcements WHERE course_id = %s", (course["id"],))}
    for t in items:
        try:
            if int(t["id"]) in known:
                continue
            execute_query(
                "INSERT INTO announcements (course_id, canvas_announcement_id, title, body, posted_at) VALUES (%s,%s,%s,%s,%s)",
                (course["id"], int(t["id"]), (t.get("title") or "")[:512], t.get("message"), _to_local(t.get("posted_at"))))
        except Exception as e:
            log_error(SCRIPT, type(e).__name__, f"announcement insert course {cid}", str(e))


def find_syllabus_sources(cfg, canvas_course_id):
    """On demand lookup used by the GUI. Returns {"syllabus_body": html|None, "files": [...]}."""
    result = {"syllabus_body": None, "files": []}
    r = _request(cfg, f"/api/v1/courses/{canvas_course_id}", {"include[]": "syllabus_body"})
    result["syllabus_body"] = r.json().get("syllabus_body") or None
    try:
        files = _paged(cfg, f"/api/v1/courses/{canvas_course_id}/files", {"search_term": "syllabus", "per_page": 50})
        result["files"] = [{"id": f.get("id"), "display_name": f.get("display_name"), "url": f.get("url"),
                            "content_type": f.get("content-type") or f.get("content_type")} for f in files]
    except AuthFailure:
        raise
    except Exception as e:
        log_error(SCRIPT, type(e).__name__, f"syllabus files course {canvas_course_id}", str(e))
    syllabus_sources[canvas_course_id] = result
    return result


def run_cycle(cfg):
    global _warned_rate_this_cycle
    _warned_rate_this_cycle = False
    try:
        courses = _sync_courses(cfg)
        for course in courses:
            cid = course["canvas_course_id"]
            for step in (_sync_assignments, _sync_announcements):
                try:
                    step(cfg, course)
                except (AuthFailure, _RateLimited):
                    raise
                except Exception as e:
                    log_error(SCRIPT, type(e).__name__, f"{step.__name__} course {cid}", str(e))
            if _syllabus_checked.get(cid) != date.today():
                try:
                    find_syllabus_sources(cfg, cid)
                    _syllabus_checked[cid] = date.today()
                except (AuthFailure, _RateLimited):
                    raise
                except Exception as e:
                    log_error(SCRIPT, type(e).__name__, f"syllabus check course {cid}", str(e))
    except _RateLimited as e:
        log_error(SCRIPT, "RateLimited", "run_cycle", str(e))
        time.sleep(min(e.seconds, 300))
        return None
    return None
