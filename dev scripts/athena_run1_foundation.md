# Athena scaffold, run 1 of 4: foundation

**Model: Opus 5 (`claude-opus-5`).** Well specified, foundational, no cross module reasoning yet. Fable is not needed here.

---

You are scaffolding run 1 of 4 of Athena, a locally hosted academic management platform for a UC Santa Cruz student. This run creates the foundation every later run imports: the MySQL schema, the YAML config, the config loader, the database module, the error handler, and the Telegram notifier. Write every file in full. No placeholders, no stubs, no TODOs. Do not create any file outside the list in this run. Fill every implementation gap yourself; only surface product decisions (what it does, who it serves), never technical ones.

## System overview (context only, later runs build this)

One always on Ubuntu machine (i7, RTX 3070). Three processes:

1. `pollers/poller.py`: one process, one APScheduler instance, three jobs. Canvas (canvas.ucsc.edu, Instructure hosted) every 60 seconds, GitHub every 60 seconds, Oura four times a day. Writes only to MySQL. Never imported by main.
2. `core/main.py`: the supervisor. Launches the poller and the Flask GUI as child processes at startup, restarts them if they die, sends SIGTERM to both on shutdown. Runs its own APScheduler jobs that read MySQL only: professor profiler sweep, stress calculation, schedule builder, no commit sweep, morning and evening email, and a push of JSON snapshots to a FastAPI app on AWS.
3. `gui/app.py`: Flask GUI on the local network for configuration and control. The only place that calls the Canvas API and Claude API on demand (syllabus parsing).

On AWS: FastAPI receives the snapshots and serves a React dashboard. Telegram for urgent notifications, SMTP email for morning and evening summaries. No cron anywhere, no environment variables for secrets, everything reads from `config/config.yaml`. All MySQL writes use parameterised queries. Every file is importable without side effects.

## Full directory tree (this run creates only the starred files)

```
athena/
  requirements.txt              *
  .gitignore                    *
  config/
    config.yaml                 *
  core/
    config.py                   *
    notifier.py                 *
    main.py
    supervisor.py
    stress.py
    schedule_builder.py
    professor_profiler.py
    progress.py
    email_sender.py
    aws_push.py
    syllabus_parser.py
  db/
    schema.sql                  *
    db.py                       *
  logs/
    error_handler.py            *
  pollers/
    poller.py
    canvas.py
    github.py
    oura.py
  gui/
    app.py
    static/style.css
    templates/
      base.html index.html course_mapping.html professor_profiles.html
      error_log.html hot_zones.html schedule.html presets.html syllabus_trigger.html
  api/
    fastapi_app.py store.py config.yaml requirements.txt athena_api.service
  frontend/
    package.json vite.config.js index.html
    src/App.jsx src/api.js src/config.js
    src/components/StressMeter.jsx AssignmentList.jsx DailySchedule.jsx
                   ProgressTracker.jsx ErrorBanner.jsx PollMetrics.jsx
  scripts/
    startup.sh shutdown.sh athena.service
  run/            (pidfiles, gitignored)
```

Every Python module must be runnable with the repo root on `sys.path` and import as `from core.config import load_config`, `from db.db import fetch_all`, etc. Add an empty `__init__.py` to `core`, `db`, `logs`, `pollers`, and `gui`.

## db/schema.sql

Complete DDL, InnoDB, utf8mb4, `CREATE TABLE IF NOT EXISTS`, correct types, foreign keys, and indexes on every column used in a WHERE or JOIN below. Use DATETIME(6) where microseconds matter.

`professor_profiles`: id, professor_name (UNIQUE, keyed by name so returning professors carry their profile into a new quarter), syllabus_adherence_score FLOAT (0 to 1), avg_early_post_days FLOAT, avg_late_post_days FLOAT, typical_post_days JSON (array of weekday ints 0 to 6), typical_post_hour_start TINYINT, typical_post_hour_end TINYINT, observation_count INT, last_updated

`courses`: id, canvas_course_id (UNIQUE), canvas_course_name, professor_id FK nullable, repo_name, repo_path_prefix (e.g. `Q1/ECE1`, the subfolder inside the repo that holds this class), branches JSON (array of branch names, default `["main"]`), quarter, mapped BOOLEAN, active BOOLEAN, created_at

