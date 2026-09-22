# Athena scaffold, run 3 of 4: Flask GUI and syllabus parser

**Model: Sonnet 5 (`claude-sonnet-5`).** Templates and CRUD pages against a schema and modules that already exist. No cross module design left to do.

---

You are scaffolding run 3 of 4 of Athena. Runs 1 and 2 exist. Before writing anything read in full: `db/schema.sql`, `config/config.yaml`, `core/config.py`, `db/db.py`, `logs/error_handler.py`, `core/notifier.py`, `core/stress.py`, `core/schedule_builder.py`, `core/professor_profiler.py`, `core/progress.py`, `pollers/canvas.py` (for `find_syllabus_sources`), `core/supervisor.py`. Use their exact signatures. Do not modify them.

This run creates exactly: `core/syllabus_parser.py`, `gui/app.py`, `gui/static/style.css`, and the templates `base.html`, `index.html`, `course_mapping.html`, `professor_profiles.html`, `error_log.html`, `hot_zones.html`, `schedule.html`, `presets.html`, `syllabus_trigger.html` under `gui/templates/`. Every page fully functional, not a wireframe. Plain HTML forms and server side rendering with Jinja, one small vanilla JS file inlined in base.html for confirm dialogs and auto refresh of the index page every 60 seconds. No frontend build step. Fill implementation gaps yourself; only surface product decisions.

## gui/app.py

Flask app factory `create_app(cfg)`; `if __name__ == "__main__"` reads `supervisor.gui_host` and `gui_port` and runs it. Writes `run/gui.pid` and a process_state row on start. Every route wrapped so any exception writes to error_log through `log_error` with script_name gui and renders an error flash rather than a 500 page. All reads and writes through `db/db.py` with parameterised queries. Routes:

- `GET /` index
- `GET /courses`, `POST /courses/map` (course_id, repo_name, repo_path_prefix, branches as comma separated text)
- `GET /professors`
- `GET /errors` with query params severity, script_name, date_from, date_to, page; `POST /errors/{id}/acknowledge`; `POST /errors/acknowledge_all_critical`
- `GET /hot_zones`, `POST /hot_zones/add` (professor_id, day_of_week, hour_start, hour_end, source manual), `POST /hot_zones/{id}/delete` (manual rows only; profiler rows show a note that they are recomputed automatically)
- `GET /schedule` with optional date param, `POST /schedule/{block_id}/skip` (reason text), `POST /schedule/{block_id}/unskip`, `POST /schedule/add` (date, time_category, label, start_time, end_time, priority, moveable), `POST /schedule/{block_id}/delete`
- `GET /presets`, `POST /presets` writes `difficulty_presets` and `time_presets` back through `core.config.save_config` so comments survive, then invalidates the config cache
- `GET /syllabus`, `POST /syllabus/{course_id}/find` (calls `find_syllabus_sources`, stores the result in the session), `POST /syllabus/{course_id}/parse` (source choice: body or a file id), `POST /syllabus/{parsed_id}/review` (marks human_reviewed, accepts edited predicted dates per assignment)
- `POST /control/recalc_stress` calls `stress.calculate_and_store(today)`
- `POST /control/restart_poller` reads `run/poller.pid` and sends SIGTERM to that process group; the supervisor restarts it
- `POST /control/rebuild_schedule` calls `schedule_builder.run_for_today`

## Templates

`base.html`: nav across all pages, flash messages, a sticky red banner at the top on every page when `unacknowledged_critical_count()` is above zero showing the count and the latest message with a link to the error log, and a poller status strip showing state and last cycle time for canvas, github, oura from `poller_status`.

`index.html`: today's stress ratio to two decimals with a colour coded indicator using `stress.band` (green, amber, red), the component breakdown (T_available, deadline term, sleep penalty, hours slept) in a small table, today's schedule blocks in chronological order with skipped rows struck through, assignments due in the next seven days with course, type, due date and days remaining, last poll time and average response time in microseconds over the last hour for each API from poll_metrics, and the three control buttons (recalculate stress, rebuild schedule, restart poller) with confirm dialogs.

`course_mapping.html`: every course from `courses`. Unmapped courses show a form with repo name, path prefix, and branches; mapped courses show the current mapping with a remap form. Repo names are free text with a datalist of repo names already used.

`professor_profiles.html`: one card per professor with their courses, adherence as a percentage, average early and late post days, typical weekdays as names, typical hour window, observation count, last updated.

`error_log.html`: paginated table (50 per page) with filters for severity, script name (datalist of distinct script names), and date range; acknowledge button per row; acknowledge all critical button; unacknowledged critical count in the header.

`hot_zones.html`: per professor, the current windows with source shown; add form; delete for manual rows.

`schedule.html`: date picker defaulting to today; blocks with label, time_category, start, end, priority, moveable, skipped and reason, original start time when shifted; skip with reason and unskip; add block form; delete.

`presets.html`: two forms, difficulty presets (one number input 1 to 5 per assignment type) and time presets (sleep_target_hours, commute, breakfast, lunch, dinner, chores minutes, alpha). Show the estimated hours e^(x/1.248) next to each difficulty as a live computed value with a few lines of JS.

`syllabus_trigger.html`: every active course with whether a syllabus_parsed row exists for the current quarter, its parse cost, validated and human_reviewed flags, and budget used versus `per_class_claude_budget_usd` for that course. Find button, then a source picker (syllabus body or one of the found files), then the parse button. After parsing, a review section listing each parsed item with its matched assignment (or unmatched) and editable predicted open and due dates.

`style.css`: simple, readable, no framework, works on a phone.

## core/syllabus_parser.py

`fetch_source(cfg, course_id, source)`: source is `body` or a Canvas file id; for body strip HTML to text; for a file download it with the Canvas token and extract text with pypdf for PDFs or decode for text and HTML, else raise. `parse(cfg, course_id, raw_text)`: check the budget first (sum of parse_cost_usd for that course this quarter plus a conservative estimate for this call against `per_class_claude_budget_usd`, refuse with a clear message if it would exceed); call the Anthropic API with `anthropic.parse_model` and a system prompt that demands a JSON object only, with shape `{"professor_name": str, "items": [{"title": str, "assignment_type": one of the enum values, "assignment_number": int or null, "predicted_open": "YYYY-MM-DD" or null, "predicted_due": "YYYY-MM-DD" or null, "notes": str}]}`; compute cost from the usage fields and the model's per token prices held in a small dict in this module; validate the JSON against that shape strictly (types, enum values, date formats, dates inside the quarter); on validation failure store the row with validated false and human_reviewed false and return it flagged rather than inserting anything into assignments. On success store validated true, then match items to `assignments` for the course on (assignment_type, assignment_number), falling back to a normalised title match, and write syllabus_predicted_open and syllabus_predicted_due onto matched rows. Unmatched items stay in parsed_json for the review page. Never raise past the caller; return a result dict with status, message, cost, matched and unmatched counts.

## Finish

`python -m py_compile` every Python file, render every template once with the Flask test client against an empty database and confirm no template errors. Add ruamel.yaml and pypdf to requirements.txt if missing. List product decisions only.
