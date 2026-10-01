# Mimir setup

Mimir uses one fixed port, 5000. The Oura redirect address is registered with that port, so do not change `supervisor.gui_port` unless you also register a new redirect address with Oura and update `oura.redirect_uri`. Always open the GUI as `http://localhost:5000` on the computer that runs Mimir.

## 1. Gather these first

| What | Where it comes from | Config key |
|---|---|---|
| iCal feed URL | Canvas, Calendar, Calendar Feed. It contains a secret, treat it like a password. | `ical.feed_url` |
| GitHub token and username | GitHub, Settings, Developer settings, personal access token with repo scope | `github.pat`, `github.username` |
| Oura client id and secret | Oura developer portal, after registering an app (step 4) | `oura.client_id`, `oura.client_secret` |
| Anthropic API key | console.anthropic.com | `anthropic.api_key` |
| Telegram bot token and chat id | Message @BotFather to make a bot, send it /start, then read getUpdates for the chat id | `telegram.bot_token`, `telegram.chat_id` |
| MySQL database | Step 2 | `mysql.*` |
| Optional | SMTP login for emails, DeepSeek key, AWS API url and token | `email.*`, `deepseek.*`, `aws.*` |

## 2. MySQL

Mimir works with MySQL 8 and MariaDB 10.6 or newer.

```sql
CREATE DATABASE mimir CHARACTER SET utf8mb4;
CREATE USER 'mimir'@'127.0.0.1' IDENTIFIED BY 'choose-a-password';
GRANT ALL ON mimir.* TO 'mimir'@'127.0.0.1';
```

Check what exists first with `SHOW DATABASES;` and `SELECT user,host FROM mysql.user;`. If your database is still named `athena`, either keep that name in `mysql.db` or create `mimir` fresh.

## 3. Install

```bash
python3 --version        # 3.11 or newer
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
cp config/config.example.yaml config/config.yaml
```

Fill in `config/config.yaml`. Keep `supervisor.gui_port: 5000` and `oura.redirect_uri: "http://localhost:5000/oauth/oura/callback"`. Keep `repo_manager.auto_create: false`. Set `privacy.identity` and `flask.secret_key`.

```bash
python -m db.db --init
python -m db.db --check
```

Running `--init` a second time is safe.

## 4. Register the Oura app

Do this before the first launch, because Oura will not give you a client id and secret until the app exists.

1. Sign in at cloud.ouraring.com and create an application.
2. Redirect URI, exactly: `http://localhost:5000/oauth/oura/callback`
3. Privacy policy URL: `https://lucaguiga.com/privacy.html`
4. Terms of use URL: `https://lucaguiga.com/terms.html`
5. Copy the client id and secret into `config/config.yaml` under `oura`.

The redirect URI must match character for character: scheme, host, port, and path.

## 5. Start Mimir

```bash
scripts/startup.sh
```

Check `logs/startup.log`, `logs/main.out`, `logs/poller.out`, and `logs/gui.out`. Stop with `scripts/shutdown.sh`.

## 6. Connect Oura

1. On the computer running Mimir, open `http://localhost:5000/setup/oura`. Do not use a LAN address or computer name. Oura sends your browser back to localhost, so any other address fails.
2. Click Connect Oura, approve on Oura's page, and you land back on the same page showing Connected. The first data pull starts at once.
3. If Oura access is ever revoked or expires, you get one Telegram message and the page shows Reconnect.

Only this computer can start or finish the connection. Requests from other machines on your network are refused.

## 7. Pick your courses

Open `http://localhost:5000/setup`, then fetch, select, preview, and create. Do this before the iCal poller has run for long, or it logs a warning for every event of an unselected course.

## 8. Optional pieces

- **Canvas scraper:** set `scraper.enabled: true`, run `playwright install chromium`, then `python pollers/canvas_scraper.py --login` and approve the Duo push.
- **DeepSeek page reading:** set `deepseek.enabled`, `deepseek.api_key`, and fill `privacy.identity`. Check the name swap with `python tests/scrape_identity_check.py` before relying on it.
- **AWS dashboard:** see the README.
- **Start at boot:** edit `scripts/mimir.service` for your user and paths, then follow the commands in its header.

## 9. Check that it works

- The poller strip at the top of the GUI shows iCal, GitHub, and Oura as running.
- `/setup/oura` shows Connected and the permissions granted.
- Oura tests, which need a throwaway database and never touch your real one:

```bash
MIMIR_TEST_DB=mimir_test MIMIR_TEST_USER=root MIMIR_TEST_PASSWORD=... python -m unittest tests.test_oura_auth -v
```

## If something goes wrong

- **Connect Oura is greyed out:** the page says why. Usually the page was opened by LAN address instead of localhost, or the port is not 5000.
- **Oura says the redirect does not match:** compare the registered address with `oura.redirect_uri`, character by character.
- **Telegram says Oura is disconnected:** open `/setup/oura` and click Reconnect.
- **Machine was off for weeks:** the Oura refresh token may have expired. Reconnect.