`assignments`: id, course_id FK, canvas_assignment_id (UNIQUE), title, assignment_type ENUM(hw, quiz, test, lab, project_milestone, reading, other), assignment_number INT nullable (digits parsed from the title, used for commit matching), available_from, due_at, syllabus_predicted_open, syllabus_predicted_due, status ENUM(pending, open, submitted, graded), canvas_posted_at, profiled BOOLEAN default false, created_at, updated_at. No difficulty column; difficulty comes from config presets keyed by assignment_type.

`assignment_changes`: id, assignment_id FK, field_changed ENUM(due_at, available_from, title), old_value, new_value, detected_at, profiled BOOLEAN default false, reported BOOLEAN default false (set once the evening email has included it)

`announcements`: id, course_id FK, canvas_announcement_id (UNIQUE), title, body TEXT, posted_at, detected_at, profiled BOOLEAN default false

`hot_zones`: id, professor_id FK, day_of_week TINYINT (0 to 6), hour_start TINYINT, hour_end TINYINT, source ENUM(profiler, manual), created_at. Informational only; polling frequency never changes.

`oura_daily`: id, date DATE UNIQUE, sleep_score, readiness_score, hrv_avg, resting_hr, total_sleep_seconds, sleep_efficiency, data_source ENUM(api, estimated), missing BOOLEAN, filled_at, created_at

`stress_scores`: id, date, ratio FLOAT, t_awake, t_class, t_travel, t_clubs, t_chores, t_fixed, t_available, deadline_term, sleep_penalty, hours_slept, calculated_at. Index on (date, calculated_at). Multiple rows per date are expected; the latest calculated_at wins.

`schedule_blocks`: id, date, time_category ENUM(class, travel, clubs, chores, fixed, flexible), label, start_time, end_time, priority TINYINT, moveable BOOLEAN, skipped BOOLEAN, skip_reason, original_start_time nullable (set when the builder shifts a block), created_at

`github_commits`: id, course_id FK, assignment_id FK nullable, repo_name, branch, file_path, commit_sha nullable (NULL marks a no commit record), commit_message, commit_author, commit_timestamp, file_size_bytes, prev_file_size_bytes, size_delta, no_commit BOOLEAN default false, logged_at. UNIQUE on (commit_sha, file_path) where commit_sha is not null (use a generated column or an application level check, MySQL does not support partial unique indexes).

`syllabus_parsed`: id, course_id FK, raw_text LONGTEXT, parsed_json JSON, parse_model, parse_cost_usd DECIMAL(8,4), validated BOOLEAN, human_reviewed BOOLEAN, quarter, created_at

`error_log`: id, timestamp DATETIME(6), script_name, error_type, operation, raw_message TEXT, severity ENUM(warning, critical), acknowledged BOOLEAN default false

`poll_metrics`: id, timestamp DATETIME(6), api_name ENUM(canvas, github, oura), endpoint, response_time_us INT, http_status SMALLINT

`poller_status`: api_name ENUM(canvas, github, oura) PRIMARY KEY, state ENUM(running, paused, auth_failed, backoff), last_cycle_at, last_cycle_duration_us, last_error, updated_at

`process_state`: process_name VARCHAR PRIMARY KEY (main, poller, gui), pid INT, started_at, last_heartbeat, updated_at

## config/config.yaml

Every field present with a clear comment above it and an empty or default value. Group under top level keys:

- `canvas`: token, base_url (`https://canvas.ucsc.edu`), poll_seconds (60), rate_limit_warn_threshold (100)
- `github`: pat, username, poll_seconds (60), rate_limit_backoff_threshold (200), category_folders (map of repo folder name to assignment_type: HW to hw, Labs to lab, Tests to test, Quizzes to quiz, Projects to project_milestone, Readings to reading)
- `oura`: pat, poll_times (list of four HH:MM strings, default 07:00, 12:00, 17:00, 22:00), backfill_days (7), morning_poll_index (0)
- `anthropic`: api_key, parse_model (`claude-sonnet-5`), per_class_claude_budget_usd (dict keyed by canvas_course_name, empty by default)
- `telegram`: bot_token, chat_id
- `email`: smtp_host, smtp_port, smtp_user, smtp_password, recipient, morning_time (07:30), evening_time (21:00)
- `mysql`: host, port, user, password, db
- `aws`: api_url, api_token, push_seconds (60), enabled (true)
- `quarter_label`
- `assignment_type_keywords`: map of assignment_type to a list of lowercase substrings matched against the Canvas assignment group name and title (hw: homework, hw, problem set; quiz: quiz; test: exam, midterm, final, test; lab: lab; project_milestone: project, milestone; reading: reading)
- `difficulty_presets`: hw 2, quiz 2, test 5, lab 3, project_milestone 4, reading 1, other 2 (each 1 to 5, comment that e^(x/1.248) maps this onto roughly 2 to 55 estimated hours)
- `time_presets`: sleep_target_hours 8, commute_minutes, breakfast_minutes, lunch_minutes, dinner_minutes, chores_minutes, alpha (sleep penalty strength, default 2.0)
- `stress`: recalc_minutes (15), green_below (0.8), amber_below (1.0), min_available_hours (0.5), min_days_remaining (0.25)
- `schedule_builder`: run_time (06:30)
- `supervisor`: start_poller true, start_gui true, gui_host 0.0.0.0, gui_port 5000, restart_backoff_seconds (10), max_restarts_per_10min (3), shutdown_grace_seconds (30)
- `paths`: run_dir (`run`), log_dir (`logs`)

## core/config.py

`load_config(path=None)` returns the parsed dict, cached after first call, path defaults to `config/config.yaml` relative to the repo root (resolve from `__file__`, not the working directory). `validate_config(cfg)` raises `ConfigError` listing every missing or empty required key in one message; required keys are the five tokens (canvas.token, github.pat, oura.pat, anthropic.api_key, telegram.bot_token) plus telegram.chat_id and the full mysql block. `get(cfg, "a.b.c", default)` dotted lookup helper. `save_config(cfg)` writes back with ruamel.yaml round trip mode so comments survive (the GUI presets page uses this). `repo_root()` helper.

## db/db.py

Single connection manager on mysql connector with a small pool. Expose `get_connection()` (context manager, commits on clean exit, rolls back on exception), `execute_query(sql, params=None)` returning lastrowid or rowcount, `execute_many(sql, seq_of_params)`, `fetch_all(sql, params=None)` returning list of dicts, `fetch_one(sql, params=None)`. Retry once on a lost connection. Raise `DBError` on failure; never log from inside db.py (the error handler imports db, so db must not import the error handler). `apply_schema()` reads schema.sql and executes each statement. `if __name__ == "__main__"` with `--init` applies the schema and `--check` prints connectivity and table count.

## logs/error_handler.py

`log_error(script_name, error_type, operation, raw_message, severity="warning")` writes to error_log with a parameterised insert; if severity is critical it also calls `notifier.send_critical` with a one line summary. If the database write itself fails, append the entry as one JSON line to `logs/fallback.log` and call `notifier.send_critical` about the database failure. `get_todays_errors()`, `get_unacknowledged(severity=None, script_name=None, date_from=None, date_to=None, page=1, per_page=50)` returning rows plus total count, `acknowledge_error(error_id)`, `unacknowledged_critical_count()`, `errors_for_date(date)` for the evening email. A `log_assertion(script_name, operation, condition, message)` helper: if the condition is false it logs a warning and returns False rather than raising, so callers can decide whether to continue.

## core/notifier.py

Plain HTTPS calls to the Telegram Bot API `sendMessage` endpoint with requests and a 10 second timeout (synchronous, no async library). Expose `send_message(text)`, `send_warning(text)` (prefixed with a warning marker), `send_critical(text)` (prefixed with a critical marker). Never raise: on failure return False and write a warning to `logs/fallback.log` directly (not through the error handler, to avoid a loop where a failed critical alert tries to send another critical alert). Truncate messages to 4000 characters.

## requirements.txt and .gitignore

requirements.txt: apscheduler, requests, mysql-connector-python, pyyaml, ruamel.yaml, flask, anthropic, pypdf. Pin major versions. .gitignore: `config/config.yaml`, `run/`, `logs/*.log`, `__pycache__/`, `frontend/node_modules/`, `frontend/dist/`, `frontend/src/config.js`, `api/config.yaml`.

## Finish

Run `python -m py_compile` on every Python file, run the schema through a MySQL syntax check if a server is available, and end with a short list of any product decisions you had to make. Do not list technical decisions.
