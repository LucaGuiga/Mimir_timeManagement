# Mimir run 7: iCal course bootstrap, lab pairing, repo setup GUI

**Model: Sonnet 5 (`claude-sonnet-5`).** New GUI flow and course seeding. No scheduler changes.

---

You are adding run 7 to Mimir. Runs 1 through 6 are all on main. Before writing anything read in full: `db/schema.sql`, `config/config.yaml`, `config/config.example.yaml`, `core/config.py`, `db/db.py`, `logs/error_handler.py`, `core/notifier.py`, `core/repo_manager.py`, `core/naming_schema.py`, `pollers/ical_poller.py`, `core/main.py`, `gui/app.py`, `gui/templates/base.html`. Use their exact signatures and table columns.

This run creates or modifies exactly: `core/ical_bootstrap.py` (new), `db/migrations/004_run7_schema.sql` (new), `gui/app.py` (modify, add routes only), `gui/templates/course_setup.html` (new), `config/config.yaml` (modify, additive only), `config/config.example.yaml` (modify, additive only). No other files.

---

## Context and architectural change

The Canvas API is permanently gone for this deployment. `pollers/canvas.py` remains in the codebase but its `run_cycle` will always AUTH_FAIL; this is expected and non-blocking. The `courses` table was previously seeded by Canvas. Going forward the iCal feed is the sole source of course discovery. Run 7 provides the manual bootstrap GUI that seeds the `courses` table before first launch. The `check_new_courses` job in `core/main.py` already respects `repo_manager.auto_create`; ensure config sets it to false (see config section below).

---

## Schema additions

Add to `db/schema.sql` and `db/migrations/004_run7_schema.sql` using `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` only. Never drop or alter existing columns.

Add to `courses`:
- `lab_parent_id` INT nullable — FK to `courses.id`. When set, this course row is a lab section and its parent is the lecture course. Used for display grouping only; both rows are independent in the DB.
- `is_lab` BOOLEAN DEFAULT false — true when this course is a lab section (has an L-suffix course code).
- `setup_complete` BOOLEAN DEFAULT false — set true after the user confirms course selection and repos are created via run 7 GUI.

Migration file `db/migrations/004_run7_schema.sql`:

```sql
ALTER TABLE courses ADD COLUMN IF NOT EXISTS lab_parent_id INT NULL;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS is_lab BOOLEAN DEFAULT false;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS setup_complete BOOLEAN DEFAULT false;
```

No foreign key constraint on `lab_parent_id` — MariaDB FK syntax varies and the column is display-only. Leave it unconstrained.

---

## core/ical_bootstrap.py (new)

This module fetches the iCal feed on demand (not on a schedule), parses all unique course codes, detects lab pairings, and provides the course seeding logic for the GUI. It is never called by the scheduler.

### `fetch_and_parse(cfg) -> list[dict]`

Fetch `cfg["ical"]["feed_url"]` replacing `webcal://` with `https://`, 15-second timeout. On HTTP error raise `BootstrapError` with message. Parse with `icalendar` library.

For each VEVENT in the feed:
- Extract the bracketed suffix from the end of SUMMARY using regex `\[([A-Z0-9\-]+)\]$`. This is the `ical_course_code` (e.g. `ECE-141-01`, `MUSC-11C-01`).
- Extract the human-readable course name: everything in SUMMARY before the bracket, stripped. E.g. `"Computer Organization and Assembly Language"` from `"Computer Organization and Assembly Language [ECE-141-01]"`.
- Skip VEVENTs with no bracketed suffix.
- Deduplicate by `ical_course_code`. Keep the first human-readable name seen per code.

Return a list of dicts, one per unique course code:
```python
{
    "ical_course_code": str,   # e.g. "ECE-141-01"
    "course_name": str,        # human-readable name before bracket
    "is_lab": bool,            # True if L-suffix detected (see pairing logic)
    "lab_for": str | None,     # ical_course_code of parent if is_lab, else None
}
```

