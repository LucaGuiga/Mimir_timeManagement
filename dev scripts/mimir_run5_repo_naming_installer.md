# Mimir run 5: auto repo creation, naming schema, smart notifications, installer

**Model: Sonnet 5 (`claude-sonnet-5`).** Well scoped feature additions on top of a stable base. No cross module redesign needed.

---

You are adding run 5 features to Mimir, an academic management platform. Runs 1 to 4 exist and are on main. Before writing anything read in full: `db/schema.sql`, `config/config.yaml`, `core/config.py`, `db/db.py`, `logs/error_handler.py`, `core/notifier.py`, `pollers/canvas.py`, `pollers/github.py`, `core/main.py`, `core/supervisor.py`. Use their exact signatures and table columns. Do not modify any existing file unless a schema addition is required; if you add a column, add it to schema.sql and list the change at the end.

This run creates exactly: `core/repo_manager.py`, `core/naming_schema.py`, `core/installer.py`, `scripts/install.sh`, `scripts/update.sh`, and one git hook template at `hooks/commit-msg.sample`. It also adds one new scheduled job to `core/main.py` and two new Flask routes to `gui/app.py`. Write every file in full. No placeholders, no stubs. Fill implementation gaps yourself; only surface product decisions.

---

## Feature 1: auto repo creation

`core/repo_manager.py`

`create_course_repo(cfg, course)`: called when Mimir detects a new course through Canvas that has no repo yet. Uses the GitHub PAT from config with the GitHub REST API (no PyGithub, raw requests only, same pattern as `pollers/github.py`). Creates a new private repo under `github.username` named after the course code (e.g. `ECE1`, parsed from the Canvas course name). Initialises it with a README, then creates the following folder structure on the default branch by committing placeholder `.gitkeep` files: `HW/`, `Labs/`, `Tests/`, `Quizzes/`, `Projects/`, `Readings/`. Creates branches: `main` (default), `hw`, `labs`, `tests`, `quizzes`, `projects`, `readings`. Updates the `courses` table: sets `repo_name`, `repo_path_prefix`, `branches`, and `mapped` true. Sends a Telegram notification confirming the repo was created with its URL and the branch list. On any API failure logs to error_log and sends a Telegram warning without raising past the caller.

`check_new_courses(cfg)`: reads all active unmapped courses from the `courses` table and calls `create_course_repo` for each one. Called as a scheduled job in `core/main.py` every 60 seconds alongside the existing check children job. Only acts if `repo_manager.auto_create` is true in config (default true, so add that field to the config).

Add to `config/config.yaml` under a new `repo_manager` block: `auto_create` (boolean, default true), `default_branches` (list, default hw/labs/tests/quizzes/projects/readings/main), `default_folders` (list matching the branches), `course_code_pattern` (regex string used to parse the course code from the Canvas course name, default `[A-Z]+\d+`).

---

## Feature 2: naming schema enforcement

`core/naming_schema.py`

The required filename schema is: `{type}{number}_q{question}_part{part}_{course_code}`

Examples: `Lab1_q1_part1_ECE1`, `HW2_q3_part0_ECE101`

Rules:
- `type` is one of: Lab, HW, Test, Quiz, Project, Reading (case insensitive on input, normalised to title case on output)
- `number` is a positive integer with no leading zeros
- `q{question}` is a positive integer with no leading zeros
- `part{part}` is a non negative integer; part0 means single part
- `course_code` matches the pattern from config

`validate_filename(filename, cfg)` returns a dict with keys: `valid` (boolean), `errors` (list of strings), `warnings` (list of strings). Rules for warnings (not errors): if part is 1 or higher, emit a warning that multiple parts were indicated but only one file is present; this check is done at commit time by the hook by scanning all staged files for the same type, number, and question prefix across all parts and warning if no part2 (or higher) companion is staged alongside a part1 file.

`parse_filename(filename)` returns a dict of the parsed components or raises `NamingSchemaError` with a clear message.

`format_expected(assignment_type, number, question, part, course_code)` returns the correctly formatted filename string, used in notifications.

`NamingSchemaError(Exception)` with a `user_message` property that gives a plain English explanation suitable for a terminal or Telegram message.

`hooks/commit-msg.sample`

A bash git hook script. Users copy this to `.git/hooks/pre-commit` in each course repo (the installer does this automatically, see Feature 4). On each commit it reads the list of staged files, calls a small Python helper `scripts/validate_hook.py` (also write this file) passing the staged filenames as arguments, exits 1 with the error messages printed if any file fails validation, and exits 0 otherwise. The part companion check described above runs here: if a staged file has part1 in the name, the hook checks whether a file with the same prefix but part2 is also staged or already exists in the repo, and warns if neither is true.

`scripts/validate_hook.py`: a standalone script that takes filenames as argv, loads the config from a path stored in a `.mimir` file in the repo root (written by the installer), and calls `naming_schema.validate_filename` on each. Prints errors and warnings clearly. Exit code 1 if any errors, 0 otherwise with warnings printed as non blocking output.

---

## Feature 3: smart notifications

Add to `core/notifier.py` (append only, do not change existing functions):

