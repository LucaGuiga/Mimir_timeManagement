"""Interactive installer (--install) and non interactive updater (--update).
Project imports are deferred so this file runs on a fresh clone before requirements are installed."""
import glob
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV = os.path.join(ROOT, "venv")
HOOK_MARKER = "Mimir naming schema pre-commit hook"


def _venv_bin(name):
    p = os.path.join(VENV, "bin", name)
    return p if os.path.exists(p) else name


def _run(cmd, check=True, **kw):
    print("  $", " ".join(cmd))
    return subprocess.run(cmd, cwd=kw.pop("cwd", ROOT), check=check, **kw)


def _ask(prompt, default=""):
    v = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return v or default


def _cfg_path(cfg_path=None):
    return os.path.abspath(cfg_path or os.path.join(ROOT, "config", "config.yaml"))


# ------------------------------------------------------------------ hooks
def install_hook(repo_dir, cfg_path):
    """Copies hooks/commit-msg.sample to .git/hooks/pre-commit and writes .mimir. Returns 'installed', 'already', or an error string."""
    git_dir = os.path.join(repo_dir, ".git")
    if not os.path.isdir(git_dir):
        return f"{repo_dir} is not a git repository"
    hooks = os.path.join(git_dir, "hooks")
    os.makedirs(hooks, exist_ok=True)
    target = os.path.join(hooks, "pre-commit")
    with open(os.path.join(repo_dir, ".mimir"), "w", encoding="utf-8") as f:
        f.write(os.path.abspath(cfg_path) + "\n")
    if os.path.exists(target):
        with open(target, encoding="utf-8", errors="replace") as f:
            if HOOK_MARKER in f.read():
                return "already"
        shutil.copy(target, target + ".bak")
    shutil.copy(os.path.join(ROOT, "hooks", "commit-msg.sample"), target)
    os.chmod(target, 0o755)
    return "installed"


# ------------------------------------------------------------- migrations
def migration_files():
    files = glob.glob(os.path.join(ROOT, "db", "migrations", "*.sql"))
    return sorted(files, key=lambda p: (int(re.match(r"(\d+)", os.path.basename(p)).group(1)) if re.match(r"\d+", os.path.basename(p)) else 10**9, p))


def _ensure_migrations_table():
    from db.db import execute_query
    execute_query("CREATE TABLE IF NOT EXISTS schema_migrations (id INT AUTO_INCREMENT PRIMARY KEY, filename VARCHAR(255) UNIQUE NOT NULL, "
                  "applied_at DATETIME NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")


def record_all_migrations():
    """After a fresh --init the schema already carries every migration; record them so --update never re-runs them."""
    from db.db import execute_query
    _ensure_migrations_table()
    for f in migration_files():
        execute_query("INSERT IGNORE INTO schema_migrations (filename, applied_at) VALUES (%s, NOW())", (os.path.basename(f),))


def apply_pending_migrations():
    """Applies unrecorded db/migrations/*.sql in numeric order. Stops at the first failure. Returns (applied, failed)."""
    from db.db import _split_statements, execute_query, fetch_all, get_connection
    from logs.error_handler import log_error
    _ensure_migrations_table()
    done = {r["filename"] for r in fetch_all("SELECT filename FROM schema_migrations")}
    applied = []
    for path in migration_files():
        name = os.path.basename(path)
        if name in done:
            continue
        with open(path, encoding="utf-8") as f:
            statements = _split_statements(f.read())
        try:
            with get_connection() as conn:
                cur = conn.cursor()
                try:
                    for st in statements:
                        cur.execute(st)
                finally:
                    cur.close()
            execute_query("INSERT INTO schema_migrations (filename, applied_at) VALUES (%s, %s)", (name, datetime.now()))
            applied.append(name)
            print(f"  applied {name} ({len(statements)} statements)")
        except Exception as e:
            log_error("installer", type(e).__name__, f"migration {name}", str(e), "critical")
            print(f"  FAILED {name}: {e}")
            return applied, name
    return applied, None


