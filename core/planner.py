"""Builds the schedule: class blocks from class_meetings and study blocks for upcoming assignments.

Runs every morning before schedule_builder.run_for_today, at startup, after a syllabus parse and from the GUI.
It only replaces blocks it made itself (source 'class' or 'study') from now on; blocks added by hand, and blocks
already in the past, are never touched. Past study blocks that were not skipped count as work done.

Study time per assignment is its estimated hours (core.workload: per class difficulty and hour scale), minus the
study blocks already done, spread evenly over the days left before it is due. So each class gets sessions sized to
its own workload rather than a fixed length. Sessions end by study.latest_end (22:00) unless an assignment due within
the planning window cannot otherwise be finished, then up to study.hard_latest_end."""
from datetime import date, datetime, time, timedelta

from core import syllabus_parser, workload
from core.config import get, load_config
from db.db import execute_query, fetch_all
from logs.error_handler import log_error

SCRIPT = "planner"
DAY_LETTERS = "MTWRFSU"   # date.weekday() order: Monday is M, Thursday R, Sunday U
STEP = 15                 # minutes; blocks start and end on quarter hours


def _m(hhmm):
    h, m = (int(x) for x in str(hhmm).split(":")[:2])
    return h * 60 + m


def _mins(v):
    """Minutes since midnight from a TIME column (timedelta), a time, or "HH:MM"."""
    if isinstance(v, timedelta):
        return int(v.total_seconds() // 60)
    if isinstance(v, time):
        return v.hour * 60 + v.minute
    return _m(v)


def _hhmm(mins):
    mins = max(0, min(24 * 60 - 1, int(mins)))
    return f"{mins // 60:02d}:{mins % 60:02d}"


def _up(mins):
    return -(-int(mins) // STEP) * STEP


def settings(cfg):
    s = get(cfg, "study", {}) or {}
    return {"horizon_days": int(s.get("horizon_days", 7)),
            "day_start": _m(s.get("day_start", "08:00")),
            "latest_end": _m(s.get("latest_end", "22:00")),
            "hard_latest_end": min(_m(s.get("hard_latest_end", "23:45")), 24 * 60 - 1),
            "min_block": int(s.get("min_block_minutes", 30)),
            "max_block": int(s.get("max_block_minutes", 180)),
            "break": int(s.get("break_minutes", 15)),
            "max_per_day": int(float(s.get("max_hours_per_day", 8)) * 60)}


# ------------------------------------------------------------------ pure planning
def class_blocks(meetings, days):
    """{date: [block]} for each meeting on each date whose weekday letter is in its days and within its date range."""
    out = {d: [] for d in days}
    for m in meetings:
        for d in days:
            if DAY_LETTERS[d.weekday()] in m["days"] and m.get("first_day", d) <= d <= m.get("last_day", d):
                out[d].append({"date": d, "time_category": "class", "label": m["label"], "start": _mins(m["start_time"]),
                               "end": _mins(m["end_time"]), "priority": 1, "moveable": False, "source": "class",
                               "course_id": m["course_id"], "assignment_id": None})
    return out


def _free(busy, lo, hi, gap):
    """Free (start, end) intervals in [lo, hi) around busy intervals, keeping `gap` minutes clear of each."""
    spans, t = [], lo
    for s, e in sorted(busy):
        s, e = s - gap, e + gap
        if e <= t:
            continue
        if s > t:
            spans.append((t, min(s, hi)))
        t = max(t, e)
        if t >= hi:
            break
    if t < hi:
        spans.append((t, hi))
    return [(_up(s), e) for s, e in spans if _up(s) < e]


def _place(want, busy, lo, hi, st, cap_left):
    """Places up to `want` minutes as blocks in free time between lo and hi. Returns [(start, end)]."""
    placed, want = [], min(want, cap_left)
    while want >= STEP:
        spans = _free(busy + placed, lo, hi, st["break"])
        if not spans:
            break
        s, e = spans[0]
        length = min(want, st["max_block"], e - s)
        length -= length % STEP
        if length < STEP:
            busy = busy + [(s, e)]   # too small to use; look past it
            continue
        placed.append((s, s + length))
        want -= length
    return placed


def study_blocks(assignments, busy_by_day, today, now_min, st):
    """{date: [block]} of study sessions. assignments: [{id, course_id, label, due_at, hours, done}] (hours and done in hours).
    busy_by_day: {date: [(start, end)]} already taken, including the class blocks."""
    days = [today + timedelta(days=i) for i in range(st["horizon_days"])]
    busy = {d: list(busy_by_day.get(d, [])) for d in days}
    used = {d: 0 for d in days}   # study minutes placed per day, capped at max_hours_per_day
    out = {d: [] for d in days}
    for a in sorted(assignments, key=lambda x: x["due_at"]):
        remaining = max(0.0, a["hours"] - a.get("done", 0.0)) * 60
        due = a["due_at"]
        last = due.date() if due.time() >= time(12, 0) else due.date() - timedelta(days=1)   # a morning deadline means the night before
        last = max(last, today)
        necessary = last < today + timedelta(days=st["horizon_days"])   # due inside the window: it has to fit
        for d in days:
            if remaining < 1 or d > last:
                break
            days_left = (last - d).days + 1
            share = remaining / days_left
            want = _up(max(share, min(remaining, st["min_block"])))
            lo = max(st["day_start"], _up(now_min) if d == today else 0)
            hi = st["latest_end"]
            if d == last and due.date() == d:
                hi = min(hi, due.hour * 60 + due.minute)   # finish before the deadline on the due day itself
            got = _place(want, busy[d], lo, hi, st, st["max_per_day"] - used[d])
            short = want - sum(e - s for s, e in got)
            if short >= STEP and necessary and hi == st["latest_end"]:   # it cannot wait: allow later than latest_end
                got += _place(short, busy[d] + got, st["latest_end"], st["hard_latest_end"], st, st["max_per_day"] - used[d] - sum(e - s for s, e in got))
            for s, e in got:
                busy[d].append((s, e))
                used[d] += e - s
                remaining -= e - s
                out[d].append({"date": d, "time_category": "flexible", "label": a["label"], "start": s, "end": e,
                               "priority": 2 if (due.date() - d).days <= 2 else 3, "moveable": True, "source": "study",
                               "course_id": a["course_id"], "assignment_id": a["id"]})
    return out


# ------------------------------------------------------------------ database
def _code(c):
    return c.get("ical_course_code") or c.get("canvas_course_name") or "?"


def _meetings(cfg, today):
    courses = {c["id"]: c for c in fetch_all("SELECT id, ical_course_code, canvas_course_name FROM courses WHERE active = TRUE AND monitor = TRUE")}
    window = syllabus_parser.quarter_window(cfg, today)
    last_due = {r["course_id"]: r["d"].date() for r in fetch_all(
        "SELECT course_id, MAX(due_at) AS d FROM assignments WHERE due_at IS NOT NULL GROUP BY course_id")}
    rows = fetch_all("SELECT * FROM class_meetings ORDER BY course_id, id")
    edited = {r["course_id"] for r in rows if r["source"] == "edited"}
    out = []
    for r in rows:
        c = courses.get(r["course_id"])
        if not c or (r["course_id"] in edited and r["source"] != "edited"):   # edited times win over the syllabus ones
            continue
        out.append(dict(r, label=f"{_code(c)} {r['kind']}" + (f" ({r['location']})" if r.get("location") else ""),
                        first_day=window[0], last_day=min(window[1], last_due.get(r["course_id"], window[1]))))
    return out


def plan(cfg=None, today=None, now=None):
    """Rebuilds the generated blocks from now through study.horizon_days. Returns {class, study} block counts."""
    cfg = cfg or load_config()
    now = now or datetime.now()
    today = today or now.date()
    st = settings(cfg)
    now_min = now.hour * 60 + now.minute if today == now.date() else 0
    days = [today + timedelta(days=i) for i in range(st["horizon_days"])]
    end = days[-1]
    execute_query("DELETE FROM schedule_blocks WHERE source IN ('class','study') AND ((date = %s AND start_time >= %s) OR (date > %s AND date <= %s))",
                  (today, _hhmm(now_min), today, end))
    classes = class_blocks(_meetings(cfg, today), days)
    cancelled = {(r["course_id"], r["date"]) for r in fetch_all("SELECT course_id, date FROM class_cancellations WHERE date >= %s AND date <= %s", (today, end))}
    classes = {d: [b for b in v if (b["course_id"], d) not in cancelled] for d, v in classes.items()}   # "no class Thursday"
    kept = fetch_all("SELECT date, start_time, end_time FROM schedule_blocks WHERE date >= %s AND date <= %s AND skipped = FALSE", (today, end))
    busy = {d: [(b["start"], b["end"]) for b in classes[d]] for d in days}
    for b in kept:
        busy.setdefault(b["date"], []).append((_mins(b["start_time"]), _mins(b["end_time"])))
    presets = workload.Presets(cfg)
    done = {r["assignment_id"]: float(r["h"] or 0) / 3600 for r in fetch_all(
        "SELECT assignment_id, SUM(TIME_TO_SEC(TIMEDIFF(end_time, start_time))) AS h FROM schedule_blocks "
        "WHERE source = 'study' AND skipped = FALSE AND (date < %s OR (date = %s AND end_time <= %s)) GROUP BY assignment_id",
        (today, today, _hhmm(now_min)))}
    rows = fetch_all("SELECT a.id, a.course_id, a.title, a.assignment_type, a.due_at, c.ical_course_code, c.canvas_course_name "
                     "FROM assignments a JOIN courses c ON c.id = a.course_id WHERE a.status IN ('pending','open') AND a.due_at > %s "
                     "AND a.is_event = FALSE AND c.active = TRUE AND c.monitor = TRUE", (now,))
    assignments = [{"id": r["id"], "course_id": r["course_id"], "due_at": r["due_at"], "done": done.get(r["id"], 0.0),
                    "hours": presets.hours(r["course_id"], r["assignment_type"]), "label": f"Study: {_code(r)} {r['title']}"[:255]} for r in rows]
    study = study_blocks(assignments, busy, today, now_min, st)
    counts = {"class": 0, "study": 0}
    for d in days:
        for b in classes[d] + study[d]:
            if b["source"] == "class" and d == today and b["start"] < now_min:
                continue   # already started or over; the kept copy (if any) stays
            execute_query("INSERT INTO schedule_blocks (date, time_category, label, start_time, end_time, priority, moveable, source, course_id, assignment_id) "
                          "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                          (d, b["time_category"], b["label"][:255], _hhmm(b["start"]), _hhmm(b["end"]), b["priority"], b["moveable"],
                           b["source"], b["course_id"], b["assignment_id"]))
            counts[b["source"]] += 1
    return counts


def plan_safely(cfg=None):
    try:
        return plan(cfg)
    except Exception as e:
        log_error(SCRIPT, type(e).__name__, "plan", str(e))
        return None
