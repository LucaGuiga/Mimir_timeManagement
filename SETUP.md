# Mimir setup

Mimir's GUI starts on port 5000 by default, with nothing to configure. The Oura redirect address is registered with that port, so do not change `supervisor.gui_port` unless you also register a new redirect address with Oura and update `oura.redirect_uri`. If another program already uses port 5000, the GUI cannot start, so check first (step 5). Always open the GUI as `http://localhost:5000` on the computer that runs Mimir.

## 1. Gather these first

Required:

- **iCal feed URL**: in Canvas, open Calendar, then Calendar Feed. The link contains a secret, so treat it like a password. Config key: `ical.feed_url`.
- **GitHub token and username**: GitHub, Settings, Developer settings, personal access token with repo scope. Config keys: `github.pat` and `github.username`.
- **Telegram bot token and chat id**: message @BotFather to make a bot, send the bot /start, then read getUpdates for the chat id. Config keys: `telegram.bot_token` and `telegram.chat_id`.
- **MySQL database**: made in step 2. Config keys: the `mysql` block.

Needed for Oura (you get these in step 4):

- **Oura client id and secret**. Config keys: `oura.client_id` and `oura.client_secret`.

Optional:

- SMTP login for the morning and evening emails (`email.*`).
- DeepSeek key for page reading and syllabus parsing (`deepseek.*`). Mimir tracks its token spend, warns on Telegram if a billing period is on track to pass `deepseek.monthly_cap_usd` (default $10), and shows the billing period total in both emails. Set `deepseek.billing_day` to the day your period starts and the `deepseek.price_*` keys to DeepSeek's current prices.
- AWS API url and token for the dashboard (`aws.*`).

## 2. MySQL

Mimir works with MySQL 8 and MariaDB 10.6 or newer.

```sql
CREATE DATABASE mimir CHARACTER SET utf8mb4;
CREATE USER 'mimir'@'127.0.0.1' IDENTIFIED BY 'choose-a-password';
GRANT ALL ON mimir.* TO 'mimir'@'127.0.0.1';
```

Check what exists first with `SHOW DATABASES;` and `SELECT user,host FROM mysql.user;`. If your database is still named `athena`, either keep that name in `mysql.db` or create `mimir` fresh.

### If you already created a database

Use this to confirm what you made matches what Mimir expects. Run the commands in order and stop at the first one that fails.

1. **The server is running and which one it is.**

   ```bash
   mysql --version
   sudo systemctl status mysql        # or: sudo systemctl status mariadb
   ```

2. **The database and the user exist.** Log in as the admin user.

   ```bash
   mysql -u root -p -e "SHOW DATABASES; SELECT user,host FROM mysql.user;"
   ```

   You should see your database name (for example `mimir`) and your Mimir user. The `host` column matters: Mimir connects over TCP to `127.0.0.1`, so the user's host must be `127.0.0.1` or `%`. A user created as `'mimir'@'localhost'` is refused. If yours is `localhost`, create the `127.0.0.1` one with the commands above.

3. **The user has rights on the database.**

   ```bash
   mysql -u root -p -e "SHOW GRANTS FOR 'mimir'@'127.0.0.1';"
   ```

   It should list `ALL PRIVILEGES ON mimir.*` (use your own database and user names).

4. **The login works the way Mimir will use it.** This catches a wrong password, wrong host, or wrong port.

   ```bash
   mysql -h 127.0.0.1 -P 3306 -u mimir -p mimir -e "SELECT 1"
   ```

   If your server listens on another port, find it with `mysql -u root -p -e "SHOW VARIABLES LIKE 'port'"`.

5. **The values in `config/config.yaml` match what you just tested.** In the `mysql` block: `host` is `127.0.0.1`, `port` is the port from step 4, `user` and `password` are what you logged in with, and `db` is the database name. Nothing else in Mimir reads these, so a typo here is the usual cause of a startup failure.

6. **Mimir can connect.** From the repo folder with the virtual environment active:

   ```bash
   python -m db.db --check
   ```

   Success looks like `connected to mimir (MySQL 8.0..., N tables)`. Failures read like this:

   - `1045 Access denied for user`: wrong user or password, or the user's host is wrong (step 2).
   - `1049 Unknown database`: the `db` name does not exist (step 2).
   - `2003 Can't connect to MySQL server`: the server is not running, or `host` or `port` is wrong (steps 1 and 4).

7. **Bring the tables up to date.** This is safe on an empty database, a new one, or one from an older version of the project (including the old `athena` database), and safe to repeat.

   ```bash
   python -m db.db --init
   python -c "from core.installer import apply_pending_migrations; print(apply_pending_migrations())"
   python -m db.db --check
   ```

   The second command prints the migration files it applied, or an empty list if nothing was left. The last command now reports the table count. Keep the database name your `mysql.db` points to; there is no need to rename an old `athena` database.

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

First make sure nothing else is using port 5000. On Ubuntu this command should print nothing:

```bash
sudo ss -ltnp | grep ':5000'
```

If it prints a line, the last column names the program using the port (a Docker registry and other web apps commonly use 5000). Stop or reconfigure that program, then continue.

```bash
scripts/startup.sh
```

Once it is running, `sudo ss -ltnp | grep ':5000'` should show a `python` process, and `http://localhost:5000/setup/oura` should open.

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
- **Syllabi:** with the scraper and DeepSeek on, Mimir finds each course's syllabus on its Canvas pages and in syllabus PDFs and parses it into JSON about once a day, only when it changed. Scanned image PDFs are skipped because there is no text to read. You can also upload or paste a syllabus on the Syllabus page.
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
