"""Morning and evening summary emails over SMTP."""
import html
import smtplib
from datetime import date, datetime, timedelta
from email.message import EmailMessage

from core import deepseek_usage, progress, stress
from core.config import get, load_config
from db.db import execute_query, fetch_all, fetch_one
from logs.error_handler import errors_for_date, log_error

SCRIPT = "email_sender"
TIMEOUT = 20
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _fmt(v):
    if isinstance(v, datetime):
        return v.strftime("%a %b %d %H:%M")
    if isinstance(v, timedelta):
        return f"{v.seconds // 3600:02d}:{(v.seconds % 3600) // 60:02d}"
    return "" if v is None else str(v)


def _section(title, rows, empty="none"):
    """rows: list of strings. Returns (html, text)."""
    if not rows:
        return f"<h3>{html.escape(title)}</h3><p><i>{html.escape(empty)}</i></p>", f"\n{title}\n  {empty}\n"
    h = f"<h3>{html.escape(title)}</h3><ul>" + "".join(f"<li>{html.escape(r)}</li>" for r in rows) + "</ul>"
    return h, f"\n{title}\n" + "".join(f"  - {r}\n" for r in rows)


def _send(cfg, subject, parts):
    e = get(cfg, "email", {}) or {}
    if not e.get("smtp_host") or not e.get("recipient"):
        log_error(SCRIPT, "ConfigMissing", "send", "email.smtp_host or email.recipient not configured")
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, e.get("smtp_user") or e["recipient"], e["recipient"]
    msg.set_content("".join(t for _, t in parts))
    msg.add_alternative("<html><body>" + "".join(h for h, _ in parts) + "</body></html>", subtype="html")
    port = int(e.get("smtp_port") or 587)
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(e["smtp_host"], port, timeout=TIMEOUT)
        else:
            server = smtplib.SMTP(e["smtp_host"], port, timeout=TIMEOUT)
            server.starttls()
        with server:
            if e.get("smtp_user"):
                server.login(e["smtp_user"], e.get("smtp_password") or "")
            server.send_message(msg)
        return True
    except Exception as ex:
        log_error(SCRIPT, type(ex).__name__, f"send '{subject}'", str(ex))
        return False


def _stress_rows(d, cfg):
    s = stress.latest_for(d) or stress.calculate_and_store(d, cfg)
    return [f"Ratio {s['ratio']:.2f} ({s['band']})", f"T_available {s['t_available']:.1f} h of {s['t_awake']:.1f} awake",
            f"Deadline term {s['deadline_term']:.2f}", f"Sleep penalty {s['sleep_penalty']:.2f} ({s['hours_slept']:.1f} h slept)"]


def _code(row):
    return row.get("ical_course_code") or row.get("canvas_course_name")


def milestones_for(course_id, now=None):
    """Run 6: {midterm: row|None, final: row|None} from the course pointers, falling back to is_midterm / is_final rows."""
    now = now or datetime.now()
    c = fetch_one("SELECT midterm_assignment_id, final_assignment_id FROM courses WHERE id = %s", (course_id,))
    out = {}
    for key, col, flag in (("midterm", "midterm_assignment_id", "is_midterm"), ("final", "final_assignment_id", "is_final")):
        row = fetch_one("SELECT id, title, due_at FROM assignments WHERE id = %s", (c[col],)) if c and c[col] else None
        if row is None:
            row = fetch_one(f"SELECT id, title, due_at FROM assignments WHERE course_id = %s AND {flag} = TRUE AND due_at IS NOT NULL "
                            "ORDER BY (due_at < %s), due_at LIMIT 1", (course_id, now))
        out[key] = row
    return out


def _days_until(dt, now):
    return (dt - now).total_seconds() / 86400 if dt else None


