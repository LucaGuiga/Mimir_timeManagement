# Mimir Project — Full Session Export
**Date:** 2026-09-29
**User:** Luca Guiga (UCSC student, kennethguiga@gmail.com)
**Project:** Mimir — locally hosted academic management platform

---

## Project Overview

Mimir is a self-hosted Python platform that monitors academic workload, manages git repos for coursework, tracks health data via Oura, and sends intelligent notifications via Telegram and email. It runs as a persistent background service on a local Linux machine (Ubuntu / MariaDB).

The system has 7 completed runs (Claude Code prompts executed as PRs). Runs 1 through 7 are all on main. The system has not been launched yet as of this session.

---

## Architecture Summary

| Layer | Technology |
|---|---|
| Backend scheduler | APScheduler (BlockingScheduler in poller, BackgroundScheduler in main) |
| Database | MySQL / MariaDB |
| Config | YAML only — no environment variables, never committed |
| Local GUI | Flask |
| Remote dashboard | FastAPI + React on AWS |
| Notifications | Telegram bot (Huginn/Muninn) + SMTP email |
| Course data | Canvas iCal feed (Canvas API permanently unavailable post-breach) |
| Web scraping | Playwright with cookie-based Canvas session |
| Health data | Oura API |
| Repo management | GitHub REST API (raw requests, no PyGithub) |

**Process model:** `main.py` supervises `poller.py` and `gui/app.py` as subprocesses. Main reads MySQL only; poller writes MySQL.

---

## The Canvas API Situation

In April–May 2026, Canvas (Instructure) suffered a major breach (ShinyHunters group, 3.65TB of data). UCSC responded by locking token generation entirely. As of this session, Canvas API tokens cannot be obtained for UCSC accounts. The Playwright scraper (run 6) handles session-cookie-based scraping as a partial replacement. The iCal feed (which contains an embedded auth token in the URL) is used as the primary source of assignment data.

**Implication for Mimir:** The `pollers/canvas.py` module remains in the codebase but always AUTH_FAILs. This is expected and non-blocking. The `courses` table, previously seeded by Canvas, must be seeded via the iCal-based GUI flow added in run 7.

---

## Run History

### Run 1 — Foundation
**Model:** Opus 5
**Output:** `athena_run1_foundation.md`

Created the base schema (15 tables), `config/config.yaml`, `db/db.py`, `logs/error_handler.py`, `core/notifier.py` (Telegram), `requirements.txt`, `.gitignore`.

Key schema tables: `courses`, `assignments`, `assignment_changes`, `github_commits`, `oura_daily`, `oura_intraday`, `poll_metrics`, `poller_status`, `schema_migrations`, and others.

Notable fix: MariaDB rejected SHA1 in generated columns. Fixed with composite unique index on `(commit_sha, file_path)`.

---

### Run 2 — Poller and Core
**Model:** Fable 5.1
**Output:** `athena_run2_poller_and_core.md`

Created: `pollers/poller.py`, `pollers/canvas.py`, `pollers/github.py`, `pollers/oura.py`, `core/main.py`, `core/supervisor.py`, `core/stress.py`, `core/schedule_builder.py`, `core/professor_profiler.py`, `core/progress.py`, `core/email_sender.py`, `core/aws_push.py`.

Stress equation used across the system:
```
S_day = (Σ e^(x_i/1.248)/D_i + alpha * max(0, sleep_target - s)) / T_available
```

---

### Run 3 — Flask GUI
**Model:** Sonnet 5
**Output:** `athena_run3_flask_gui.md`

Created: `gui/app.py`, `core/syllabus_parser.py`, 9 Jinja2 templates including `base.html`, `index.html`, `course_mapping.html`.

---

### Run 4 — AWS and Scripts
**Model:** Sonnet 5
**Output:** `athena_run4_aws_and_scripts.md`

Created: FastAPI backend, React dashboard, `scripts/startup.sh`, `scripts/shutdown.sh`, `athena.service` (systemd unit, later renamed to `mimir.service`).

---

### Run 5 — Repo Naming, Installer, Smart Notifications
**Model:** Sonnet 5
**Output:** `mimir_run5_repo_naming_installer.md`
**PR:** #6 (merged)

**Note:** Run 6 was written and merged before run 5 ran. Run 5 was updated to treat runs 1–4 and 6 as the base.

Created:
- `core/repo_manager.py` — GitHub repo creation via REST API. Creates private repos under `github.username`, commits placeholder `.gitkeep` files to create folder structure (`HW/`, `Labs/`, `Tests/`, `Quizzes/`, `Projects/`, `Readings/`), creates branches (`main`, `hw`, `labs`, `tests`, `quizzes`, `projects`, `readings`), updates `courses` table, sends Telegram confirmation.
- `core/naming_schema.py` — filename validation. Schema: `{type}{number}_q{question}_part{part}_{course_code}`. E.g. `Lab1_q1_part1_ECE1`. `part0` = single part; `part1` without a `part2` companion triggers a warning.
- `core/installer.py` — interactive installer and non-interactive updater. Applies migrations, installs git hooks, optionally sets up systemd service, sends Telegram test message.
- `scripts/install.sh`, `scripts/update.sh`
- `hooks/commit-msg.sample` — pre-commit hook that validates staged filenames against naming schema.
- `scripts/validate_hook.py`
- `db/migrations/003_add_reminder_sent_today.sql`

