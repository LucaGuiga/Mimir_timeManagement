"""Poller process: one BlockingScheduler, three jobs. Launched by core/main.py."""
import os
import signal
import sys
import threading
import time

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.config import ConfigError, get, load_config, repo_root, validate_config
from db.db import execute_query
from logs.error_handler import log_error
from pollers import canvas, github, oura

SCRIPT = "poller"
APIS = {"canvas": canvas, "github": github, "oura": oura}
_stop = threading.Event()
_paused = set()
_active = 0
_active_lock = threading.Lock()


def _record_cycle(api, state, duration_us, error):
    try:
        execute_query(
            "INSERT INTO poller_status (api_name, state, last_cycle_at, last_cycle_duration_us, last_error) "
            "VALUES (%s,%s,NOW(6),%s,%s) ON DUPLICATE KEY UPDATE state=VALUES(state), last_cycle_at=VALUES(last_cycle_at), "
            "last_cycle_duration_us=VALUES(last_cycle_duration_us), last_error=VALUES(last_error)",
            (api, state, duration_us, error))
    except Exception as e:
        log_error(SCRIPT, type(e).__name__, f"poller_status {api}", str(e))


def _set_state(api, state):
    try:
        execute_query("INSERT INTO poller_status (api_name, state) VALUES (%s,%s) ON DUPLICATE KEY UPDATE state=VALUES(state)",
                      (api, state))
    except Exception as e:
        log_error(SCRIPT, type(e).__name__, f"poller_status {api}", str(e))


def _heartbeat(start=False):
    try:
        if start:
            execute_query("INSERT INTO process_state (process_name, pid, started_at, last_heartbeat) VALUES ('poller',%s,NOW(),NOW()) "
                          "ON DUPLICATE KEY UPDATE pid=VALUES(pid), started_at=NOW(), last_heartbeat=NOW()", (os.getpid(),))
        else:
            execute_query("UPDATE process_state SET pid=%s, last_heartbeat=NOW() WHERE process_name='poller'", (os.getpid(),))
    except Exception as e:
        log_error(SCRIPT, type(e).__name__, "process_state heartbeat", str(e))


def _job(api, module, cfg, scheduler):
    def wrapped():
        global _active
        if _stop.is_set() or api in _paused:
            return
        with _active_lock:
            _active += 1
        t0 = time.perf_counter_ns()
        state, err = "running", None
        try:
            state = module.run_cycle(cfg) or "running"
        except module.AuthFailure as e:
            state, err = "auth_failed", str(e)
            _paused.add(api)
            try:
                scheduler.pause_job(api)
            except Exception as e2:
                log_error(SCRIPT, type(e2).__name__, f"pause_job {api}", str(e2))
            log_error(SCRIPT, "AuthFailure", f"{api}.run_cycle", str(e), "critical")
        except Exception as e:
            err = str(e)
            log_error(SCRIPT, type(e).__name__, f"{api}.run_cycle", str(e))
        finally:
            with _active_lock:
                _active -= 1
        _record_cycle(api, state, (time.perf_counter_ns() - t0) // 1000, err)
        _heartbeat()
    return wrapped


def _pidfile(cfg):
    run_dir = os.path.join(repo_root(), get(cfg, "paths.run_dir", "run"))
    os.makedirs(run_dir, exist_ok=True)
    return os.path.join(run_dir, "poller.pid")


def run():
    try:
        cfg = load_config()
        validate_config(cfg)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
    pidfile = _pidfile(cfg)
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
    _heartbeat(start=True)
    scheduler = BlockingScheduler(job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 30})
    scheduler.add_job(_job("canvas", canvas, cfg, scheduler), IntervalTrigger(seconds=int(get(cfg, "canvas.poll_seconds", 60))), id="canvas")
    scheduler.add_job(_job("github", github, cfg, scheduler), IntervalTrigger(seconds=int(get(cfg, "github.poll_seconds", 60))), id="github")
    oura_job = _job("oura", oura, cfg, scheduler)
    for i, hhmm in enumerate(get(cfg, "oura.poll_times", []) or []):
        h, m = (int(x) for x in str(hhmm).split(":"))
        scheduler.add_job(oura_job, CronTrigger(hour=h, minute=m), id="oura" if i == 0 else f"oura_{i}")
    for api in APIS:
        _set_state(api, "running")

    def _handle(signum, frame):
        _stop.set()
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    try:
        scheduler.start()
    except Exception as e:
        log_error(SCRIPT, type(e).__name__, "scheduler", str(e), "critical")
    deadline = time.monotonic() + float(get(cfg, "supervisor.shutdown_grace_seconds", 30))
    while _active > 0 and time.monotonic() < deadline:
        time.sleep(0.2)
    for api in APIS:
        _set_state(api, "paused")
    try:
        os.remove(pidfile)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(run())
