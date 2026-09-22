# Athena scaffold, run 2 of 4: poller and core

**Model: Fable 5.1 (`claude-fable-5-1`).** This is the run where every module has to agree with every other one (supervisor, three pollers, stress, scheduler, profiler, email, AWS push). Use the strongest model once, here.

---

You are scaffolding run 2 of 4 of Athena. Run 1 already exists. Before writing anything, read in full: `db/schema.sql`, `config/config.yaml`, `core/config.py`, `db/db.py`, `logs/error_handler.py`, `core/notifier.py`, `requirements.txt`. Use their exact function signatures and table columns. Do not modify run 1 files unless you find a real gap in the schema; if you add a column, add it to schema.sql as well and list the change at the end.

This run creates exactly these files: `pollers/poller.py`, `pollers/canvas.py`, `pollers/github.py`, `pollers/oura.py`, `core/main.py`, `core/supervisor.py`, `core/stress.py`, `core/schedule_builder.py`, `core/professor_profiler.py`, `core/progress.py`, `core/email_sender.py`, `core/aws_push.py`. Write every file in full. No placeholders, no stubs. Fill implementation gaps yourself; only surface product decisions.

## Process model

Two long running processes come from this run. `core/main.py` is the supervisor and the only process the startup script launches. `pollers/poller.py` is launched by main as a child process with `subprocess.Popen` and `start_new_session=True` so it has its own process group. Main also launches `gui/app.py` the same way (run 3 writes it; launch it now, and if the file does not exist yet log a warning and continue). Main never imports anything from `pollers/`. The poller never imports anything from `core/` except `core.config` and `core.notifier`. The poller writes to MySQL; main reads MySQL and writes only its own derived tables (stress_scores, schedule_blocks, professor_profiles, hot_zones, github_commits no commit rows, assignment_changes.reported, assignments.profiled, announcements.profiled, process_state).

Rules for every module: wrap every operation in try/except and write every caught exception to error_log through `log_error` before continuing; log assertion failures through `log_assertion`; parameterised SQL only; no cron, APScheduler triggers only; no environment variables; importable without side effects, all scheduling and signal handling lives under `if __name__ == "__main__"` or inside an explicit `run()` function.

## pollers/poller.py

One `BlockingScheduler` with three jobs, each `max_instances=1`, `coalesce=True`, `misfire_grace_time=30`: `canvas.run_cycle` every `canvas.poll_seconds`, `github.run_cycle` every `github.poll_seconds`, `oura.run_cycle` at each time in `oura.poll_times` using APScheduler `CronTrigger` (this is an in process trigger, not system cron). Each job wrapper records the cycle duration in microseconds and the outcome into `poller_status`, and writes a heartbeat to `process_state` with process_name poller. On SIGTERM or SIGINT set a stop flag, let the running cycle finish, shut the scheduler down, write state paused to poller_status for all three, exit 0. On an AUTH_FAILURE raised by a cycle: pause only that job, set poller_status.state to auth_failed for that API, log critical, and keep the other jobs running. The paused job stays paused until the poller process is restarted. Write the poller PID to `run/poller.pid` on start.

Each cycle module exposes `run_cycle(cfg)` and a module level `AuthFailure` exception. Every HTTP call goes through a shared helper in each module that times the request, writes a `poll_metrics` row (api_name, endpoint path without query string, response_time_us, http_status), raises `AuthFailure` on 401, and returns the response. Use `requests` directly against both REST APIs; do not use canvasapi or PyGithub, they hide the rate limit and ETag headers this design depends on. Timeouts of 15 seconds on every request.

## pollers/canvas.py

Bearer token from `canvas.token`, base URL from `canvas.base_url`. Follow Link header pagination on every list call. Read `X-Rate-Limit-Remaining` on every response and log a warning once per cycle if it drops below `canvas.rate_limit_warn_threshold`; on a 403 with Retry-After sleep for that many seconds and end the cycle. Per cycle:

1. `GET /api/v1/courses?enrollment_state=active&include[]=teachers&per_page=100`. Upsert into `courses` on canvas_course_id (name, quarter from config, active true). For each teacher name upsert a `professor_profiles` row on professor_name with zeroed stats and link `courses.professor_id`. Any course with `mapped` false that was not seen before this cycle triggers one `send_warning` telling the user to open the GUI and map it (track already notified course ids in memory for the process lifetime and also skip if the course row already existed).
2. Per active course: `GET /api/v1/courses/{id}/assignments?include[]=submission&per_page=100`. Assignment group names are fetched once per day per course from `/assignment_groups` and cached in memory for classification. Classify assignment_type by matching `assignment_type_keywords` against the lowercased group name first, then the title, else other. Parse assignment_number as the first integer in the title. Derive status: submission.workflow_state graded to graded, submitted to submitted, else open if unlock_at is null or in the past, else pending. Upsert on canvas_assignment_id. When due_at, available_from, or title differs from the stored row, write an `assignment_changes` row with old and new values before updating. New rows carry canvas_posted_at from the assignment created_at field.
3. Per active course: `GET /api/v1/courses/{id}/discussion_topics?only_announcements=true&per_page=50`. Insert new announcements on canvas_announcement_id; never update existing ones.
4. Once per day per course (track in memory by date): `GET /api/v1/courses/{id}?include[]=syllabus_body` and `GET /api/v1/courses/{id}/files?search_term=syllabus`. Record what was found in a `syllabus_sources` in memory dict and expose `find_syllabus_sources(cfg, course_id)` as a plain function the GUI can call on demand in run 3 (it returns the syllabus body HTML if present and a list of candidate file dicts with id, display_name, url, content_type).

Expected call volume at four courses: roughly nine calls per minute, well under the 700 per ten minutes ceiling.

## pollers/github.py

Fine grained PAT from `github.pat`, owner from `github.username`. Read `X-RateLimit-Remaining` and `X-RateLimit-Reset` on every response; if remaining is below `github.rate_limit_backoff_threshold` set poller_status state to backoff, log a warning, and skip cycles until the reset time passes. Keep an in memory dict of ETags keyed by request URL and send `If-None-Match`; a 304 means nothing changed and costs nothing against the limit.

Per cycle, for each course with `mapped` true and a non empty repo_name: for each branch in `courses.branches`, `GET /repos/{owner}/{repo}/commits?sha={branch}&path={repo_path_prefix}&since={last_logged_commit_timestamp_for_course_or_30_days_ago}&per_page=50`. For each commit SHA not already in github_commits: `GET /repos/{owner}/{repo}/commits/{sha}` for the file list, then for each changed file under the prefix `GET /repos/{owner}/{repo}/contents/{path}?ref={sha}` for its size (removed files record size 0). Insert one github_commits row per file per commit with prev_file_size_bytes taken from the most recent logged row for that file path in that repo and size_delta computed from it.

Assignment matching: strip the prefix from the file path, take the first remaining path segment as the category folder and map it through `github.category_folders` to an assignment_type, take the first integer found in the second segment (or the filename if there is no second segment) as the number, then match against `assignments` for that course on (assignment_type, assignment_number). No match leaves assignment_id null. Never write no commit rows here; main does that.

## pollers/oura.py

PAT from `oura.pat`. On each run: `GET https://api.ouraring.com/v2/usercollection/daily_sleep` and `daily_readiness` for the last `oura.backfill_days` days, plus `sleep` documents for total_sleep_duration and efficiency (use the long_sleep type per day). Upsert oura_daily per date: real data sets missing false, data_source api, and filled_at when the row previously existed as missing. Any date in the window with no data and no row gets a row with missing true and data_source estimated. If today's data is absent and this run's scheduled time is `oura.poll_times[oura.morning_poll_index]`, `send_warning` telling the user to open the Oura app and sync. Determine which scheduled slot is running by comparing the current time to the configured list (nearest slot), do not rely on job ordering.

## core/supervisor.py

