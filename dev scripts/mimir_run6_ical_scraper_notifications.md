# Mimir run 6: iCal poller, Playwright scraper, notification protocol update

**Model: Sonnet 5 (`claude-sonnet-5`).** Updating existing modules and adding new ones against a stable base. No cross module redesign.

---

You are issuing a targeted update to Mimir, an academic management platform. Runs 1 to 5 exist and are on main. This is not a rewrite. Read every file listed below in full before touching anything. Preserve all existing logic unless this prompt explicitly replaces it. Do not remove any existing database columns, scheduler jobs, or notification calls unless told to.

Before writing anything read in full: `db/schema.sql`, `config/config.yaml`, `config/config.example.yaml`, `core/config.py`, `db/db.py`, `logs/error_handler.py`, `core/notifier.py`, `core/main.py`, `core/stress.py`, `core/email_sender.py`, `pollers/poller.py`, `pollers/canvas.py`, `pollers/oura.py`, `gui/app.py`, `gui/templates/base.html`, `gui/templates/index.html`, `gui/templates/course_mapping.html`.

This run creates or modifies exactly these files: `pollers/ical_poller.py` (new), `pollers/canvas_scraper.py` (new), `pollers/poller.py` (modify), `core/email_sender.py` (modify), `core/notifier.py` (modify), `core/main.py` (modify, scheduler jobs only), `db/schema.sql` (modify, additive only), `db/migrations/002_run6_schema.sql` (new), `config/config.yaml` (modify, additive only), `config/config.example.yaml` (modify, additive only), `gui/app.py` (modify, add routes only), `gui/templates/course_mapping.html` (modify), `gui/templates/course_monitoring.html` (new). No other files.

---

## Schema additions

Add to `db/schema.sql` and `db/migrations/002_run6_schema.sql` using `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` and `CREATE TABLE IF NOT EXISTS` only. Never drop or alter existing columns.

Add to `courses`:
- `monitor` BOOLEAN DEFAULT true
- `telegram_enabled` BOOLEAN DEFAULT true
- `email_enabled` BOOLEAN DEFAULT true
- `midterm_assignment_id` INT nullable FK to assignments
- `final_assignment_id` INT nullable FK to assignments
- `ical_course_code` VARCHAR(50) nullable (must match the bracketed suffix in the iCal SUMMARY field exactly, e.g. ECE-141-01)

Add to `assignments`:
- `canvas_assignment_url` VARCHAR(500) nullable
- `ical_uid` VARCHAR(200) nullable UNIQUE
- `is_midterm` BOOLEAN DEFAULT false
- `is_final` BOOLEAN DEFAULT false
- `grade_percent` FLOAT nullable
- `grade_detected_at` DATETIME nullable
- `reminder_24h_sent` BOOLEAN DEFAULT false

New table `oura_intraday`:
```sql
CREATE TABLE IF NOT EXISTS oura_intraday (
  id INT AUTO_INCREMENT PRIMARY KEY,
  date DATE NOT NULL,
  poll_time TIME NOT NULL,
  readiness_score SMALLINT,
  hrv_avg FLOAT,
  stress_high BOOLEAN DEFAULT false,
  stress_threshold_used FLOAT,
  notified BOOLEAN DEFAULT false,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_oura_intraday_date (date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

Add `ical` and `scraper` to the `api_name` ENUM in `poller_status`. Since MySQL cannot ADD to an ENUM with IF NOT EXISTS, the migration must use:
```sql
ALTER TABLE poller_status MODIFY COLUMN api_name ENUM('canvas','github','oura','ical','scraper');
```

---

## pollers/ical_poller.py (new)

Fetches and parses the Canvas iCal feed. Pure HTTP, no browser.

`run_cycle(cfg)`: fetch `cfg["ical"]["feed_url"]` replacing `webcal://` with `https://`, 15 second timeout. On HTTP error log to error_log and return. Parse with the `icalendar` library.

**For each VEVENT:**

Course matching: extract the course code from the bracketed suffix at the end of SUMMARY, e.g. `[ECE-141-01]`. Match against `courses.ical_course_code`. If no match and a course row has NULL ical_course_code whose canvas_course_name contains a similar string, update it automatically. If still no match, log warning and skip. Skip if matched course has `monitor` false.

Date parsing: DTSTART VALUE=DATE means 23:59:59 Pacific on that date. DTSTART with Z suffix is UTC, convert to Pacific. DTEND same handling. available_from stays NULL if absent.