### Lab pairing logic

After collecting all unique codes, run pairing detection:

1. Build a set of all codes.
2. For each code, check if it contains `L` immediately before the section number separator. The pattern is: split the code on `-`. If any segment matches `[A-Z]+L` (letter(s) followed by capital L), and removing the L from that segment produces a code that exists in the set, this code is a lab section.
   - Example: `ECE-141L-01` → segment `141L` → strip L → `141` → reconstruct `ECE-141-01` → exists → `ECE-141L-01` is a lab for `ECE-141-01`.
   - Example: `MUSC-11C-01` → no segment matches `[A-Z]+L` pattern → not a lab.
3. Set `is_lab = True` and `lab_for = parent_code` for matched lab codes.
4. Sort the final list: lecture courses first (is_lab=False), lab courses last. Within each group sort alphabetically by ical_course_code.

### `BootstrapError(Exception)`

Plain exception with a `message` property. Used when the feed fetch fails.

### `seed_courses(cfg, conn, selections) -> list[dict]`

Called after the user confirms their selection in the GUI. `selections` is a list of dicts:
```python
{
    "ical_course_code": str,
    "course_name": str,
    "is_lab": bool,
    "lab_for": str | None,   # ical_course_code of parent, used to look up lab_parent_id
    "monitor": bool,         # from user checkbox in GUI
}
```

For each selection:
1. Check if a row already exists in `courses` with this `ical_course_code`. If yes, update `monitor`, `telegram_enabled`, `email_enabled` to match, leave everything else. Skip INSERT.
2. If no existing row: INSERT into `courses`:
   - `canvas_course_id`: use a negative synthetic ID. Generate as `-(abs(hash(ical_course_code)) % 1000000 + 1)` to avoid collision with real IDs and satisfy NOT NULL. This is the same negative-ID pattern run 6 uses for calendar events.
   - `canvas_course_name`: the `course_name` from selection.
   - `ical_course_code`: from selection.
   - `is_lab`: from selection.
   - `monitor`: from selection.
   - `telegram_enabled`: true.
   - `email_enabled`: true.
   - `mapped`: false (repo not yet created).
   - `setup_complete`: false.
   - All other columns: NULL or their schema defaults.
3. After inserting all rows, resolve `lab_parent_id`: for each inserted lab row, look up the `id` of the row whose `ical_course_code` matches `lab_for`, then UPDATE `courses SET lab_parent_id = <parent_id> WHERE ical_course_code = <lab_code>`.
4. Return a list of dicts: one per inserted/updated course row with `id`, `ical_course_code`, `canvas_course_name`, `is_lab`, `lab_parent_id`, `monitor`.

### `preview_repos(cfg, seeded_courses) -> list[dict]`

Given the list returned by `seed_courses`, build a preview structure without touching GitHub. Returns:
```python
[
    {
        "course_id": int,
        "ical_course_code": str,
        "repo_name": str,          # derived by repo_manager course_code_pattern from ical_course_code
        "is_lab": bool,
        "branches": list[str],     # from cfg["repo_manager"]["default_branches"]
        "folders": list[str],      # from cfg["repo_manager"]["default_folders"]
        "lab_folder_note": str | None,  # "Lab sections tracked in Labs/ folder" if is_lab else None
    }
]
```

For `repo_name`: apply the `course_code_pattern` regex from config to the `ical_course_code`. Extract the first match. If the pattern `[A-Z]+\d+` is used, this strips the section number and dashes. Example: `ECE-141-01` → match `ECE` and `141` → join as `ECE141`. If no match, use the full `ical_course_code` with dashes replaced by underscores as a fallback.

The `repo_name` must be identical to what `repo_manager.create_course_repo` would use, so read `core/repo_manager.py` carefully and use the same extraction logic.

---

## gui/app.py (modify, add routes only)

Read the existing file fully. Add these routes. Do not touch existing routes.

### `GET /setup`

