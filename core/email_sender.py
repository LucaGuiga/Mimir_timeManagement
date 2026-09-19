"""Morning and evening summary emails over SMTP."""
import html
import smtplib
from datetime import date, datetime, timedelta
from email.message import EmailMessage

from core import progress, stress
from core.config import get, load_config
from db.db import execute_query, fetch_all
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


def _stress_line(d, cfg):
    s = stress.latest_for(d) or stress.calculate_and_store(d, cfg)
    return [f"S = {s['ratio']:.2f} ({s['band']}), {s['t_available']:.1f}h available, slept {s['hours_slept']:.1f}h"]


def send_morning(cfg=None):
    cfg = cfg or load_config()
    today, now = date.today(), datetime.now()
    parts = [_section("Stress", _stress_line(today, cfg))]
    blocks = fetch_all("SELECT * FROM schedule_blocks WHERE date = %s ORDER BY start_time", (today,))
    parts.append(_section("Schedule", [f"{_fmt(b['start_time'])}-{_fmt(b['end_time'])} {b['label']} [{b['time_category']}]"
                                       + (f" SKIPPED ({b['skip_reason']})" if b["skipped"] else "") for b in blocks]))
    due = fetch_all("SELECT a.title, a.assignment_type, a.due_at, a.status, c.canvas_course_name FROM assignments a "
                    "JOIN courses c ON c.id = a.course_id WHERE a.status IN ('pending','open') AND a.due_at BETWEEN %s AND %s "
                    "ORDER BY a.due_at", (now, now + timedelta(days=7)))
    parts.append(_section("Due within 7 days", [f"{_fmt(a['due_at'])}  {a['canvas_course_name']}: {a['title']} ({a['assignment_type']})" for a in due]))
    ann = fetch_all("SELECT an.title, an.posted_at, c.canvas_course_name FROM announcements an JOIN courses c ON c.id = an.course_id "
                    "WHERE an.detected_at >= %s ORDER BY an.posted_at DESC", (now - timedelta(hours=24),))
    parts.append(_section("Announcements (24h)", [f"{a['canvas_course_name']}: {a['title']}" for a in ann]))
    oura = fetch_all("SELECT * FROM oura_daily WHERE missing = FALSE AND (created_at >= %s OR filled_at >= %s) ORDER BY date DESC",
                     (now - timedelta(hours=24), now - timedelta(hours=24)))
    parts.append(_section("Oura", [f"{o['date']}: sleep {o['sleep_score']}, readiness {o['readiness_score']}, "
                                   f"{(o['total_sleep_seconds'] or 0) / 3600:.1f}h, HRV {o['hrv_avg']}, RHR {o['resting_hr']}" for o in oura],
                          "no new Oura data since yesterday morning"))
    hz = fetch_all("SELECT p.professor_name, h.day_of_week, h.hour_start, h.hour_end, h.source FROM hot_zones h "
                   "JOIN professor_profiles p ON p.id = h.professor_id WHERE h.day_of_week = %s ORDER BY p.professor_name, h.hour_start",
                   (today.weekday(),))
    parts.append(_section("Hot zones today (informational)", [f"{h['professor_name']}: {h['hour_start']:02d}:00-{h['hour_end']:02d}:00 ({h['source']})" for h in hz]))
    return _send(cfg, f"Athena morning {today.isoformat()}", parts)


def send_evening(cfg=None):
    cfg = cfg or load_config()
    today = date.today()
    parts = []
    commits = progress.commits_for_day(today)
    rows = []
    for course, assignments in commits["by_course"].items():
        for title, files in assignments.items():
            delta = sum(f["size_delta"] or 0 for f in files)
            rows.append(f"{course} / {title}: {len({f['commit_sha'] for f in files})} commits, {len(files)} files, {delta:+d} bytes")
    parts.append(_section("Commits today", rows))
    parts.append(_section("No commit today", [f"{r['course_name']}: {r['assignment_title'] or '(unmatched)'}" for r in commits["no_commit"]]))
    parts.append(_section("Stress", _stress_line(today, cfg)))
    changes = fetch_all("SELECT ac.id, ac.field_changed, ac.old_value, ac.new_value, a.title, c.canvas_course_name FROM assignment_changes ac "
                        "JOIN assignments a ON a.id = ac.assignment_id JOIN courses c ON c.id = a.course_id WHERE ac.reported = FALSE "
                        "ORDER BY ac.detected_at")
    parts.append(_section("Assignment changes", [f"{c['canvas_course_name']}: {c['title']} {c['field_changed']} "
                                                 f"{c['old_value']} -> {c['new_value']}" for c in changes]))
    errors = [e for e in errors_for_date(today) if not e["acknowledged"]]
    if errors:
        parts.append(_section("Errors today (unacknowledged)", [f"[{e['severity']}] {e['script_name']} {e['operation']}: {(e['raw_message'] or '')[:160]}" for e in errors]))
    ok = _send(cfg, f"Athena evening {today.isoformat()}", parts)
    if ok and changes:
        ph = ",".join(["%s"] * len(changes))
        execute_query(f"UPDATE assignment_changes SET reported = TRUE WHERE id IN ({ph})", [c["id"] for c in changes])
    return ok