Assignment type detection (check in this order, case insensitive against SUMMARY):
- final exam, final → is_final true, assignment_type test
- midterm, mid-term, mid term → is_midterm true, assignment_type test
- quiz → quiz
- lab → lab
- homework, hw, problem set → hw
- project, milestone → project_milestone
- reading → reading
- discussion → other
- else → other

Also detect tests and quizzes posted as calendar events rather than Canvas assignment submissions; the iCal feed includes all calendar event types regardless of Canvas category.

Upsert logic: match on ical_uid first, then (course_id, title) as fallback. New row: status pending if DTSTART is future, open if past. Set canvas_assignment_url from URL field. Set is_midterm or is_final. If is_midterm update courses.midterm_assignment_id; if is_final update courses.final_assignment_id. Existing row: if due_at changed write assignment_changes row before updating. Never downgrade status.

New assignment Telegram notification: only if the row is genuinely new AND due_at is within `notifications.new_assignment_telegram_hours` hours AND courses.telegram_enabled is true. Call `notifier.send_warning`.

24 hour reminder: if due_at within 24 hours, status not submitted or graded, reminder_24h_sent false, telegram_enabled true: call `notifier.send_assignment_reminder_24h`, set reminder_24h_sent true.

Record poll_metrics row for the fetch. Log all exceptions to error_log.

---

## pollers/canvas_scraper.py (new)

Playwright based scraper for announcements, submission status, grade detection, and available_from. Uses saved session cookies.

`COOKIE_PATH = "run/canvas_cookies.json"`

`SessionExpiredError(Exception)`: raised when the saved session is no longer valid.

`load_session(playwright)`: launch Chromium headless, load cookies from COOKIE_PATH if it exists, navigate to `https://canvas.ucsc.edu`, check page title. If title contains Dashboard return browser and page. If login form visible raise SessionExpiredError with message: "Canvas session expired. Run: python pollers/canvas_scraper.py --login"

`interactive_login(playwright)`: launch Chromium in headed mode, navigate to UCSC Canvas login page, wait for the user to complete login and Duo Mobile manually, wait until URL contains "dashboard" or 60 second timeout. On success save all cookies to COOKIE_PATH as JSON. Print confirmation.

`run_cycle(cfg)`: if COOKIE_PATH does not exist log warning and return. Load session; on SessionExpiredError log warning, call `notifier.send_session_expired()`, return. For each course where monitor is true:

1. Announcements: navigate to `/courses/{canvas_course_id}/announcements`. Scrape each announcement title, body text, and posted date. Upsert into announcements on (course_id, title, posted_at). Mark profiled false on new rows.

2. Assignment status and available_from: navigate to `/courses/{canvas_course_id}/assignments`. For each visible assignment row: match to assignments table by normalised lowercase title. Extract available date if shown and update available_from if currently NULL. Extract submission status from badge: submitted → submitted, graded → graded, else leave as is. If status changed to graded extract grade percentage if visible, write to grade_percent and grade_detected_at.

3. Wait 2 seconds between course navigations.

On any Playwright exception: log to error_log with page URL, take screenshot to `logs/scraper_error_{timestamp}.png`, close browser, return.

`if __name__ == "__main__"`: if `--login` in argv launch interactive_login with a synchronous Playwright context, print user instructions about completing Duo Mobile, confirm cookie save.

Add `playwright` and `icalendar` to `requirements.txt`. Add a comment that `playwright install chromium` must be run once after pip install.

---

## pollers/poller.py (modify)

Read the existing file fully before editing. Add exactly two new jobs to the existing BlockingScheduler, following the same pattern as existing jobs (max_instances=1, coalesce=True, misfire_grace_time=30, poller_status write, heartbeat):

1. `ical_poller.run_cycle` every `ical.poll_minutes` minutes.
2. `canvas_scraper.run_cycle` every `scraper.poll_minutes` minutes.

Do not touch any existing jobs or their configuration.

---

## core/notifier.py (modify, append only)

Add these four functions at the bottom. Do not touch existing functions.

`send_assignment_reminder_24h(assignment, course)`: critical marker, title, course code, "due in X hours", current status.

`send_stress_alert(current_score, baseline_score)`: warning marker, "Stress above baseline: current {x} vs average {y}. Check your schedule."

`send_grade_notification(assignment, course, grade_percent)`: plain message with assignment title, course, and grade percentage. Used in evening email summary only, not called by the scraper directly.

`send_session_expired()`: warning marker, "Canvas session expired. SSH into the server and run: python pollers/canvas_scraper.py --login"

---

## core/email_sender.py (modify)

Read the existing send_morning and send_evening functions fully before editing. Make only the changes listed here and nothing else.

**Morning email changes:**

