"""Canvas iCal feed poller. Pure HTTP, no browser. Writes assignments, assignment_changes, courses milestone pointers."""
import re
import time
import zlib
from datetime import date, datetime, time as dtime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests
from icalendar import Calendar

from core import notifier
from core.config import get
from db.db import execute_query, fetch_all, fetch_one
from logs.error_handler import log_error

SCRIPT = "ical"
TIMEOUT = 15
PACIFIC = ZoneInfo("America/Los_Angeles")
CODE_RE = re.compile(r"\[([^\[\]]+)\]\s*$")
STATUS_RANK = {"pending": 0, "open": 1, "submitted": 2, "graded": 3}
# (regex, assignment_type, flag) checked in order against the lowercased SUMMARY
TYPE_RULES = [
    (r"\bfinal(s)?\b", "test", "final"),
    (r"\bmid[\s-]?terms?\b", "test", "midterm"),
    (r"\bquiz(zes)?\b", "quiz", None),
    (r"\blabs?\b", "lab", None),
    (r"\bhomework\b|\bhw\b|\bproblem sets?\b", "hw", None),
    (r"\bprojects?\b|\bmilestones?\b", "project_milestone", None),
    (r"\breadings?\b", "reading", None),
    (r"\bdiscussions?\b", "other", None),
]


class AuthFailure(Exception):
    """Raised on HTTP 401 so the poller wrapper pauses this job like the other APIs."""


def classify(summary):
    s = (summary or "").lower()
    for pattern, atype, flag in TYPE_RULES:
        if re.search(pattern, s):
            return atype, flag
    return "other", None


def to_pacific(v):
    """DATE values mean 23:59:59 Pacific; datetimes are converted to naive Pacific local time."""
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=PACIFIC)
        return v.astimezone(PACIFIC).replace(tzinfo=None)
    if isinstance(v, date):
        return datetime.combine(v, dtime(23, 59, 59))
    return None


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _stem(code):
    parts = [p for p in re.split(r"[\s_-]+", code.strip()) if p]
    return _norm("".join(parts[:2])) if len(parts) >= 2 else _norm(code)


def match_course(code, courses):
    """Exact ical_course_code match, else claim an unassigned course whose name contains the dept+number stem."""
    for c in courses:
        if c.get("ical_course_code") == code:
            return c
    stem = _stem(code)
    if len(stem) >= 4:
        for c in courses:
            if not c.get("ical_course_code") and stem in _norm(c.get("canvas_course_name")):
                execute_query("UPDATE courses SET ical_course_code = %s WHERE id = %s", (code[:50], c["id"]))
                c["ical_course_code"] = code
                return c
    return None


def canvas_id_for(uid, url):
    """Canvas assignment id from the UID or URL. Calendar events that are not assignments get a negative id
    so the NOT NULL UNIQUE canvas_assignment_id column stays satisfied without colliding with real ids."""
    m = re.search(r"assignment[-_](\d+)", uid or "") or re.search(r"/assignments/(\d+)", url or "")
    if m:
        return int(m.group(1))
    m = re.search(r"calendar[-_]event[-_](\d+)", uid or "") or re.search(r"/calendar_events/(\d+)", url or "")
    if m:
        return -int(m.group(1))
    return -(zlib.crc32((uid or url or "").encode()) or 1)


def _number(title):
    m = re.search(r"\d+", title or "")
    return int(m.group()) if m else None