# ---------------------------------------------------------------- install
def run_install(cfg_path=None):
    cfg_path = _cfg_path(cfg_path)
    summary, manual = [], []
    print("== Mimir installer ==")
    # 1. prerequisites
    if sys.version_info < (3, 10):
        print(f"Python 3.10 or newer is required, found {sys.version.split()[0]}"); return 1
    if not shutil.which("git"):
        print("git is not installed"); return 1
    print(f"Python {sys.version.split()[0]} and git found.")
    # 2. venv and requirements
    if not os.path.isdir(VENV):
        _run([sys.executable, "-m", "venv", VENV]); summary.append("created venv/")
    _run([_venv_bin("pip"), "install", "-q", "-r", os.path.join(ROOT, "requirements.txt")]); summary.append("installed requirements")
    if sys.executable != _venv_bin("python") and os.path.exists(_venv_bin("python")):
        # re-exec inside the venv so the project imports below see the freshly installed packages
        os.execv(_venv_bin("python"), [_venv_bin("python"), os.path.abspath(__file__), "--install", "--config", cfg_path])
    sys.path.insert(0, ROOT)
    from core.config import ConfigError, load_config, reload_config, save_config, validate_config
    # 3. config
    example = os.path.join(ROOT, "config", "config.example.yaml")
    if not os.path.exists(cfg_path):
        shutil.copy(example, cfg_path); summary.append("created config/config.yaml from the example")
        print("Fill in every required field: the five tokens, telegram.chat_id, and the mysql block.")
    editor = os.environ.get("EDITOR") or (shutil.which("nano") and "nano") or "vi"
    while True:
        try:
            validate_config(reload_config(cfg_path)); break
        except ConfigError as e:
            print(f"Config incomplete: {e}")
            if _ask("Open the editor now? [Y/n]", "Y").lower().startswith("n"):
                print("Install stopped; fill config/config.yaml and rerun scripts/install.sh"); return 1
            subprocess.run([editor, cfg_path])
    cfg = load_config(cfg_path)
    # 1c. MySQL reachability, prompting for credentials until it works
    while subprocess.run([_venv_bin("python"), "-m", "db.db", "--check"], cwd=ROOT).returncode != 0:
        print("MySQL is not reachable with the configured credentials.")
        if _ask("Enter MySQL credentials now? [Y/n]", "Y").lower().startswith("n"):
            print("Install stopped; fix the mysql block and rerun"); return 1
        mysql = {k: _ask(f"mysql.{k}", str(cfg["mysql"].get(k, ""))) for k in ("host", "port", "user", "password", "db")}
        mysql["port"] = int(mysql["port"])
        save_config({"_path": cfg_path, "mysql": mysql}); cfg = reload_config(cfg_path)
    summary.append("MySQL reachable")
    # 4. schema
    _run([_venv_bin("python"), "-m", "db.db", "--init"]); record_all_migrations(); summary.append("schema applied and migrations recorded")
    # 5. hooks in course repos
    from db.db import fetch_all
    courses = fetch_all("SELECT canvas_course_name, repo_name FROM courses WHERE mapped = TRUE AND repo_name IS NOT NULL")
    if courses:
        base = os.path.expanduser(_ask("Directory that holds (or will hold) your course repo clones", os.path.expanduser("~/courses")))
        os.makedirs(base, exist_ok=True)
        owner = cfg["github"].get("username")
        for c in courses:
            repo_dir = os.path.join(base, c["repo_name"])
            if not os.path.isdir(repo_dir):
                r = subprocess.run(["git", "clone", f"https://github.com/{owner}/{c['repo_name']}.git", repo_dir])
                if r.returncode != 0:
                    manual.append(f"clone {owner}/{c['repo_name']} into {repo_dir} and rerun the installer to add its hook"); continue
            status = install_hook(repo_dir, cfg_path)
            summary.append(f"hook {status} in {repo_dir}")
    else:
        print("No mapped course repos yet; hooks are installed by rerunning the installer once repos exist.")
    # 6. systemd
    unit_src, unit_dst = os.path.join(ROOT, "scripts", "mimir.service"), "/etc/systemd/system/mimir.service"
    if hasattr(os, "geteuid") and os.geteuid() == 0 and shutil.which("systemctl"):
        shutil.copy(unit_src, unit_dst); _run(["systemctl", "daemon-reload"]); _run(["systemctl", "enable", "mimir"])
        summary.append("installed and enabled mimir.service")
    else:
        manual.append(f"sudo cp {unit_src} {unit_dst} && sudo systemctl daemon-reload && sudo systemctl enable --now mimir "
                      "(edit User and WorkingDirectory in the unit first)")
    # 7. telegram
    from core import notifier
    if notifier.send_message("Mimir installed successfully"):
        if _ask("A Telegram test message was sent. Did it arrive? [y/N]", "N").lower().startswith("y"):
            summary.append("Telegram confirmed")
        else:
            manual.append("check telegram.bot_token and telegram.chat_id; the test message was not received")
    else:
        manual.append("Telegram send failed; see logs/fallback.log")
    # 8. summary
    print("\n== Done ==")
    for s in summary:
        print(f"  [x] {s}")
    if manual:
        print("Still needs manual action:")
        for m in manual:
            print(f"  [ ] {m}")
    print("Start with scripts/startup.sh (or systemctl start mimir).")
    return 0


# ----------------------------------------------------------------- update
def run_update(cfg_path=None):
    cfg_path = _cfg_path(cfg_path)
    sys.path.insert(0, ROOT)
    from core.config import load_config
    cfg = load_config(cfg_path)
    print("== Mimir updater ==")
    _run(["git", "pull", "origin", "main"])
    _run([_venv_bin("pip"), "install", "-q", "-r", os.path.join(ROOT, "requirements.txt")])
    applied, failed = apply_pending_migrations()
    if failed:
        print(f"Update stopped: migration {failed} failed; see error_log."); return 1
    print(f"Migrations applied: {', '.join(applied) if applied else 'none pending'}")
    if shutil.which("systemctl") and subprocess.run(["systemctl", "cat", "mimir"], capture_output=True).returncode == 0:
        r = subprocess.run(["systemctl", "restart", "mimir"])
        print("Service restarted." if r.returncode == 0 else "Could not restart the service; run: sudo systemctl restart mimir")
    else:
        print("No systemd unit found; restart by hand: scripts/shutdown.sh && scripts/startup.sh")
    from core import notifier
    notifier.send_message("Mimir updated successfully")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    path = args[args.index("--config") + 1] if "--config" in args else None
    if "--install" in args:
        sys.exit(run_install(path))
    if "--update" in args:
        sys.exit(run_update(path))
    print("usage: python core/installer.py --install | --update [--config path]")
    sys.exit(1)