Migration numbering note: run 6 used `001_` and `002_`. This run's file is `003_`.

Config additions:
```yaml
repo_manager:
  auto_create: true   # changed to false in run 7
  default_branches: [main, hw, labs, tests, quizzes, projects, readings]
  default_folders: [HW, Labs, Tests, Quizzes, Projects, Readings]
  course_code_pattern: "[A-Z]+\\d+"

notifications:
  assignment_reminder_days: 3
```

Added `pollers/oura.py` feature: `write_intraday_snapshot()` — called at end of every `run_cycle`, writes a row to `oura_intraday` with current readiness and HRV.

Added GUI routes: `GET /repos`, `POST /repos/{course_id}/create`, `gui/templates/repos.html`.

---

### Run 6 — iCal Poller, Playwright Scraper, Notification Updates
**Model:** Sonnet 5
**Output:** `mimir_run6_ical_scraper_notifications.md`
**PR:** #5 (merged first, before run 5)

Created:
- `pollers/ical_poller.py` — fetches Canvas iCal feed (`webcal://` → `https://`), parses VEVENTs, extracts `[COURSE-CODE]` from SUMMARY brackets, matches against `courses.ical_course_code`, upserts assignments, fires Telegram for new assignments due within 48h and 24h reminders. Uses negative synthetic IDs for calendar events (exams etc.) that have no Canvas assignment ID.
- `pollers/canvas_scraper.py` — Playwright-based scraper. Cookie persistence at `run/canvas_cookies.json`. `--login` flag for manual Duo Mobile first-time auth. Scrapes announcements, submission status, grades, `available_from`.

Schema additions:
- `courses`: `monitor`, `telegram_enabled`, `email_enabled`, `midterm_assignment_id`, `final_assignment_id`, `ical_course_code`
- `assignments`: `canvas_assignment_url`, `ical_uid`, `is_midterm`, `is_final`, `grade_percent`, `grade_detected_at`, `reminder_24h_sent`
- New table: `oura_intraday`
- `poller_status.api_name` ENUM extended to include `ical` and `scraper`

Config additions:
```yaml
ical:
  feed_url: ""
  poll_minutes: 10

scraper:
  poll_minutes: 15
  cookie_path: "run/canvas_cookies.json"

oura:
  stress_alert_threshold_pct: 15

notifications:
  urgent_reminder_hours: 24
  new_assignment_telegram_hours: 48
```

Added notifier functions: `send_assignment_reminder_24h`, `send_stress_alert`, `send_grade_notification`, `send_session_expired`.

Email sender changes:
- Stress calculation moved from evening to morning email.
- Morning email: Oura sleep/readiness delta, upcoming milestones (midterm/final countdowns), tests and projects radar section. Announcements placeholder pending LLM summarisation (run 8).
- Evening email: Oura intraday section, per-assignment status log, grades section.

Added `oura_stress_check` job to `core/main.py` (every 60 min): checks intraday readiness against 14-day baseline, fires Telegram if below threshold.

GUI additions: `GET /monitoring`, `POST /monitoring/{course_id}`, `gui/templates/course_monitoring.html`.

---

### Run 7 — iCal Course Bootstrap and Repo Setup GUI
**Model:** Sonnet 5
**Output:** `mimir_run7_course_bootstrap.md`
**Status:** Written this session. Not yet executed.

**Context:** Critical architectural fix before first launch. Canvas API was the only path to populate the `courses` table. Without it, the iCal poller has nothing to match against. Run 7 adds a deliberate GUI flow to seed courses from the iCal feed.

Created:
- `core/ical_bootstrap.py`
  - `fetch_and_parse(cfg)` — fetches iCal feed on demand, extracts all unique `[COURSE-CODE]` values from SUMMARY brackets, returns deduplicated list with course names and lab pairing data.
  - Lab detection: L-suffix heuristic. `ECE-141L-01` → strip L from segment → `ECE-141-01` → if that code also exists in the feed, it's a lab section for it. Auto-detected, user-confirmed via GUI.
  - `seed_courses(cfg, conn, selections)` — inserts course rows with synthetic negative `canvas_course_id` (same pattern run 6 uses for calendar events), sets `ical_course_code`, `is_lab`, `lab_parent_id`, `monitor`.
  - `preview_repos(cfg, seeded_courses)` — builds preview structure (repo name, branches, folders) without touching GitHub.
  - `BootstrapError(Exception)`

- `db/migrations/004_run7_schema.sql`
  - `courses.lab_parent_id` INT nullable
  - `courses.is_lab` BOOLEAN DEFAULT false
  - `courses.setup_complete` BOOLEAN DEFAULT false

- `gui/app.py` routes added:
  - `GET /setup` (multi-step: fetch → select → preview → done)
  - `POST /setup/fetch`
  - `POST /setup/seed`
  - `POST /setup/create`
  - Multi-step state held in Flask session.