def _fetch(cfg):
    url = (get(cfg, "ical.feed_url") or "").strip()
    if not url:
        raise ValueError("ical.feed_url is not configured")
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    t0 = time.perf_counter_ns()
    status = 0
    try:
        r = requests.get(url, timeout=TIMEOUT)
        status = r.status_code
    finally:
        try:
            execute_query("INSERT INTO poll_metrics (api_name, endpoint, response_time_us, http_status) VALUES (%s,%s,%s,%s)",
                          ("ical", urlsplit(url).path[:512], (time.perf_counter_ns() - t0) // 1000, status))
        except Exception as e:
            log_error(SCRIPT, type(e).__name__, "poll_metrics insert", str(e))
    if r.status_code == 401:
        raise AuthFailure("ical feed returned 401")
    if r.status_code >= 400:
        raise RuntimeError(f"ical feed returned http {r.status_code}")
    return r.content


def _upsert_event(cfg, course, comp, now):
    uid = str(comp.get("UID") or "").strip()[:200] or None
    summary = str(comp.get("SUMMARY") or "").strip()
    title = CODE_RE.sub("", summary).strip()[:512] or summary[:512]
    url = str(comp.get("URL") or "").strip()[:500] or None
    dtstart = comp.get("DTSTART")
    dtend = comp.get("DTEND")
    due = to_pacific(dtstart.dt) if dtstart is not None else (to_pacific(dtend.dt) if dtend is not None else None)
    atype, flag = classify(summary)
    is_mid, is_fin = flag == "midterm", flag == "final"
    canvas_id = canvas_id_for(uid, url)
    row = fetch_one("SELECT * FROM assignments WHERE ical_uid = %s", (uid,)) if uid else None
    if row is None and canvas_id > 0:
        row = fetch_one("SELECT * FROM assignments WHERE canvas_assignment_id = %s", (canvas_id,))
    if row is None:
        row = fetch_one("SELECT * FROM assignments WHERE course_id = %s AND title = %s", (course["id"], title))
    new = row is None
    if new:
        status = "pending" if due and due > now else "open"
        aid = execute_query(
            "INSERT INTO assignments (course_id, canvas_assignment_id, title, assignment_type, assignment_number, due_at, status, "
            "canvas_posted_at, canvas_assignment_url, ical_uid, is_midterm, is_final) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (course["id"], canvas_id, title, atype, _number(title), due, status, now, url, uid, is_mid, is_fin))
    else:
        aid = row["id"]
        if due != row["due_at"] and due is not None:
            execute_query("INSERT INTO assignment_changes (assignment_id, field_changed, old_value, new_value) VALUES (%s,'due_at',%s,%s)",
                          (aid, None if row["due_at"] is None else str(row["due_at"]), str(due)))
        if uid and row["ical_uid"] and title != row["title"]:
            execute_query("INSERT INTO assignment_changes (assignment_id, field_changed, old_value, new_value) VALUES (%s,'title',%s,%s)",
                          (aid, row["title"], title))
        computed = "pending" if due and due > now else "open"
        status = computed if STATUS_RANK[computed] > STATUS_RANK.get(row["status"], 0) else row["status"]
        execute_query(
            "UPDATE assignments SET due_at = COALESCE(%s, due_at), title = %s, status = %s, canvas_assignment_url = COALESCE(canvas_assignment_url, %s), "
            "ical_uid = COALESCE(ical_uid, %s), is_midterm = is_midterm OR %s, is_final = is_final OR %s WHERE id = %s",
            (due, title if (uid and row["ical_uid"]) else row["title"], status, url, uid, is_mid, is_fin, aid))
    if is_mid:
        execute_query("UPDATE courses SET midterm_assignment_id = %s WHERE id = %s", (aid, course["id"]))
    if is_fin:
        execute_query("UPDATE courses SET final_assignment_id = %s WHERE id = %s", (aid, course["id"]))
    if new and due and course.get("telegram_enabled") and \
            now < due <= now + timedelta(hours=float(get(cfg, "notifications.new_assignment_telegram_hours", 48))):
        notifier.send_warning(f"New on Canvas: {title} [{course.get('ical_course_code') or course['canvas_course_name']}] due {due:%a %b %d %H:%M}")
    return aid, new


def send_due_reminders(now=None):
    now = now or datetime.now()
    rows = fetch_all(
        "SELECT a.*, c.canvas_course_name, c.ical_course_code FROM assignments a JOIN courses c ON c.id = a.course_id "
        "WHERE c.monitor = TRUE AND c.telegram_enabled = TRUE AND a.reminder_24h_sent = FALSE "
        "AND a.status NOT IN ('submitted','graded') AND a.due_at > %s AND a.due_at <= %s", (now, now + timedelta(hours=24)))
    sent = 0
    for a in rows:
        try:
            if notifier.send_assignment_reminder_24h(a, a):
                execute_query("UPDATE assignments SET reminder_24h_sent = TRUE WHERE id = %s", (a["id"],))
                sent += 1
        except Exception as e:
            log_error(SCRIPT, type(e).__name__, f"reminder assignment {a['id']}", str(e))
    return sent


def run_cycle(cfg):
    now = datetime.now()
    try:
        raw = _fetch(cfg)
        cal = Calendar.from_ical(raw)
    except AuthFailure:
        raise
    except Exception as e:
        log_error(SCRIPT, type(e).__name__, "fetch feed", str(e))
        return None
    courses = fetch_all("SELECT id, canvas_course_name, ical_course_code, monitor, telegram_enabled FROM courses WHERE active = TRUE")
    seen, created = 0, 0
    for comp in cal.walk("VEVENT"):
        try:
            summary = str(comp.get("SUMMARY") or "")
            m = CODE_RE.search(summary)
            if not m:
                log_error(SCRIPT, "NoCourseCode", "match course", f"no [CODE] suffix in SUMMARY: {summary[:120]}")
                continue
            course = match_course(m.group(1).strip(), courses)
            if course is None:
                log_error(SCRIPT, "UnmatchedCourse", "match course", f"no course for [{m.group(1)}]; set ical_course_code on the Monitoring page")
                continue
            if not course.get("monitor"):
                continue
            _, new = _upsert_event(cfg, course, comp, now)
            seen += 1
            created += int(new)
        except Exception as e:
            log_error(SCRIPT, type(e).__name__, f"event {str(comp.get('UID'))[:60]}", str(e))
    send_due_reminders(now)
    return None
