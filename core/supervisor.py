"""Child process management for core/main.py."""
import os
import signal
import subprocess
import time
from collections import deque

from core.config import get, repo_root
from db.db import execute_query
from logs.error_handler import log_error

SCRIPT = "supervisor"


class Supervisor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.children = {}   # name -> dict(proc, argv, restarts, died_at, gave_up)
        self.run_dir = os.path.join(repo_root(), get(cfg, "paths.run_dir", "run"))
        self.log_dir = os.path.join(repo_root(), get(cfg, "paths.log_dir", "logs"))
        os.makedirs(self.run_dir, exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)

    def _pidfile(self, name):
        return os.path.join(self.run_dir, f"{name}.pid")

    def _state(self, name, pid, started):
        try:
            if started:
                execute_query("INSERT INTO process_state (process_name, pid, started_at, last_heartbeat) VALUES (%s,%s,NOW(),NOW()) "
                              "ON DUPLICATE KEY UPDATE pid=VALUES(pid), started_at=NOW(), last_heartbeat=NOW()", (name, pid))
            else:
                execute_query("UPDATE process_state SET pid=NULL WHERE process_name=%s", (name,))
        except Exception as e:
            log_error(SCRIPT, type(e).__name__, f"process_state {name}", str(e))

    def start_child(self, name, argv):
        out = open(os.path.join(self.log_dir, f"{name}.out"), "ab")
        try:
            proc = subprocess.Popen(argv, cwd=repo_root(), stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        finally:
            out.close()
        entry = self.children.setdefault(name, {"restarts": deque(), "gave_up": False})
        entry.update({"proc": proc, "argv": argv, "died_at": None})
        with open(self._pidfile(name), "w") as f:
            f.write(str(proc.pid))
        self._state(name, proc.pid, True)
        return proc.pid

    def check_children(self):
        now = time.monotonic()
        backoff = float(get(self.cfg, "supervisor.restart_backoff_seconds", 10))
        limit = int(get(self.cfg, "supervisor.max_restarts_per_10min", 3))
        for name, c in self.children.items():
            proc = c.get("proc")
            if c["gave_up"] or proc is None or proc.poll() is None:
                continue
            if c["died_at"] is None:
                c["died_at"] = now
                log_error(SCRIPT, "ChildExited", f"check_children {name}", f"exit code {proc.returncode}; restart in {backoff:.0f}s")
                self._state(name, None, False)
                continue
            if now - c["died_at"] < backoff:
                continue
            while c["restarts"] and now - c["restarts"][0] > 600:
                c["restarts"].popleft()
            if len(c["restarts"]) >= limit:
                c["gave_up"] = True
                log_error(SCRIPT, "RestartLimit", f"check_children {name}",
                          f"{len(c['restarts'])} restarts in 10 minutes; giving up on {name}", "critical")
                continue
            try:
                self.start_child(name, c["argv"])
                c["restarts"].append(now)
            except Exception as e:
                log_error(SCRIPT, type(e).__name__, f"restart {name}", str(e), "critical")

    def restart_child(self, name):
        c = self.children.get(name)
        if c and c.get("proc") and c["proc"].poll() is None:
            try:
                os.killpg(c["proc"].pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if c:
            c["gave_up"] = False

    def stop_all(self, grace_seconds=None):
        grace = float(grace_seconds if grace_seconds is not None else get(self.cfg, "supervisor.shutdown_grace_seconds", 30))
        live = [(n, c["proc"]) for n, c in self.children.items() if c.get("proc") and c["proc"].poll() is None]
        for name, proc in live:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + grace
        for name, proc in live:
            try:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                log_error(SCRIPT, "ShutdownTimeout", f"stop_all {name}", f"{name} ignored SIGTERM for {grace:.0f}s; sending SIGKILL")
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    pass
        for name in self.children:
            self._state(name, None, False)
            try:
                os.remove(self._pidfile(name))
            except OSError:
                pass
