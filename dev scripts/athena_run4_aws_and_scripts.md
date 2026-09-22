# Athena scaffold, run 4 of 4: AWS API, React dashboard, scripts, systemd

**Model: Sonnet 5 (`claude-sonnet-5`).** The payload contract is already fixed by `core/aws_push.py`; this is implementation against it.

---

You are scaffolding run 4 of 4 of Athena. Runs 1 to 3 exist. Before writing anything read in full: `core/aws_push.py` (the snapshot keys and shapes are the contract; do not change them), `core/stress.py` (`band` thresholds), `core/main.py`, `core/supervisor.py`, `core/config.py`, `db/db.py`, `config/config.yaml`, `.gitignore`.

This run creates exactly: `api/fastapi_app.py`, `api/store.py`, `api/config.yaml`, `api/requirements.txt`, `api/athena_api.service`, the `frontend/` app (`package.json`, `vite.config.js`, `index.html`, `src/main.jsx`, `src/App.jsx`, `src/api.js`, `src/config.example.js`, `src/components/StressMeter.jsx`, `AssignmentList.jsx`, `DailySchedule.jsx`, `ProgressTracker.jsx`, `ErrorBanner.jsx`, `PollMetrics.jsx`), `scripts/startup.sh`, `scripts/shutdown.sh`, `scripts/athena.service`, and `README.md`. Write every file in full. No placeholders. Fill implementation gaps yourself; only surface product decisions.

## AWS side

The AWS box runs FastAPI with uvicorn and serves the built React bundle from the same process. It holds no MySQL; it keeps the latest snapshot per key in SQLite.

`api/config.yaml`: api_token, host, port, sqlite_path, allowed_origins (list). No environment variables. Loaded by a small loader inside `fastapi_app.py`.

`api/store.py`: SQLite table `snapshots(key TEXT PRIMARY KEY, payload TEXT, updated_at TEXT)`. `set_many(dict)`, `get(key)` returning the parsed payload and updated_at or None, `get_all_updated_at()`.

`api/fastapi_app.py`: bearer token dependency on every route, constant time comparison against `api_token`. `POST /ingest` accepts `{"snapshots": {key: payload}}`, validates that every key is one of the eight known keys, stores them, returns the keys written. GET endpoints, each returning the stored payload plus `updated_at` and `stale: true` when updated_at is older than five minutes:

- `/stress/today` from `stress_today`
- `/stress/history?days=30` slices `stress_history`
- `/assignments/upcoming` from `assignments_upcoming`
- `/schedule/today` from `schedule_today`
- `/commits/recent` from `commits_recent`
- `/oura/recent?days=7` slices `oura_recent`
- `/errors/active` from `errors_active`
- `/poll_metrics/summary` from `poll_metrics_summary`
- `/health` without auth returning ok and the oldest updated_at across keys

Mount `frontend/dist` as static at `/` after the API routes. CORS from `allowed_origins`.

`api/requirements.txt`: fastapi, uvicorn, pyyaml, pinned major versions.

`api/athena_api.service`: systemd unit running uvicorn as a non root user, Restart=always, WorkingDirectory set to the api directory, After=network.target. Include install and enable commands as comments at the top.

## React dashboard

Vite plus React, JavaScript not TypeScript, no UI framework, plain CSS in one file. `src/config.js` is gitignored and holds `API_BASE` and `API_TOKEN`; ship `src/config.example.js`. `src/api.js` wraps fetch with the bearer header and a 10 second AbortController timeout; every component polls its endpoint every 60 seconds and shows the `stale` flag as a small grey label. Note in the README that the token in the bundle is visible to anyone who loads the page, which is acceptable for a personal dashboard on an obscure subdomain, and that basic auth at the reverse proxy is the fix if that ever changes.

`StressMeter.jsx`: circular SVG gauge of the ratio, scale 0 to 1.5 with the needle pinned at the end above 1.5 and the number shown unclamped to two decimals. Colour green below 0.8, amber 0.8 to 1.0, red above 1.0. Below it a small sparkline of `stress_history` for 30 days.

`AssignmentList.jsx`: one card per upcoming assignment: course name, title, type as a tag, due date, days remaining, status, commit count and last commit time; a card with zero commits and fewer than three days remaining gets a red left border.

`DailySchedule.jsx`: vertical timeline of today's blocks from 06:00 to midnight; class and fixed blocks in one colour, clubs in a second, flexible in a third, travel and chores in grey; skipped blocks struck through with the reason on hover.

`ProgressTracker.jsx`: per course accordion; per assignment a row of the last 14 days where each day shows the summed size delta as a small bar and a no commit day as a hollow marker; totals per assignment.

`ErrorBanner.jsx`: sticky top banner, hidden when critical_count is zero, otherwise count plus the latest message.

`PollMetrics.jsx`: three stat cards (Canvas, GitHub, Oura) with last poll time, average response time in microseconds, and state; a state other than running turns the card amber.

`App.jsx`: banner on top, then a two column layout on wide screens and single column on phones: StressMeter and PollMetrics left, AssignmentList and DailySchedule right, ProgressTracker full width below.

## Local scripts and systemd

`scripts/startup.sh`: resolve the repo root from the script location; confirm MySQL is running (systemctl is active or a successful `db/db.py --check`, use whichever exists); confirm the five tokens in `config/config.yaml` are non empty by calling `core.config.validate_config` through a one line Python invocation; refuse to start if `run/main.pid` points at a live process; start `core/main.py` with nohup in the background with stdout and stderr appended to `logs/main.out`; main starts the poller and GUI itself; append the timestamp and the main PID to `logs/startup.log`, then after five seconds append the poller and GUI PIDs read from their pidfiles.

`scripts/shutdown.sh`: read `run/main.pid`, send SIGTERM to main only (main cascades to its children and waits for their current cycles), wait up to 45 seconds polling for the PID to exit, SIGKILL main and any PIDs left in `run/*.pid` if it has not, remove stale pidfiles, append the shutdown timestamp to `logs/startup.log`.

`scripts/athena.service`: Type=forking, ExecStart the startup script, ExecStop the shutdown script, PIDFile `run/main.pid`, After=mysql.service and network online target, Requires=mysql.service, Restart=on failure, User set to the operating user, WorkingDirectory the repo root. Install and enable commands as comments at the top.

`README.md`: how to fill config.yaml, apply the schema with `db/db.py --init`, install both requirements files, build the frontend, install both systemd units, and the process model in five lines (main supervises poller and GUI; poller writes MySQL; main reads MySQL and pushes to AWS; AWS serves the dashboard).

## Finish

`python -m py_compile` on the API file, `npm install` and `npm run build` in `frontend/` if node is available, `bash -n` on both scripts. List product decisions only.
