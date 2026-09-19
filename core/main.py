"""Athena supervisor process. The only process the startup script launches."""
import os
import signal
import sys
import threading
from datetime import date

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core import aws_push, email_sender, professor_profiler, progress, schedule_builder, stress
from core.config import ConfigError, get, load_config, repo_root, validate_config
from core.supervisor import Supervisor
from db.db import DBError, execute_query, fetch_one
from logs.error_handler import log_error

SCRIPT = "main"
_stop = threading.Event()


def _safe(name, fn):
    def wrapped():
        try:
            fn()
        except Exception as e:
            log_error(SCRIPT, type(e).__name__, name, str(e))
    return wrapped


def _cron(hhmm):
    h, m = (int(x) for x in str(hhmm).split(":"))
    return CronTrigger(hour=h, minute=m)


def _heartbeat(start=False):
    if start:
        execute_query("INSERT INTO process_state (process_name, pid, started_at, last_heartbeat) VALUES ('main',%s,NOW(),NOW()) "
                      "ON DUPLICATE KEY UPDATE pid=VALUES(pid), started_at=NOW(), last_heartbeat=NOW()", (os.getpid(),))
    else:
        execute_query("UPDATE process_state SET last_heartbeat=NOW() WHERE process_name='main'")


def run():
    try:
        cfg = load_config()
        validate_config(cfg)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
    try:
        fetch_one("SELECT 1 AS ok")
    except DBError as e:
        log_error(SCRIPT, "DBError", "startup connectivity", str(e), "critical")
        print(f"mysql unavailable: {e}", file=sys.stderr)
        return 1
    run_dir = os.path.join(repo_root(), get(cfg, "paths.run_dir", "run"))
    os.makedirs(run_dir, exist_ok=True)
    pidfile = os.path.join(run_dir, "main.pid")
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
    _safe("process_state start", lambda: _heartbeat(True))()

    sup = Supervisor(cfg)
    if get(cfg, "supervisor.start_poller", True):
        _safe("start poller", lambda: sup.start_child("poller", [sys.executable, "-m", "pollers.poller"]))()
    if get(cfg, "supervisor.start_gui", True):
        if os.path.exists(os.path.join(repo_root(), "gui", "app.py")):
            _safe("start gui", lambda: sup.start_child("gui", [sys.executable, "-m", "gui.app"]))()
        else:
            log_error(SCRIPT, "MissingFile", "start gui", "gui/app.py does not exist yet; GUI not started")

    sched = BackgroundScheduler(job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 60})
    sched.add_job(_safe("check_children", sup.check_children), IntervalTrigger(seconds=30), id="check_children")
    sched.add_job(_safe("profiler sweep", professor_profiler.sweep), IntervalTrigger(seconds=60), id="profiler")
    sched.add_job(_safe("stress", lambda: stress.calculate_and_store(date.today())),
                  IntervalTrigger(minutes=int(get(cfg, "stress.recalc_minutes", 15))), id="stress")
    sched.add_job(_safe("schedule builder", schedule_builder.run_for_today), _cron(get(cfg, "schedule_builder.run_time", "06:30")), id="schedule")
    sched.add_job(_safe("no_commit_sweep", lambda: progress.no_commit_sweep(date.today())), CronTrigger(hour=23, minute=55), id="no_commit")
    sched.add_job(_safe("morning email", email_sender.send_morning), _cron(get(cfg, "email.morning_time", "07:30")), id="email_am")
    sched.add_job(_safe("evening email", email_sender.send_evening), _cron(get(cfg, "email.evening_time", "21:00")), id="email_pm")
    if get(cfg, "aws.enabled", True):
        sched.add_job(_safe("aws push", aws_push.push_all), IntervalTrigger(seconds=int(get(cfg, "aws.push_seconds", 60))), id="aws")
    sched.add_job(_safe("heartbeat", _heartbeat), IntervalTrigger(seconds=60), id="heartbeat")

    def _handle(signum, frame):
        _stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    sched.start()
    _safe("stress at startup", lambda: stress.calculate_and_store(date.today()))()
    while not _stop.is_set():
        _stop.wait(1.0)
    _safe("scheduler shutdown", lambda: sched.shutdown(wait=True))()
    _safe("stop children", sup.stop_all)()
    _safe("process_state clear", lambda: execute_query("UPDATE process_state SET pid=NULL WHERE process_name='main'"))()
    try:
        os.remove(pidfile)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(run())
