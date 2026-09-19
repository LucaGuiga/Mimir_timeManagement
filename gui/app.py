"""Athena Flask GUI. Launched by core/main.py as `python -m gui.app`."""
import functools
import json
import os
import signal
import sys
from datetime import date, datetime, timedelta

from flask import Flask, flash, redirect, render_template, request, session, url_for

from core import schedule_builder, stress, syllabus_parser
from core.config import ConfigError, get, load_config, reload_config, repo_root, save_config, validate_config
from db.db import execute_query, fetch_all, fetch_one
from logs.error_handler import acknowledge_error, get_unacknowledged, log_error, unacknowledged_critical_count
from pollers.canvas import find_syllabus_sources

SCRIPT = "gui"
TYPES = ("hw", "quiz", "test", "lab", "project_milestone", "reading", "other")
CATEGORIES = ("class", "travel", "clubs", "chores", "fixed", "flexible")
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _hhmm(v):
    if isinstance(v, timedelta):
        return f"{v.seconds // 3600:02d}:{(v.seconds % 3600) // 60:02d}"
    return "" if v is None else str(v)[:5]


def _dt(v):
    if isinstance(v, datetime):
        return v.strftime("%a %b %d %H:%M")
    return "" if v is None else str(v)


def _jsonload(v, default):
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v) if v else default
    except ValueError:
        return default


def _parse_date(s):
    try:
        return date.fromisoformat(s) if s else date.today()
    except ValueError:
        return date.today()


