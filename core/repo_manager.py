"""Creates one private GitHub repo per course and maps it in the courses table."""
import base64
import json
import re
import time

import requests

from core import notifier
from core.config import get
from db.db import execute_query, fetch_all
from logs.error_handler import log_error

SCRIPT = "repo_manager"
API = "https://api.github.com"
TIMEOUT = 15
RETRY_AFTER_S = 1800
_last_attempt = {}


def course_code(name, cfg):
    pattern = get(cfg, "repo_manager.course_code_pattern", r"[A-Z]+\d+")
    m = re.search(pattern, re.sub(r"[\s_-]", "", (name or "").upper()))
    return m.group() if m else None


def _headers(cfg):
    return {"Authorization": f"Bearer {get(cfg, 'github.pat')}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def _call(cfg, method, path, **kw):
    r = requests.request(method, API + path, headers=_headers(cfg), timeout=TIMEOUT, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"github {method} {path} -> {r.status_code}: {r.text[:200]}")
    return r.json() if r.content else {}


def _ensure_repo(cfg, owner, name):
    try:
        return _call(cfg, "POST", "/user/repos", json={"name": name, "private": True, "auto_init": True,
                                                      "description": f"Course work for {name}, managed by Mimir"}), True
    except RuntimeError as e:
        if "422" not in str(e):
            raise
        return _call(cfg, "GET", f"/repos/{owner}/{name}"), False


def _default_sha(cfg, owner, name, branch):
    for _ in range(10):
        try:
            return _call(cfg, "GET", f"/repos/{owner}/{name}/git/ref/heads/{branch}")["object"]["sha"]
        except RuntimeError as e:
            if "409" not in str(e) and "404" not in str(e):
                raise
            time.sleep(1)
    raise RuntimeError(f"{name}: default branch {branch} never appeared")


def create_course_repo(cfg, course):
    """Never raises. Returns {ok, repo_name, url, branches, created, message}."""
    owner = get(cfg, "github.username")
    result = {"ok": False, "repo_name": None, "url": None, "branches": [], "created": False, "message": ""}
    try:
        name = course.get("repo_name") or course_code(course.get("canvas_course_name"), cfg)
        if not name:
            raise ValueError(f"cannot derive a course code from '{course.get('canvas_course_name')}'")
        if not owner or not get(cfg, "github.pat"):
            raise ValueError("github.username or github.pat is not configured")
        repo, created = _ensure_repo(cfg, owner, name)
        default = repo.get("default_branch") or "main"
        url = repo.get("html_url") or f"https://github.com/{owner}/{name}"
        folders = list(get(cfg, "repo_manager.default_folders", []) or [])
        wanted = list(get(cfg, "repo_manager.default_branches", ["main"]) or ["main"])
        _default_sha(cfg, owner, name, default)
        for folder in folders:
            path = f"{folder}/.gitkeep"
            try:
                _call(cfg, "GET", f"/repos/{owner}/{name}/contents/{path}", params={"ref": default})
                continue
            except RuntimeError as e:
                if "404" not in str(e):
                    raise
            _call(cfg, "PUT", f"/repos/{owner}/{name}/contents/{path}",
                  json={"message": f"Add {folder}/", "content": base64.b64encode(b"").decode(), "branch": default})
        sha = _default_sha(cfg, owner, name, default)
        existing = {b["name"] for b in _call(cfg, "GET", f"/repos/{owner}/{name}/branches", params={"per_page": 100})}
        for b in wanted:
            if b in existing or b == default:
                continue
            _call(cfg, "POST", f"/repos/{owner}/{name}/git/refs", json={"ref": f"refs/heads/{b}", "sha": sha})
        branches = [default] + [b for b in wanted if b != default]
        execute_query("UPDATE courses SET repo_name = %s, repo_path_prefix = NULL, branches = %s, mapped = TRUE WHERE id = %s",
                      (name, json.dumps(branches), course["id"]))
        notifier.send_message(f"{'Created' if created else 'Mapped existing'} repo {name} for {course.get('canvas_course_name')}: {url}\n"
                              f"Branches: {', '.join(branches)}\nFolders: {', '.join(folders)}")
        result.update(ok=True, repo_name=name, url=url, branches=branches, created=created,
                      message=f"{'created' if created else 'mapped existing'} {url} with branches {', '.join(branches)}")
    except Exception as e:
        log_error(SCRIPT, type(e).__name__, f"create_course_repo {course.get('canvas_course_name')}", str(e))
        notifier.send_warning(f"Repo creation failed for {course.get('canvas_course_name')}: {e}")
        result["message"] = f"{type(e).__name__}: {e}"
    return result


def check_new_courses(cfg):
    if not get(cfg, "repo_manager.auto_create", False):
        return 0
    rows = fetch_all("SELECT id, canvas_course_name, repo_name FROM courses WHERE active = TRUE AND mapped = FALSE AND monitor = TRUE")
    done = 0
    for c in rows:
        last = _last_attempt.get(c["id"])
        if last and time.monotonic() - last < RETRY_AFTER_S:
            continue
        _last_attempt[c["id"]] = time.monotonic()
        if create_course_repo(cfg, c)["ok"]:
            done += 1
    return done