def send_morning(cfg=None):
    cfg = cfg or load_config()
    today, now = date.today(), datetime.now()
    parts = [_section("Stress", _stress_rows(today, cfg))]
    blocks = fetch_all("SELECT * FROM schedule_blocks WHERE date = %s ORDER BY start_time", (today,))
    parts.append(_section("Schedule", [f"{_fmt(b['start_time'])}-{_fmt(b['end_time'])} {b['label']} [{b['time_category']}]"
                                       + (f" SKIPPED ({b['skip_reason']})" if b["skipped"] else "") for b in blocks]))
    due = fetch_all("SELECT a.title, a.assignment_type, a.due_at, a.status, c.canvas_course_name FROM assignments a "
                    "JOIN courses c ON c.id = a.course_id WHERE a.status IN ('pending','open') AND a.due_at BETWEEN %s AND %s "
                    "ORDER BY a.due_at", (now, now + timedelta(days=7)))
    parts.append(_section("Due within 7 days", [f"{_fmt(a['due_at'])}  {a['canvas_course_name']}: {a['title']} ({a['assignment_type']})" for a in due]))
    # Upcoming milestones: always shown, one line per active course
    rows = []
    for c in fetch_all("SELECT id, canvas_course_name, ical_course_code FROM courses WHERE active = TRUE ORDER BY canvas_course_name"):
        m = milestones_for(c["id"], now)
        mid, fin = m["midterm"], m["final"]
        if mid and mid["due_at"] and mid["due_at"] > now:
            rows.append(f"{_code(c)}: Midterm in {_days_until(mid['due_at'], now):.0f} days ({_fmt(mid['due_at'])})")
        elif fin and fin["due_at"] and fin["due_at"] > now:
            rows.append(f"{_code(c)}: Final in {_days_until(fin['due_at'], now):.0f} days ({_fmt(fin['due_at'])})")
        elif fin or mid:
            rows.append(f"{_code(c)}: midterm and final have passed")
        else:
            rows.append(f"{_code(c)}: no midterm or final detected yet")
    parts.append(_section("Upcoming milestones", rows, "no active courses"))
    radar = fetch_all("SELECT a.title, a.assignment_type, a.due_at, a.status, c.canvas_course_name, c.ical_course_code FROM assignments a "
                      "JOIN courses c ON c.id = a.course_id WHERE c.active = TRUE AND a.status IN ('pending','open') "
                      "AND a.assignment_type IN ('test','quiz','project_milestone') ORDER BY a.due_at IS NULL, a.due_at")
    parts.append(_section("Tests and projects on the radar", [f"{_fmt(r['due_at']) or 'no date'}  {_code(r)}: {r['title']} ({r['assignment_type']}, {r['status']})" for r in radar], "None active"))
    # ANNOUNCEMENTS: held pending LLM summarisation (run 7)
    night = fetch_one("SELECT * FROM oura_daily WHERE missing = FALSE AND date <= %s ORDER BY date DESC LIMIT 1", (today,))
    if night and night["date"] >= today - timedelta(days=1):
        avg = fetch_one("SELECT AVG(sleep_score) s, AVG(readiness_score) r FROM oura_daily WHERE missing = FALSE AND date < %s AND date >= %s",
                        (night["date"], night["date"] - timedelta(days=7)))
        lines = []
        for label, key, mean in (("Sleep score", "sleep_score", avg["s"] if avg else None), ("Readiness", "readiness_score", avg["r"] if avg else None)):
            v = night[key]
            if v is None:
                lines.append(f"{label}: no data")
            elif mean is None:
                lines.append(f"{label}: {v} (no 7 day average yet)")
            else:
                lines.append(f"{label}: {v} vs 7 day average {mean:.0f} ({v - mean:+.0f})")
        parts.append(_section("Last night vs your average", lines))
    else:
        parts.append(_section("Last night vs your average", [], "no Oura data for last night; open the Oura app to sync"))
    hz = fetch_all("SELECT p.professor_name, h.day_of_week, h.hour_start, h.hour_end, h.source FROM hot_zones h "
                   "JOIN professor_profiles p ON p.id = h.professor_id WHERE h.day_of_week = %s ORDER BY p.professor_name, h.hour_start",
                   (today.weekday(),))
    parts.append(_section("Hot zones today (informational)", [f"{h['professor_name']}: {h['hour_start']:02d}:00-{h['hour_end']:02d}:00 ({h['source']})" for h in hz]))
    parts.append(_section("DeepSeek usage (billing period)", deepseek_usage.email_rows(cfg)))
    return _send(cfg, f"Mimir morning {today.isoformat()}", parts)