`Supervisor(cfg)` with `start_child(name, argv)`, `stop_all(grace_seconds)`, `check_children()`. Children are started with `subprocess.Popen(..., start_new_session=True, cwd=repo_root)`, stdout and stderr appended to `logs/{name}.out`. PIDs written to `run/{name}.pid` and to `process_state`. `check_children()` runs every 30 seconds from main: a dead child is restarted after `supervisor.restart_backoff_seconds` with a warning logged; more than `supervisor.max_restarts_per_10min` restarts in ten minutes logs critical and stops restarting that child. `stop_all` sends SIGTERM to each child's process group, waits up to `shutdown_grace_seconds` for a clean exit, then SIGKILL, and removes pidfiles. A `restart_child(name)` used by the GUI restart button (the GUI does this by sending SIGTERM to the pidfile PID; the supervisor's check then restarts it, so main needs no IPC).

## core/main.py

`run()`: load and validate config (exit 1 with the ConfigError text on failure), verify MySQL with a trivial query (exit 1 on failure after logging critical), write `run/main.pid` and process_state, start children if enabled in `supervisor`, then start a `BackgroundScheduler` with these jobs, each `max_instances=1`, `coalesce=True`:

- `check_children` every 30 seconds
- `professor_profiler.sweep` every 60 seconds
- `stress.calculate_and_store(today)` every `stress.recalc_minutes` minutes and once at startup
- `schedule_builder.run_for_today` daily at `schedule_builder.run_time`
- `progress.no_commit_sweep` daily at 23:55 for that day
- `email_sender.send_morning` daily at `email.morning_time`
- `email_sender.send_evening` daily at `email.evening_time`
- `aws_push.push_all` every `aws.push_seconds` seconds if `aws.enabled`
- heartbeat to process_state every 60 seconds

Main then blocks on a signal wait loop, never on a network call; the only network calls in main are in aws_push and email_sender, and both run inside scheduler jobs with hard timeouts. On SIGTERM: shut the scheduler down (wait for running jobs), `stop_all`, remove pidfile, exit 0.

## core/stress.py

`calculate(date, cfg)` returns a dict; `calculate_and_store(date)` writes a stress_scores row and returns it. The equation:

S_day = ( Σ over active assignments of e^(x_i / 1.248) / D_i  +  alpha * max(0, sleep_target_hours - s) ) / T_available

- Active assignments: status pending or open, due_at not null, due_at later than now minus one day. No seven day cutoff; the 1/D_i term handles horizon.
- x_i: difficulty from `difficulty_presets[assignment_type]`, 1 to 5. e^(x/1.248) is the estimated hours (about 2 to 55).
- D_i: days until due as a float, floored at `stress.min_days_remaining`.
- s: hours slept, `oura_daily.total_sleep_seconds / 3600` for that date; if the row is missing or marked missing use the mean of the last seven non missing rows; if none exist use `sleep_target_hours`.
- alpha and sleep_target_hours from `time_presets`.
- T_awake = 24 minus s.
- T_class = hours of schedule_blocks for that date with time_category class, not skipped. T_fixed likewise for fixed. T_clubs likewise for clubs.
- T_travel = travel blocks for that date if any exist, otherwise `commute_minutes / 60`.
- T_chores = chores blocks for that date if any exist, otherwise `chores_minutes / 60`, plus (breakfast + lunch + dinner minutes) / 60 always.
- Flexible blocks are not subtracted; they are what the available time gets spent on alongside study.
- T_available = T_awake minus the five T terms, floored at `stress.min_available_hours` with a warning logged when the floor is hit.
- Output is a ratio, not a percentage. No cap. Store every component in the stress_scores row. Expose `band(ratio, cfg)` returning green, amber, or red using `stress.green_below` and `stress.amber_below`, and `latest_for(date)`.

## core/schedule_builder.py