- `gui/templates/course_setup.html` — step-aware template. Select step shows lecture courses with lab sections indented beneath. Preview step shows styled cards per repo. Done step links to `/monitoring` and dashboard.

Config change: `repo_manager.auto_create` set to `false` (was `true` in run 5).

---

## Naming Schema Reference

```
{type}{number}_q{question}_part{part}_{course_code}
```

Examples:
- `Lab1_q1_part1_ECE1`
- `HW2_q3_part0_ECE101`

Rules:
- `type`: Lab, HW, Test, Quiz, Project, Reading (normalised to title case)
- `part0` = single-part file
- `part1` without a `part2` companion staged = warning at commit time
- `course_code` matches `[A-Z]+\d+` pattern from config

---

## Telegram Bot

Bot name: Huginn (sender) / Muninn (context, not a separate bot — just the naming convention).
Test commands to verify the bot is working:
```bash
# Get chat ID
curl "https://api.telegram.org/bot<TOKEN>/getUpdates"

# Send test message
curl -X POST "https://api.telegram.org/bot<TOKEN>/sendMessage" \
  -d "chat_id=<CHAT_ID>&text=Mimir+test"
```

---

## Migration Sequence

| File | Run | Content |
|---|---|---|
| `001_` | Run 6 | Run 6 schema (iCal/scraper additions) |
| `002_run6_schema.sql` | Run 6 | Same run, secondary migration |
| `003_add_reminder_sent_today.sql` | Run 5 | `reminder_sent_today` column on assignments |
| `004_run7_schema.sql` | Run 7 | `lab_parent_id`, `is_lab`, `setup_complete` on courses |

Note: `reminder_sent_today` (daily reset, run 5) is distinct from `reminder_24h_sent` (one-time, run 6).

---

## Pre-Launch Checklist

Before filling in `config/config.yaml` and running for the first time:

1. Merge run 7 PR and confirm it is on main.
2. Set `repo_manager.auto_create: false` in config (run 7 does this).
3. Fill in config:
   - `github.token` (PAT with repo write permissions)
   - `github.username`
   - `ical.feed_url` (from Canvas Calendar > Calendar Feed — treat as a password, contains embedded auth token)
   - `telegram.bot_token` and `telegram.chat_id`
   - `oura.personal_access_token`
   - `smtp.*` credentials
   - `flask.secret_key` (any random string)
   - `mysql.*` credentials
4. Run `python db/db.py --init` to apply schema.
5. Run `pip install playwright icalendar && playwright install chromium`.
6. Set up Canvas Playwright session: `python pollers/canvas_scraper.py --login` (completes Duo Mobile manually once, saves cookies).
7. Navigate to `http://localhost:<port>/setup` and run the course bootstrap flow.
8. Verify Telegram bot is working (test message from installer or manual curl).
9. Start the service.

---

## Bookmarked Future Runs

| Run | Description |
|---|---|
| Run 8 | LLM summarisation of Canvas announcements (placeholder in morning email) |
| Future | Submission status confirmation via Playwright scraping |
| Future | Archive module: monthly export of `poll_metrics` to NAS via rsync/rclone |

---

## Key Technical Decisions and Gotchas

- **Negative synthetic IDs:** Calendar events (exams, finals) from iCal have no Canvas assignment ID. Use `-(abs(hash(uid)) % 1000000 + 1)` to generate a negative synthetic ID that satisfies NOT NULL UNIQUE without colliding with real Canvas IDs (positive integers in the millions).
- **MariaDB FK syntax:** `ADD FOREIGN KEY IF NOT EXISTS` works; `ADD CONSTRAINT IF NOT EXISTS` does not. Always use the former.
- **MariaDB ENUM migration:** Cannot `ADD` to an ENUM with IF NOT EXISTS. Must use `MODIFY COLUMN` with the full new ENUM definition.
- **iCal URL:** `webcal://` must be replaced with `https://` before fetching. The URL contains an embedded auth token — treat identically to a password, gitignore it.
- **Flask session secret:** Required for multi-step setup GUI. Set in config under `flask.secret_key`.
- **Virtual environment:** Absolute paths throughout. venv at `venv/` relative to repo root.
- **`reminder_sent_today` vs `reminder_24h_sent`:** Two different columns. `reminder_sent_today` resets daily (run 5). `reminder_24h_sent` is a one-time flag that never resets (run 6).
- **`config/config.example.yaml`:** The actual `config/config.yaml` is gitignored. The example file is the committed template.
- **JSX in .js files:** Vite refuses JSX in `.api.js`. Use `createElement` instead or rename to `.jsx`.

---

## Output Files

All prompt files are at `/mnt/user-data/outputs/`:

| File | Run |
|---|---|
| `athena_run1_foundation.md` | Run 1 |
| `athena_run2_poller_and_core.md` | Run 2 |
| `athena_run3_flask_gui.md` | Run 3 |
| `athena_run4_aws_and_scripts.md` | Run 4 |
| `mimir_run5_repo_naming_installer.md` | Run 5 |
| `mimir_run6_ical_scraper_notifications.md` | Run 6 |
| `mimir_run7_course_bootstrap.md` | Run 7 |