def send_evening(cfg=None):
    cfg = cfg or load_config()
    today, now = date.today(), datetime.now()
    parts = []
    commits = progress.commits_for_day(today)
    rows = []
    for course, assignments in commits["by_course"].items():
        for title, files in assignments.items():
            delta = sum(f["size_delta"] or 0 for f in files)
            rows.append(f"{course} / {title}: {len({f['commit_sha'] for f in files})} commits, {len(files)} files, {delta:+d} bytes")
    parts.append(_section("Commits today", rows))
    parts.append(_section("No commit today", [f"{r['course_name']}: {r['assignment_title'] or '(unmatched)'}" for r in commits["no_commit"]]))
    intraday = fetch_all("SELECT * FROM oura_intraday WHERE date = %s ORDER BY poll_time", (today,))
    parts.append(_section("Oura readiness today", [
        f"{_fmt(r['poll_time'])}: readiness {r['readiness_score'] if r['readiness_score'] is not None else '–'}, HRV {r['hrv_avg'] if r['hrv_avg'] is not None else '–'}"
        + (f"  STRESS HIGH (more than {r['stress_threshold_used']:.0f}% below baseline)" if r["stress_high"] else "") for r in intraday],
        "no intraday readiness rows today"))
    # Per assignment status log, grouped by course, monitored and email enabled courses only
    log_rows = fetch_all(
        "SELECT a.id, a.title, a.assignment_type, a.status, a.due_at, a.grade_percent, c.canvas_course_name, c.ical_course_code "
        "FROM assignments a JOIN courses c ON c.id = a.course_id WHERE c.active = TRUE AND c.monitor = TRUE AND c.email_enabled = TRUE "
        "AND (a.status IN ('pending','open','submitted') OR a.grade_detected_at >= %s) ORDER BY c.canvas_course_name, a.due_at IS NULL, a.due_at",
        (now - timedelta(days=7),))
    today_commits = {r["assignment_id"]: r for r in fetch_all(
        "SELECT assignment_id, COUNT(DISTINCT commit_sha) n, COALESCE(SUM(size_delta), 0) d FROM github_commits "
        "WHERE no_commit = FALSE AND assignment_id IS NOT NULL AND DATE(commit_timestamp) = %s GROUP BY assignment_id", (today,))}
    lines, last_course = [], None
    for a in log_rows:
        if a["canvas_course_name"] != last_course:
            last_course = a["canvas_course_name"]
            lines.append(f"== {_code(a)} ==")
        tc = today_commits.get(a["id"], {"n": 0, "d": 0})
        days = _days_until(a["due_at"], now)
        grade = f", grade {a['grade_percent']:.1f}%" if a["status"] == "graded" and a["grade_percent"] is not None else ""
        lines.append(f"{a['title']} ({a['assignment_type']}): {a['status']}{grade}, {tc['n']} commits today ({int(tc['d']):+d} B), "
                     + (f"{days:.1f} days left" if days is not None else "no due date"))
    parts.append(_section("Assignment status", lines, "no active assignments on monitored courses"))
    grades = fetch_all("SELECT a.title, a.grade_percent, c.canvas_course_name, c.ical_course_code FROM assignments a JOIN courses c ON c.id = a.course_id "
                       "WHERE DATE(a.grade_detected_at) = %s ORDER BY c.canvas_course_name", (today,))
    parts.append(_section("Grades posted today", [f"{_code(g)}: {g['title']}: {g['grade_percent']:.1f}%" if g["grade_percent"] is not None else f"{_code(g)}: {g['title']}: graded" for g in grades]))
    changes = fetch_all("SELECT ac.id, ac.field_changed, ac.old_value, ac.new_value, a.title, c.canvas_course_name FROM assignment_changes ac "
                        "JOIN assignments a ON a.id = ac.assignment_id JOIN courses c ON c.id = a.course_id WHERE ac.reported = FALSE "
                        "ORDER BY ac.detected_at")
    parts.append(_section("Assignment changes", [f"{c['canvas_course_name']}: {c['title']} {c['field_changed']} "
                                                 f"{c['old_value']} -> {c['new_value']}" for c in changes]))
    parts.append(_section("DeepSeek usage (billing period)", deepseek_usage.email_rows(cfg)))
    errors = [e for e in errors_for_date(today) if not e["acknowledged"]]
    if errors:
        parts.append(_section("Errors today (unacknowledged)", [f"[{e['severity']}] {e['script_name']} {e['operation']}: {(e['raw_message'] or '')[:160]}" for e in errors]))
    ok = _send(cfg, f"Mimir evening {today.isoformat()}", parts)
    if ok and changes:
        ph = ",".join(["%s"] * len(changes))
        execute_query(f"UPDATE assignment_changes SET reported = TRUE WHERE id IN ({ph})", [c["id"] for c in changes])
    return ok