`run_for_today()` re evaluates existing blocks, never rebuilds. Steps: recompute S for today; if S is above `stress.amber_below` mark every moveable clubs block skipped with reason `stress_overloaded` and recompute; then resolve overlaps in start_time order: any flexible block overlapping a class, fixed, or clubs block that is not skipped moves to the nearest free gap of equal length later the same day within waking hours (wake time is today's date at 24 minus s hours before the first class block, or 07:00 if no class), recording original_start_time; if no gap fits, mark it skipped with reason `no_gap`. Write all changes back, log each skip as a warning with the reason, store the final S. Also expose `skip_block(block_id, reason)` and `unskip_block(block_id)` for the GUI.

## core/professor_profiler.py

`sweep()` processes rows where profiled is false in assignments, announcements, and assignment_changes, oldest first, marking each profiled after use. For each event resolve the professor through the course. Observation timestamp is canvas_posted_at, posted_at, or detected_at respectively. Update as rolling values using observation_count as the weight: typical_post_days (weekday of the event added to the set, kept as the set of weekdays seen in at least 20 percent of observations), typical_post_hour_start and end (10th and 90th percentile of observed hours, approximated by keeping a running histogram of 24 bins in memory per sweep and persisting the derived values). For assignments with a syllabus_predicted_open date compute delta_days = actual minus predicted; positive updates avg_late_post_days, negative updates avg_early_post_days (rolling means over their own counts), and adherence for that observation is max(0, 1 minus abs(delta_days) / 7); syllabus_adherence_score is the rolling mean of per observation adherence. Increment observation_count. After each professor's update, replace their `hot_zones` rows with source profiler (leave manual rows untouched): one row per typical weekday with the typical hour window.

## core/progress.py

`no_commit_sweep(date)`: for every active assignment (status pending or open) on a mapped course with no github_commits row dated that day, insert one row with commit_sha NULL, no_commit true, logged_at that date. `commits_for_day(date)` grouped by course and assignment for the evening email. `commit_summary_for_assignment(assignment_id)` returning commit_count, last_commit_at, total_size_delta, no_commit_days for the dashboard payloads.

## core/email_sender.py

Plain SMTP with STARTTLS from the `email` block, HTML body with a plain text alternative, 20 second timeout. Morning: today's S ratio and band, today's schedule blocks in order with skipped ones marked, assignments due within seven days sorted by due_at with course name and type, announcements detected in the last 24 hours, Oura data that arrived since the previous morning, and today's hot zone windows per professor (informational). Evening: commits logged today grouped by course and assignment with size deltas, no commit assignments, today's latest S, assignment_changes with reported false (then mark them reported), and an error log section that appears only if error_log has unacknowledged rows dated today. Both return True on send and log a warning on failure.

## core/aws_push.py

`push_all()` builds every snapshot below from MySQL and POSTs one body `{"snapshots": {key: payload}}` to `{aws.api_url}/ingest` with a bearer `aws.api_token`, 10 second timeout, one attempt per cycle, warning on failure. Snapshot keys and shapes (run 4 serves these unchanged):

- `stress_today`: {date, ratio, band, t_available, t_awake, deadline_term, sleep_penalty, hours_slept, calculated_at}
- `stress_history`: list of {date, ratio} for the last 90 days, latest row per date
- `assignments_upcoming`: list of {course_name, title, assignment_type, due_at, days_remaining, status, commit_count, last_commit_at}, status pending or open, due within 14 days, sorted by due_at
- `schedule_today`: list of {label, time_category, start_time, end_time, moveable, skipped, skip_reason}
- `commits_recent`: last 50 github_commits rows joined to course and assignment: {course_name, assignment_title, file_path, size_delta, file_size_bytes, commit_message, commit_timestamp, no_commit}
- `oura_recent`: last 14 oura_daily rows: {date, sleep_score, readiness_score, hrv_avg, resting_hr, total_sleep_seconds, missing, data_source}
- `errors_active`: {critical_count, latest: {timestamp, script_name, error_type, raw_message} or null}
- `poll_metrics_summary`: {canvas, github, oura} each {last_poll, avg_response_us over the last hour, state from poller_status}

## Finish

`python -m py_compile` every file. Then a dry run: import each module with the repo root on sys.path and confirm no side effects. List any schema additions and any product decisions you made. Nothing else.