Renders `course_setup.html`. On first load (no query params): render with `step="fetch"`, no courses data.

### `POST /setup/fetch`

Calls `ical_bootstrap.fetch_and_parse(cfg)`. On `BootstrapError` flash the error and redirect to `/setup`. On success store the result in `session["bootstrap_courses"]` (Flask session) and redirect to `/setup?step=select`.

Flask session data will be a JSON-serialisable list. Import `flask.session` and set `app.secret_key` from `cfg["flask"]["secret_key"]` if that key exists, otherwise use a hardcoded dev fallback `"mimir-dev-secret"` with a logged warning. Read `gui/app.py` to check if `secret_key` is already set; if it is, do not set it again.

### `GET /setup` with `?step=select`

Read `session["bootstrap_courses"]`. If missing redirect to `/setup`. Render `course_setup.html` with `step="select"` and `courses=session["bootstrap_courses"]`.

### `POST /setup/seed`

Accepts a form POST. Form fields:
- `selected_codes`: a multi-value field — one value per `ical_course_code` the user checked.
- `monitor_{ical_course_code}`: checkbox per course for monitoring toggle (present = true).

Build the `selections` list from the posted form data: only codes present in `selected_codes` are included. For each, look up the full dict from `session["bootstrap_courses"]` using `ical_course_code` as key, add `monitor` = true if `monitor_{code}` checkbox was posted.

Call `ical_bootstrap.seed_courses(cfg, conn, selections)`. Store the result in `session["seeded_courses"]`. Redirect to `/setup?step=preview`.

### `GET /setup` with `?step=preview`

Read `session["seeded_courses"]`. If missing redirect to `/setup`. Call `ical_bootstrap.preview_repos(cfg, session["seeded_courses"])`. Render `course_setup.html` with `step="preview"` and `preview=<preview_list>`.

### `POST /setup/create`

Read `session["seeded_courses"]`. For each course in the list where `monitor` is true:
- Call `repo_manager.create_course_repo(cfg, course_dict)` where `course_dict` has at minimum `id`, `canvas_course_name`, `ical_course_code`, `is_lab`. `create_course_repo` expects a course object; pass it a `SimpleNamespace` or dict — read `repo_manager.py` to see what fields it accesses and supply exactly those.
- Collect results: successes and failures.

After all repos attempted: UPDATE `courses SET setup_complete = true` for all successfully created repos. Clear `session["bootstrap_courses"]` and `session["seeded_courses"]`.

Flash a summary: "Created N repos. M failed." Redirect to `/setup?step=done`.

### `GET /setup` with `?step=done`

Render `course_setup.html` with `step="done"`. Query `courses` for all rows with `setup_complete = true`, pass to template.

---

## gui/templates/course_setup.html (new)

Single template that renders different content based on `step`. Extends `base.html`. Consistent with existing template style.

### Step: fetch

```
<h2>Course Setup</h2>
<p>Mimir will read your Canvas iCal feed and detect your enrolled courses automatically.</p>
<form method="POST" action="/setup/fetch">
  <button type="submit">Fetch Courses from iCal</button>
</form>
```

### Step: select

Display a table. Each row represents one course from `session["bootstrap_courses"]`.

Columns: Checkbox (include in setup), Course Code (`ical_course_code`), Course Name (`course_name`), Type (lecture or lab — derived from `is_lab`), Paired With (`lab_for` if is_lab, else shows which lab codes list this as their parent — compute this in the template by scanning the courses list), Monitor checkbox (default checked).

Group rows visually: lecture courses first, then their lab sections indented under them. Use a simple CSS class `lab-row` with `padding-left: 2rem` for indented rows. No JavaScript required.

Below the table:
```
<form method="POST" action="/setup/seed">
  <!-- hidden inputs for each course's metadata so the POST has full data -->
  <!-- one checkbox per course for "selected_codes" multi-value -->
  <!-- one checkbox per course for "monitor_{code}" -->
  <button type="submit">Continue to Preview</button>
</form>
```