Stress section: move stress ratio prediction here. Show ratio to two decimals, band (green/amber/red), T_available, deadline term, sleep penalty. Remove stress from the evening send.

Announcements: replace with a clearly marked comment `# ANNOUNCEMENTS: held pending LLM summarisation (run 7)` and exclude announcements from the morning email entirely for now.

Oura section: replace with last night's actual sleep_score and readiness_score from oura_daily, the trailing 7 day mean for each, and the signed delta. Label as "Last night vs your average."

Add after assignments due in 7 days — Upcoming milestones section: for each active course read midterm_assignment_id and final_assignment_id. If midterm exists and due_at is in the future show "Midterm in X days." If midterm is past and final exists show "Final in X days." If neither column is set but an assignment with is_midterm or is_final true exists for that course use that instead. Always show this section even when X is large.

Add after milestones — Tests and projects on the radar: all active assignments with assignment_type in (test, quiz, project_milestone) regardless of due date, sorted by due_at. Always show this section even if empty (show "None active").

**Evening email changes:**

Remove stress section entirely (now in morning).

Add Oura intraday section: pull all oura_intraday rows for today, show readiness at each poll time, flag rows where stress_high is true with the threshold that triggered it.

Per assignment status log: for each active assignment across all monitored email_enabled courses show title, course code, assignment type, status, commit count today, total size delta today, days remaining. Group by course. Graded assignments show grade_percent if set.

Add grades section: any assignment where grade_detected_at is today. Show title, course, grade_percent.

Keep existing error log section, GitHub commits section, and assignment_changes section exactly as they are.

---

## core/main.py (modify, scheduler jobs only)

Read the existing file fully before editing. Add one new BackgroundScheduler job only. Do not change supervisor, signal handling, process startup, or any existing jobs.

New job: `oura_stress_check` every 60 minutes. Reads the most recent oura_intraday row for today. Computes baseline as the mean readiness_score across the last 14 oura_intraday rows excluding today. If current readiness is more than `oura.stress_alert_threshold_pct` percent below baseline and that row's notified is false: call `notifier.send_stress_alert(current, baseline)`, set notified true, write back.

---

## config/config.yaml and config/config.example.yaml (modify, additive only)

Read both files fully before editing. Add only the blocks below. Do not remove or rename any existing keys.

```yaml
ical:
  # Full webcal:// or https:// URL from Canvas Calendar > Calendar Feed
  feed_url: ""
  # How often to poll the iCal feed in minutes
  poll_minutes: 10

scraper:
  # How often to run the Playwright scraper in minutes
  poll_minutes: 15
  # Path to saved Canvas session cookies relative to repo root
  cookie_path: "run/canvas_cookies.json"
```

Add to existing `oura` block:
```yaml
  # Percentage below baseline readiness that triggers a Telegram stress alert
  stress_alert_threshold_pct: 15
```

Add to existing `notifications` block (or create it if absent):
```yaml
notifications:
  assignment_reminder_days: 3
  urgent_reminder_hours: 24
  new_assignment_telegram_hours: 48
```

---

## gui/app.py (modify, add routes only)

Read the existing file fully before editing. Add these routes only. Do not touch existing routes.

`GET /monitoring`: renders course_monitoring.html with all courses, their monitor, telegram_enabled, email_enabled flags, ical_course_code, midterm countdown days (null if not set), and final countdown days (null if not set).

`POST /monitoring/{course_id}`: accepts form fields monitor, telegram_enabled, email_enabled (booleans), ical_course_code (text). Updates courses row. Flash success. Redirect to /monitoring.

`POST /control/refresh_session`: flash message telling the user to SSH in and run `python pollers/canvas_scraper.py --login`. Does not launch a browser.

---

## gui/templates/course_monitoring.html (new)

One row per course. Columns: canvas_course_name, ical_course_code (editable text input), Monitor toggle checkbox, Telegram toggle checkbox, Email toggle checkbox, midterm countdown ("X days" or "not detected"), final countdown ("X days" or "not detected"), Save button per row. Plain table, consistent with existing template style in base.html. Note at top: "ical_course_code must match the bracketed suffix in your Canvas iCal feed exactly, e.g. ECE-141-01."

---

## gui/templates/course_mapping.html (modify)

Read the existing file fully before editing. Add one link to /monitoring in the page header. No other changes.

---

## gui/templates/base.html (modify)

Read the existing file fully before editing. Add "Monitoring" to the nav linking to /monitoring. No other changes.

---

## Finish

`python -m py_compile` every new or modified Python file. Run the migration SQL through sqlglot for syntax. List schema additions and product decisions made. Nothing else.