`send_assignment_reminder(assignment, course, cfg)`: builds and sends a Telegram message in this format:

```
{course_code} — {assignment_type} {number} due in {days} day(s)

Upload to: {branch}/{folder}/{assignment_type}{number}/
Files should be named: {type}{number}_q{n}_part{n}_{course_code}

Git commands:
  git checkout {branch}
  git add {folder}/{assignment_type}{number}/
  git commit -m "{assignment_type}{number} complete"
  git push origin {branch}
```

The branch and folder are derived from the assignment type using the `repo_manager.default_branches` and `default_folders` mapping in config. Days remaining is floored at 0. If days remaining is 0 the message opens with a critical marker instead of a warning marker.

Add a new scheduled job to `core/main.py`: `notifier.check_assignment_reminders(cfg)` daily at 08:00. It reads all assignments with status pending or open, due within 3 days, and where a reminder has not been sent today (track with a new boolean column `reminder_sent_today` on the assignments table, reset to false each morning by the job before sending). Calls `send_assignment_reminder` for each. Add `reminder_sent_today BOOLEAN DEFAULT false` to the assignments table in schema.sql.

---

## Feature 4: installer and updater

`core/installer.py`

`run_install(cfg_path=None)`: interactive installer that runs when `scripts/install.sh` is executed on a fresh clone. Steps in order:

1. Check Python version is 3.10 or higher, check git is available, check MySQL is reachable (attempt a connection with the credentials already in config if a config exists, or prompt for them).
2. Create the venv at `venv/` if it does not exist, install `requirements.txt` into it.
3. If `config/config.yaml` does not exist, copy `config/config.example.yaml` to `config/config.yaml` and open it in `$EDITOR` or nano as a fallback, prompting the user to fill in each required field. After the editor closes, call `validate_config` and if it fails print the missing fields and re-open the editor.
4. Apply the schema with `db/db.py --init`.
5. For each mapped course repo found in the courses table (or none on a fresh install, in which case skip), clone or locate the repo locally and install the pre commit hook by copying `hooks/commit-msg.sample` to `.git/hooks/pre-commit` and writing a `.mimir` file to the repo root containing the absolute path to `config/config.yaml`. Skip repos already hooked.
6. Copy `scripts/athena.service` to `/etc/systemd/system/mimir.service` if running as root, run `systemctl daemon-reload` and `systemctl enable mimir`, otherwise print the manual steps.
7. Send a Telegram test message "Mimir installed successfully" and confirm it arrived by prompting the user.
8. Print a summary of what was done and what still needs manual action.

`run_update(cfg_path=None)`: non interactive updater for pushing new versions. Steps:

1. Read the current config into memory.
2. `git pull origin main`.
3. Install any new requirements with `pip install -r requirements.txt` into the existing venv.
4. Run any new schema migrations: compare the current table list against the schema and run only the missing CREATE TABLE statements and any ALTER statements listed in a new `db/migrations/` folder as sequentially numbered `.sql` files (e.g. `001_add_reminder_sent_today.sql`). Apply only migrations not yet recorded in a new `schema_migrations` table (id, filename, applied_at).
5. Restart the Mimir service if systemd is available, otherwise print the manual restart command.
6. Send a Telegram message "Mimir updated successfully".

Add `db/migrations/001_add_reminder_sent_today.sql` as the first migration:

```sql
ALTER TABLE assignments ADD COLUMN IF NOT EXISTS reminder_sent_today BOOLEAN DEFAULT false;
```

Add `schema_migrations` table to `schema.sql`:

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
  id INT AUTO_INCREMENT PRIMARY KEY,
  filename VARCHAR(255) UNIQUE NOT NULL,
  applied_at DATETIME NOT NULL
);
```

`scripts/install.sh`: activates the venv (creating it first if needed), then runs `python3 core/installer.py --install`. Resolves the repo root from the script location so it works from any working directory.

`scripts/update.sh`: activates the venv, then runs `python3 core/installer.py --update`. Same root resolution.

---

## GUI additions

Add to `gui/app.py`:

`GET /repos`: lists all courses with their repo name, mapped status, branch list, and a link to the GitHub repo. Shows an auto create button for unmapped courses if `repo_manager.auto_create` is false (manual trigger).

`POST /repos/{course_id}/create`: manually triggers `repo_manager.create_course_repo` for one course. Returns the result as a flash message.

Add a `repos.html` template under `gui/templates/`: one row per course, repo URL as a link, branches as tags, mapped status, last commit timestamp from the most recent `github_commits` row for that course, and the manual create button for unmapped ones.

---

## Config additions summary

Add to `config/config.yaml` and `config/config.example.yaml`:

```yaml
repo_manager:
  auto_create: true
  default_branches: [main, hw, labs, tests, quizzes, projects, readings]
  default_folders: [HW, Labs, Tests, Quizzes, Projects, Readings]
  course_code_pattern: "[A-Z]+\\d+"

notifications:
  assignment_reminder_days: 3
```

---

## Finish

`python -m py_compile` every Python file. `bash -n` both shell scripts. List schema additions and product decisions only.