Each row needs both a `<input type="checkbox" name="selected_codes" value="{ical_course_code}">` and a `<input type="checkbox" name="monitor_{ical_course_code}" checked>`.

### Step: preview

Heading: "Repo Preview — Review before creating"

For each item in `preview`:
```
Repo name:  {repo_name}
Course:     {ical_course_code} — {canvas_course_name from seeded_courses}
Type:       {"Lab section" if is_lab else "Lecture"}
Branches:   {comma-separated branch list}
Folders:    {comma-separated folder list}
Note:       {lab_folder_note if set}
```

Render as a styled card per course, not a table. Keep it readable at a glance.

Below all cards:
```
<form method="POST" action="/setup/create">
  <button type="submit" class="btn-primary">Verify — Create Repos</button>
</form>
<a href="/setup?step=select">← Go back and change selection</a>
```

### Step: done

```
<h2>Setup Complete</h2>
<p>The following courses are now tracked by Mimir:</p>
<ul>
  {% for c in courses %}
  <li>{{ c.ical_course_code }} — {{ c.canvas_course_name }}{% if c.is_lab %} (lab){% endif %}</li>
  {% endfor %}
</ul>
<p><a href="/monitoring">Configure monitoring settings →</a></p>
<p><a href="/">Go to dashboard →</a></p>
```

---

## config/config.yaml and config/config.example.yaml (modify, additive only)

Read both files fully before editing. Add only the lines below. Do not remove or rename any existing keys.

Change `repo_manager.auto_create` from `true` to `false`. This is the one existing key that must be changed (not added). Run 5 set it to true; run 7 corrects it. Without this change the scheduler would auto-create repos for any course rows on every 60-second tick.

```yaml
repo_manager:
  auto_create: false   # changed from true — repos are created via /setup GUI only
```

No other changes to `repo_manager` block.

Add under a `flask` block if not already present:
```yaml
flask:
  secret_key: ""  # set to a random string; used for session storage in the setup GUI
```

If a `flask` block already exists in the file (check before editing), add only `secret_key` to it.

---

## base.html (modify)

Read the existing file fully. Add one nav link: `<a href="/setup">Course Setup</a>`. Place it before the "Monitoring" link added in run 6. No other changes.

---

## Product decisions made

1. **Negative synthetic canvas_course_id**: same pattern run 6 uses for calendar events — `-(abs(hash(ical_course_code)) % 1000000 + 1)`. This satisfies NOT NULL and avoids collision with real Canvas IDs (which are positive integers in the millions).

2. **Lab detection is auto, selection is manual**: The L-suffix heuristic runs automatically during fetch. The user sees the result (indented lab rows) and decides which courses and labs to include via checkboxes. There is no forced pairing — a user can include a lab without its parent or vice versa.

3. **One repo per course, including labs**: Lab sections get their own repo with the same folder structure. `is_lab` is stored on the course row for downstream logic (e.g. the iCal poller can filter differently for labs vs lectures), but repo creation treats both identically.

4. **Canvas API removed as a dependency**: `pollers/canvas.py` remains and will AUTH_FAIL silently. `check_new_courses` in `core/main.py` still runs every 60 seconds but with `auto_create: false` it does nothing until explicitly enabled. No code is deleted; the Canvas path is just dormant.

5. **Flask session for multi-step state**: The fetch → select → preview → create flow stores intermediate state in Flask session (server-side cookie). This avoids re-fetching the iCal feed on every step and keeps the form POST data manageable. Secret key must be set in config.

6. **`setup_complete` flag**: Lets the done screen show confirmed courses and gives future runs a clean signal that bootstrap has been performed. The iCal poller's match logic already works once course rows exist with `ical_course_code` set — no other wiring needed.

---

## Finish

`python -m py_compile` every new or modified Python file. `bash -n` any shell scripts touched. List only schema additions and product decisions above. Nothing else.
