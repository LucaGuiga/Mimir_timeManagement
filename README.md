# Athena

Athena is a locally hosted academic management platform. It polls Canvas, GitHub, and Oura, keeps everything in MySQL, computes a daily stress ratio and schedule, sends morning and evening emails, and pushes snapshots to a small FastAPI app on AWS that serves a React dashboard.

## Process model

1. `core/main.py` is the only process you launch. It supervises the poller and the GUI as child processes and restarts them if they die.
2. `pollers/poller.py` polls Canvas and GitHub every minute and Oura four times a day. It writes only to MySQL.
3. `core/main.py` reads MySQL and writes derived tables: stress scores, schedule changes, professor profiles, hot zones, no commit rows.
4. `gui/app.py` is the Flask GUI on the local network for mapping courses, editing the schedule, presets, and on demand syllabus parsing.
5. `core/aws_push.py` posts JSON snapshots to the AWS FastAPI app every minute; that app serves the dashboard from SQLite. AWS holds no MySQL.

## Local machine setup

1. Install Python 3.11 or newer and MySQL 8 (or MariaDB 10.6 or newer). Create a database and user:
   ```sql
   CREATE DATABASE athena CHARACTER SET utf8mb4;
   CREATE USER 'athena'@'127.0.0.1' IDENTIFIED BY 'choose-a-password';
   GRANT ALL ON athena.* TO 'athena'@'127.0.0.1';
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
   Every key has a comment above it. The five tokens (`canvas.token`, `github.pat`, `oura.pat`, `anthropic.api_key`, `telegram.bot_token`), `telegram.chat_id`, and the whole `mysql` block are required. `aws.api_url` and `aws.api_token` point at the AWS app below. `config/config.yaml` is gitignored.
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
6. Install the systemd unit so it starts at boot. Edit `User`, `WorkingDirectory`, and the two script paths in `scripts/athena.service` first, then follow the commands in its header comment.

The GUI listens on `supervisor.gui_host:gui_port` (default `0.0.0.0:5000`).

## AWS setup

1. Copy the `api/` and `frontend/` directories to the box (for example under `/opt/athena`).
2. Install the API:
   ```bash
   python3 -m venv /opt/athena/venv && /opt/athena/venv/bin/pip install -r /opt/athena/api/requirements.txt
   cp /opt/athena/api/config.example.yaml /opt/athena/api/config.yaml
   ```
   Set `api_token` to the same value as `aws.api_token` in the local config. `api/config.yaml` is gitignored.
3. Build the dashboard:
   ```bash
   cd /opt/athena/frontend
   cp src/config.example.js src/config.js   # set API_TOKEN; leave API_BASE empty when served by the API
   npm install && npm run build
   ```
   The API serves `frontend/dist` at `/` once it exists.
4. Install the unit: edit `User`, `Group`, `WorkingDirectory`, and the venv path in `api/athena_api.service`, then follow the commands in its header comment. `GET /health` needs no token and reports the oldest snapshot time.

### Token visibility

The dashboard bundle contains `API_TOKEN`, so anyone who loads the page can read it and call the API. That is acceptable for a personal dashboard on an obscure subdomain. If that ever changes, put basic auth on the reverse proxy in front of uvicorn.
