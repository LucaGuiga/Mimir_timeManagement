# Mimir

Mimir is a locally hosted academic management platform. It reads your Canvas iCal feed, and polls GitHub and Oura, keeps everything in MySQL, computes a daily stress ratio and schedule, sends morning and evening emails, and pushes snapshots to a small FastAPI app on AWS that serves a React dashboard.

## Process model

1. `core/main.py` is the only process you launch. It supervises the poller and the GUI as child processes and restarts them if they die.
2. `pollers/poller.py` polls the iCal feed and GitHub on their intervals and Oura four times a day. It writes only to MySQL.
3. `core/main.py` reads MySQL and writes derived tables: stress scores, schedule changes, professor profiles, hot zones, no commit rows.
4. `gui/app.py` is the Flask GUI on the local network for mapping courses, editing the schedule, presets, and on demand syllabus parsing.
5. `core/aws_push.py` posts JSON snapshots to the AWS FastAPI app every minute; that app serves the dashboard from DynamoDB. AWS holds no MySQL.

## Local machine setup

1. Install Python 3.11 or newer and MySQL 8 (or MariaDB 10.6 or newer). Create a database and user:
   ```sql
   CREATE DATABASE mimir CHARACTER SET utf8mb4;
   CREATE USER 'mimir'@'127.0.0.1' IDENTIFIED BY 'choose-a-password';
   GRANT ALL ON mimir.* TO 'mimir'@'127.0.0.1';
   ```
2. Install dependencies:
   ```bash
   python3 -m venv venv && . venv/bin/activate
   pip install -r requirements.txt
   ```
3. Fill the config:
   ```bash
   cp config/config.example.yaml config/config.yaml
   ```
   Every key has a comment above it. The tokens (`ical.feed_url`, `github.pat`, `telegram.bot_token`), `telegram.chat_id`, and the whole `mysql` block are required. `aws.api_url` and `aws.api_token` point at the AWS app below. `config/config.yaml` is gitignored. There is no Canvas API integration; courses and assignments come only from the iCal feed. After first start, open `/setup` in the GUI to pick your courses and create their folders in the shared `coursework` repo (one folder per course, `Fall-2026/ECE129/HW/...`; see SETUP.md section 7).
4. Apply the schema and check connectivity:
   ```bash
   python -m db.db --init
   python -m db.db --check
   ```
5. Start and stop by hand:
   ```bash
   scripts/startup.sh
   scripts/shutdown.sh
   ```
   `logs/startup.log` records every start and stop with PIDs; `logs/main.out`, `logs/poller.out`, and `logs/gui.out` hold process output; `logs/fallback.log` catches errors that could not reach MySQL.
6. Install the systemd unit so it starts at boot. Edit `User`, `WorkingDirectory`, and the two script paths in `scripts/mimir.service` first, then follow the commands in its header comment.

Optional scraper: set `scraper.enabled: true`, run `playwright install chromium`, then `python -m pollers.canvas_scraper --login` (approve the Duo push). To let DeepSeek read pages when the built-in selectors find nothing, also set `deepseek.enabled`, `deepseek.api_key`, and fill `privacy.identity` (your name, emails, student id, usernames). DeepSeek stays off while `privacy.identity` is empty. Check the swap works with `python tests/scrape_identity_check.py`.

The schema and migrations run on MariaDB 10.6+ and MySQL 8 (the MariaDB-only `IF NOT EXISTS` clauses are rewritten automatically).

### Oura

Mimir reads Oura through OAuth2, not a personal access token (Oura retired those). The redirect address is served by the local GUI, so Oura tokens and health data stay on your machine. Tokens are stored in the local MySQL database (table `oura_auth`), never in `config.yaml`, never in logs, and never sent to the AWS app. One Oura account is supported.

1. Register an app at cloud.ouraring.com with redirect URI `http://localhost:5000/oauth/oura/callback`, privacy policy `https://lucaguiga.com/privacy.html`, and terms `https://lucaguiga.com/terms.html`.
2. Put the client id and secret in `config/config.yaml` under `oura`.
3. Start Mimir, then open `http://localhost:5000/setup/oura` in a browser on the same computer and click Connect Oura.

The GUI must run on port 5000 and be opened as `localhost`, because the redirect address has to match what is registered exactly. Full steps are in [SETUP.md](SETUP.md).

The GUI listens on `supervisor.gui_host:gui_port` (default `0.0.0.0:5000`).

## AWS setup

The API and the dashboard run on AWS Lambda (python3.12) behind an API Gateway HTTP API at https://mimir.lucaguiga.com. Storage is the DynamoDB table `luca-mimir`. The Lambda runs the repo's own `api/fastapi_app.py` through Mangum, so there is no server to patch.

1. Build the package: `api/build_lambda.sh`. It builds the dashboard and writes `dist/mimir-lambda.zip` including `frontend/dist`. Upload that zip to the Lambda.
2. Set the Lambda environment variables:
   - `MIMIR_API_TOKEN`: the machine token, equal to `aws.api_token` in `config/config.yaml`.
   - `MIMIR_DYNAMO_TABLE`: `luca-mimir`.
   - `MIMIR_SESSION_DAYS`: how long a dashboard login lasts.
   - `MIMIR_LOGIN_MAX_FAILURES` and `MIMIR_LOGIN_WINDOW_MINUTES`: failed password attempts allowed per window before the login is rate limited.
   - `MIMIR_FRONTEND_DIST`: where the built dashboard sits inside the package.
3. Set `aws.api_url` to `https://mimir.lucaguiga.com` in `config/config.yaml`.
4. Add a dashboard password: `python -m core.dashboard_passwords add <label>` (see SETUP.md).

For development, `uvicorn api.fastapi_app:app` with SQLite still works locally.

### Token visibility

The dashboard bundle no longer contains a secret. The dashboard sits behind a password page, and the API only answers dashboard requests that carry a session cookie from a successful login. The machine token lives only in `config/config.yaml` and the Lambda environment.