def create_app(cfg=None):
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.secret_key = os.urandom(32)
    app.config["cfg"] = cfg or load_config()
    app.jinja_env.filters["hhmm"] = _hhmm
    app.jinja_env.filters["dt"] = _dt

    def cfg_now():
        return app.config["cfg"]

    def guarded(fn):
        @functools.wraps(fn)
        def wrapped(*a, **kw):
            try:
                return fn(*a, **kw)
            except Exception as e:
                log_error(SCRIPT, type(e).__name__, f"{request.method} {request.path}", str(e))
                flash(f"{type(e).__name__}: {e}", "error")
                if request.endpoint == "index" and request.method == "GET":
                    return render_template("base.html"), 500
                return redirect(request.referrer or url_for("index"))
        return wrapped

    @app.context_processor
    def banner():
        ctx = {"critical_count": 0, "latest_critical": None, "poller_status": [], "today": date.today(), "types": TYPES, "categories": CATEGORIES, "days": DAYS}
        try:
            ctx["critical_count"] = unacknowledged_critical_count()
            if ctx["critical_count"]:
                ctx["latest_critical"] = fetch_one("SELECT * FROM error_log WHERE severity='critical' AND acknowledged=FALSE ORDER BY timestamp DESC LIMIT 1")
            rows = {r["api_name"]: r for r in fetch_all("SELECT * FROM poller_status")}
            ctx["poller_status"] = [rows.get(a, {"api_name": a, "state": "unknown", "last_cycle_at": None}) for a in ("canvas", "github", "oura")]
        except Exception as e:
            ctx["banner_error"] = str(e)
        return ctx

    # ---------------------------------------------------------------- index
    @app.get("/")
    @guarded
    def index():
        today, now = date.today(), datetime.now()
        s = stress.latest_for(today)
        blocks = fetch_all("SELECT * FROM schedule_blocks WHERE date = %s ORDER BY start_time, id", (today,))
        due = fetch_all("SELECT a.*, c.canvas_course_name FROM assignments a JOIN courses c ON c.id = a.course_id "
                        "WHERE a.status IN ('pending','open') AND a.due_at BETWEEN %s AND %s ORDER BY a.due_at", (now, now + timedelta(days=7)))
        for a in due:
            a["days_remaining"] = round((a["due_at"] - now).total_seconds() / 86400, 1)
        avg = {r["api_name"]: r for r in fetch_all("SELECT api_name, AVG(response_time_us) AS avg_us, MAX(timestamp) AS last FROM poll_metrics "
                                                    "WHERE timestamp >= %s GROUP BY api_name", (now - timedelta(hours=1),))}
        metrics = [{"api": a, "avg_us": None if a not in avg else int(avg[a]["avg_us"]), "last": avg.get(a, {}).get("last")} for a in ("canvas", "github", "oura")]
        return render_template("index.html", s=s, blocks=blocks, due=due, metrics=metrics)

    # -------------------------------------------------------------- courses
    @app.get("/courses")
    @guarded
    def courses():
        rows = fetch_all("SELECT c.*, p.professor_name FROM courses c LEFT JOIN professor_profiles p ON p.id = c.professor_id "
                         "ORDER BY c.active DESC, c.canvas_course_name")
        for r in rows:
            r["branches_text"] = ", ".join(_jsonload(r["branches"], ["main"]))
        repos = sorted({r["repo_name"] for r in rows if r["repo_name"]})
        return render_template("course_mapping.html", courses=rows, repos=repos)

    @app.post("/courses/map")
    @guarded
    def courses_map():
        f = request.form
        repo = (f.get("repo_name") or "").strip()
        branches = [b.strip() for b in (f.get("branches") or "").split(",") if b.strip()] or ["main"]
        execute_query("UPDATE courses SET repo_name=%s, repo_path_prefix=%s, branches=%s, mapped=%s WHERE id=%s",
                      (repo or None, (f.get("repo_path_prefix") or "").strip().strip("/") or None, json.dumps(branches), bool(repo), int(f["course_id"])))
        flash("Course mapping saved." if repo else "Course mapping cleared.", "ok")
        return redirect(url_for("courses"))

    # ----------------------------------------------------------- professors
    @app.get("/professors")
    @guarded
    def professors():
        profs = fetch_all("SELECT * FROM professor_profiles ORDER BY professor_name")
        courses_by_prof = {}
        for c in fetch_all("SELECT id, canvas_course_name, professor_id, active FROM courses WHERE professor_id IS NOT NULL ORDER BY canvas_course_name"):
            courses_by_prof.setdefault(c["professor_id"], []).append(c)
        for p in profs:
            p["courses"] = courses_by_prof.get(p["id"], [])
            p["day_names"] = [DAYS[d] for d in _jsonload(p["typical_post_days"], []) if isinstance(d, int) and 0 <= d <= 6]
        return render_template("professor_profiles.html", profs=profs)

    # --------------------------------------------------------------- errors
    @app.get("/errors")
    @guarded
    def errors():
        q = request.args
        page = max(1, int(q.get("page", 1) or 1))
        rows, total = get_unacknowledged(severity=q.get("severity") or None, script_name=q.get("script_name") or None,
                                         date_from=q.get("date_from") or None,
                                         date_to=(date.fromisoformat(q["date_to"]) + timedelta(days=1)).isoformat() if q.get("date_to") else None,
                                         page=page, per_page=50)
        scripts = [r["script_name"] for r in fetch_all("SELECT DISTINCT script_name FROM error_log ORDER BY script_name")]
        return render_template("error_log.html", rows=rows, total=total, page=page, pages=max(1, (total + 49) // 50), scripts=scripts, q=q)

    @app.post("/errors/<int:error_id>/acknowledge")
    @guarded
    def errors_ack(error_id):
        acknowledge_error(error_id)
        return redirect(request.referrer or url_for("errors"))

    @app.post("/errors/acknowledge_all_critical")
    @guarded
    def errors_ack_all():
        n = execute_query("UPDATE error_log SET acknowledged = TRUE WHERE acknowledged = FALSE AND severity = 'critical'")
        flash(f"Acknowledged {n} critical errors.", "ok")
        return redirect(url_for("errors"))

    # ------------------------------------------------------------ hot zones
    @app.get("/hot_zones")
    @guarded
    def hot_zones():
        profs = fetch_all("SELECT id, professor_name FROM professor_profiles ORDER BY professor_name")
        zones = fetch_all("SELECT * FROM hot_zones ORDER BY professor_id, day_of_week, hour_start")
        for p in profs:
            p["zones"] = [z for z in zones if z["professor_id"] == p["id"]]
        return render_template("hot_zones.html", profs=profs)

    @app.post("/hot_zones/add")
    @guarded
    def hot_zones_add():
        f = request.form
        day, h0, h1 = int(f["day_of_week"]), int(f["hour_start"]), int(f["hour_end"])
        if not (0 <= day <= 6 and 0 <= h0 < h1 <= 24):
            raise ValueError("day must be 0 to 6 and 0 <= start < end <= 24")
        execute_query("INSERT INTO hot_zones (professor_id, day_of_week, hour_start, hour_end, source) VALUES (%s,%s,%s,%s,'manual')",
                      (int(f["professor_id"]), day, h0, h1))
        flash("Hot zone added.", "ok")
        return redirect(url_for("hot_zones"))

    @app.post("/hot_zones/<int:zone_id>/delete")
    @guarded
    def hot_zones_delete(zone_id):
        n = execute_query("DELETE FROM hot_zones WHERE id = %s AND source = 'manual'", (zone_id,))
        flash("Hot zone deleted." if n else "Only manual hot zones can be deleted.", "ok" if n else "error")
        return redirect(url_for("hot_zones"))

    # ------------------------------------------------------------- schedule
    @app.get("/schedule")
    @guarded
    def schedule():
        d = _parse_date(request.args.get("date"))
        blocks = fetch_all("SELECT * FROM schedule_blocks WHERE date = %s ORDER BY start_time, id", (d,))
        return render_template("schedule.html", d=d, blocks=blocks, prev=d - timedelta(days=1), next=d + timedelta(days=1))

    def _back_to_schedule(block_id=None):
        d = request.form.get("date")
        if not d and block_id:
            row = fetch_one("SELECT date FROM schedule_blocks WHERE id = %s", (block_id,))
            d = row["date"].isoformat() if row else None
        return redirect(url_for("schedule", date=d) if d else url_for("schedule"))

    @app.post("/schedule/<int:block_id>/skip")
    @guarded
    def schedule_skip(block_id):
        back = _back_to_schedule(block_id)
        schedule_builder.skip_block(block_id, (request.form.get("reason") or "manual").strip() or "manual")
        return back

    @app.post("/schedule/<int:block_id>/unskip")
    @guarded
    def schedule_unskip(block_id):
        back = _back_to_schedule(block_id)
        schedule_builder.unskip_block(block_id)
        return back

    @app.post("/schedule/add")
    @guarded
    def schedule_add():
        f = request.form
        if f["time_category"] not in CATEGORIES:
            raise ValueError("bad time_category")
        if f["end_time"] <= f["start_time"]:
            raise ValueError("end_time must be after start_time")
        execute_query("INSERT INTO schedule_blocks (date, time_category, label, start_time, end_time, priority, moveable) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                      (f["date"], f["time_category"], f["label"].strip()[:255], f["start_time"], f["end_time"],
                       max(1, min(5, int(f.get("priority") or 3))), f.get("moveable") == "on"))
        flash("Block added.", "ok")
        return redirect(url_for("schedule", date=f["date"]))

    @app.post("/schedule/<int:block_id>/delete")
    @guarded
    def schedule_delete(block_id):
        back = _back_to_schedule(block_id)
        execute_query("DELETE FROM schedule_blocks WHERE id = %s", (block_id,))
        flash("Block deleted.", "ok")
        return back

    # -------------------------------------------------------------- presets
    @app.get("/presets")
    @guarded
    def presets():
        cfg = cfg_now()
        return render_template("presets.html", difficulty=get(cfg, "difficulty_presets", {}) or {}, tp=get(cfg, "time_presets", {}) or {})

    @app.post("/presets")
    @guarded
    def presets_save():
        f, cfg = request.form, cfg_now()
        update = {"_path": cfg.get("_path")}
        if f.get("form") == "difficulty":
            update["difficulty_presets"] = {t: max(1, min(5, int(f.get(f"d_{t}") or 2))) for t in TYPES}
        else:
            keys = ("sleep_target_hours", "commute_minutes", "breakfast_minutes", "lunch_minutes", "dinner_minutes", "chores_minutes", "alpha")
            update["time_presets"] = {k: float(f.get(k) or 0) if k in ("alpha", "sleep_target_hours") else int(float(f.get(k) or 0)) for k in keys}
        save_config(update)
        app.config["cfg"] = reload_config(cfg.get("_path"))
        flash("Presets saved.", "ok")
        return redirect(url_for("presets"))

    # ------------------------------------------------------------- syllabus
    @app.get("/syllabus")
    @guarded
    def syllabus():
        cfg = cfg_now()
        quarter = get(cfg, "quarter_label", "") or None
        budgets = get(cfg, "anthropic.per_class_claude_budget_usd", {}) or {}
        rows = fetch_all("SELECT * FROM courses WHERE active = TRUE ORDER BY canvas_course_name")
        for c in rows:
            c["parsed"] = fetch_one("SELECT * FROM syllabus_parsed WHERE course_id = %s AND quarter <=> %s ORDER BY id DESC LIMIT 1", (c["id"], quarter))
            c["spent"] = syllabus_parser.spent_this_quarter(c["id"], quarter)
            c["budget"] = budgets.get(c["canvas_course_name"])
            c["sources"] = session.get(f"syl_{c['id']}")
            c["assignments"] = fetch_all("SELECT id, title FROM assignments WHERE course_id = %s ORDER BY title", (c["id"],))
            if c["parsed"]:
                pj = _jsonload(c["parsed"]["parsed_json"], {})
                c["parsed_items"] = pj.get("items") if isinstance(pj, dict) else None
                c["validation_errors"] = pj.get("validation_errors") if isinstance(pj, dict) else None
                names = {a["id"]: a["title"] for a in c["assignments"]}
                for it in c["parsed_items"] or []:
                    it["matched_title"] = names.get(it.get("matched_assignment_id"))
        return render_template("syllabus_trigger.html", courses=rows, quarter=quarter, model=get(cfg, "anthropic.parse_model"))

    @app.post("/syllabus/<int:course_id>/find")
    @guarded
    def syllabus_find(course_id):
        course = fetch_one("SELECT canvas_course_id FROM courses WHERE id = %s", (course_id,))
        found = find_syllabus_sources(cfg_now(), course["canvas_course_id"])
        session[f"syl_{course_id}"] = {"has_body": bool(found.get("syllabus_body")),
                                       "files": [{"id": f["id"], "display_name": f["display_name"]} for f in found.get("files", [])][:20]}
        flash(f"Found {'a syllabus body' if found.get('syllabus_body') else 'no syllabus body'} and {len(found.get('files', []))} candidate files.", "ok")
        return redirect(url_for("syllabus"))

    @app.post("/syllabus/<int:course_id>/parse")
    @guarded
    def syllabus_parse(course_id):
        source = request.form.get("source") or "body"
        text = syllabus_parser.fetch_source(cfg_now(), course_id, source)
        r = syllabus_parser.parse(cfg_now(), course_id, text)
        flash(r["message"], "ok" if r["status"] == "ok" else "error")
        return redirect(url_for("syllabus"))

    @app.post("/syllabus/<int:parsed_id>/review")
    @guarded
    def syllabus_review(parsed_id):
        row = fetch_one("SELECT * FROM syllabus_parsed WHERE id = %s", (parsed_id,))
        pj = _jsonload(row["parsed_json"], {})
        f = request.form
        for i, it in enumerate(pj.get("items") or []):
            it["predicted_open"] = f.get(f"open_{i}") or None
            it["predicted_due"] = f.get(f"due_{i}") or None
            aid = f.get(f"assignment_{i}")
            it["matched_assignment_id"] = int(aid) if aid else None
            if it["matched_assignment_id"]:
                execute_query("UPDATE assignments SET syllabus_predicted_open = %s, syllabus_predicted_due = %s WHERE id = %s AND course_id = %s",
                              (syllabus_parser._at(it["predicted_open"]), syllabus_parser._at(it["predicted_due"]), it["matched_assignment_id"], row["course_id"]))
        execute_query("UPDATE syllabus_parsed SET parsed_json = %s, human_reviewed = TRUE WHERE id = %s", (json.dumps(pj), parsed_id))
        flash("Review saved.", "ok")
        return redirect(url_for("syllabus"))

    # -------------------------------------------------------------- control
    @app.post("/control/recalc_stress")
    @guarded
    def control_recalc():
        r = stress.calculate_and_store(date.today(), cfg_now())
        flash(f"Stress recalculated: {r['ratio']:.2f} ({r['band']}).", "ok")
        return redirect(url_for("index"))

    @app.post("/control/rebuild_schedule")
    @guarded
    def control_rebuild():
        r = schedule_builder.run_for_today(date.today(), cfg_now())
        flash(f"Schedule re evaluated: {len(r['moved'])} moved, {len(r['skipped'])} skipped, S={r['ratio']:.2f}.", "ok")
        return redirect(url_for("index"))

    @app.post("/control/restart_poller")
    @guarded
    def control_restart():
        pidfile = os.path.join(repo_root(), get(cfg_now(), "paths.run_dir", "run"), "poller.pid")
        with open(pidfile) as fh:
            pid = int(fh.read().strip())
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            os.kill(pid, signal.SIGTERM)
        flash(f"SIGTERM sent to poller pid {pid}; the supervisor will restart it.", "ok")
        return redirect(url_for("index"))

    return app


def run():
    try:
        cfg = load_config()
        validate_config(cfg)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
    run_dir = os.path.join(repo_root(), get(cfg, "paths.run_dir", "run"))
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "gui.pid"), "w") as f:
        f.write(str(os.getpid()))
    try:
        execute_query("INSERT INTO process_state (process_name, pid, started_at, last_heartbeat) VALUES ('gui',%s,NOW(),NOW()) "
                      "ON DUPLICATE KEY UPDATE pid=VALUES(pid), started_at=NOW(), last_heartbeat=NOW()", (os.getpid(),))
    except Exception as e:
        log_error(SCRIPT, type(e).__name__, "process_state start", str(e))
    app = create_app(cfg)
    app.run(host=get(cfg, "supervisor.gui_host", "0.0.0.0"), port=int(get(cfg, "supervisor.gui_port", 5000)), debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(run())
