"""Sets up course repos on GitHub and maps them in the courses table.

repo_manager.layout "shared" (default): one private repo (repo_manager.shared_repo) holding a folder per course,
{term}/{COURSE}/{category}/, e.g. coursework/Fall-2026/ECE129/HW/. "per_course": one private repo per course."""
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


def layout(cfg):
    return "per_course" if get(cfg, "repo_manager.layout", "shared") == "per_course" else "shared"


def shared_repo(cfg):
    return (get(cfg, "repo_manager.shared_repo", "") or "coursework").strip()


def term_folder(cfg):
    """Folder above the course folders in the shared repo, from quarter_label ("Fall 2026" -> "Fall-2026"). Empty means none."""
    if not get(cfg, "repo_manager.use_term_folder", True):
        return ""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", (get(cfg, "quarter_label", "") or "").strip()).strip("-")


def course_folder_name(cfg, course):
    """Course code without its section number (ECE-129A-01 -> ECE129). A lab keeps its L so it does not collide with the lecture."""
    code = course.get("ical_course_code")
    if code:
        parts = code.upper().split("-")
        name = course_code("".join(parts[:-1] if len(parts) >= 3 else parts), cfg)
        if not name:
            return code.replace("-", "_")
    else:
        name = course_code(course.get("canvas_course_name"), cfg)
        if not name:
            return None
    if course.get("is_lab") and not name.endswith("L"):
        name += "L"
    return name


def course_path(cfg, course):
    """Path of the course folder inside the shared repo, e.g. Fall-2026/ECE129."""
    folder = course_folder_name(cfg, course)
    if not folder:
        return None
    term = term_folder(cfg)
    return f"{term}/{folder}" if term else folder


def target(cfg, course):
    """(repo name, path prefix or None) a course would be set up in under the configured layout."""
    if layout(cfg) == "shared":
        return shared_repo(cfg), course_path(cfg, course)
    return course.get("repo_name") or course_code(course.get("canvas_course_name"), cfg), None


def _headers(cfg):
    return {"Authorization": f"Bearer {get(cfg, 'github.pat')}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def _call(cfg, method, path, **kw):
    r = requests.request(method, API + path, headers=_headers(cfg), timeout=TIMEOUT, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"github {method} {path} -> {r.status_code}: {r.text[:200]}")
    return r.json() if r.content else {}


def _ensure_repo(cfg, owner, name, description=None):
    try:
        return _call(cfg, "POST", "/user/repos", json={"name": name, "private": True, "auto_init": True,
                                                      "description": description or f"Course work for {name}, managed by Mimir"}), True
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
    """Never raises. Returns {ok, repo_name, path, url, branches, created, message}.
    Shared layout: makes the shared repo if needed and adds {path}/{category}/ folders. Per course: one repo per course."""
    owner = get(cfg, "github.username")
    result = {"ok": False, "repo_name": None, "path": None, "url": None, "branches": [], "created": False, "message": ""}
    try:
        shared = layout(cfg) == "shared"
        name, prefix = target(cfg, course)
        if not name or (shared and not prefix):
            raise ValueError(f"cannot derive a course code from '{course.get('ical_course_code') or course.get('canvas_course_name')}'")
        if not owner or not get(cfg, "github.pat"):
            raise ValueError("github.username or github.pat is not configured")
        repo, created = _ensure_repo(cfg, owner, name, "School course work, one folder per course, managed by Mimir" if shared else None)
        default = repo.get("default_branch") or "main"
        url = repo.get("html_url") or f"https://github.com/{owner}/{name}"
        folders = list(get(cfg, "repo_manager.default_folders", []) or [])
        wanted = list(get(cfg, "repo_manager.default_branches", ["main"]) or ["main"])
        _default_sha(cfg, owner, name, default)
        for folder in folders:
            path = f"{prefix}/{folder}/.gitkeep" if prefix else f"{folder}/.gitkeep"
            try:
                _call(cfg, "GET", f"/repos/{owner}/{name}/contents/{path}", params={"ref": default})
                continue
            except RuntimeError as e:
                if "404" not in str(e):
                    raise
            _call(cfg, "PUT", f"/repos/{owner}/{name}/contents/{path}",
                  json={"message": f"Add {path.rsplit('/', 1)[0]}/", "content": base64.b64encode(b"").decode(), "branch": default})
        sha = _default_sha(cfg, owner, name, default)
        existing = {b["name"] for b in _call(cfg, "GET", f"/repos/{owner}/{name}/branches", params={"per_page": 100})}
        for b in wanted:
            if b in existing or b == default:
                continue
            _call(cfg, "POST", f"/repos/{owner}/{name}/git/refs", json={"ref": f"refs/heads/{b}", "sha": sha})
        branches = [default] + [b for b in wanted if b != default]
        execute_query("UPDATE courses SET repo_name = %s, repo_path_prefix = %s, branches = %s, mapped = TRUE WHERE id = %s",
                      (name, prefix, json.dumps(branches), course["id"]))
        if shared:
            folder_url = f"{url}/tree/{default}/{prefix}"
            notifier.send_message(f"Added {course.get('canvas_course_name')} to {name}{' (new repo)' if created else ''}: {folder_url}\n"
                                  f"Folders: {', '.join(f'{prefix}/{f}' for f in folders)}")
            message = f"added {prefix}/ to {url}{' (new repo)' if created else ''}"
        else:
            notifier.send_message(f"{'Created' if created else 'Mapped existing'} repo {name} for {course.get('canvas_course_name')}: {url}\n"
                                  f"Branches: {', '.join(branches)}\nFolders: {', '.join(folders)}")
            message = f"{'created' if created else 'mapped existing'} {url} with branches {', '.join(branches)}"
        result.update(ok=True, repo_name=name, path=prefix, url=url, branches=branches, created=created, message=message)
    except Exception as e:
        log_error(SCRIPT, type(e).__name__, f"create_course_repo {course.get('canvas_course_name')}", str(e))
        notifier.send_warning(f"Repo creation failed for {course.get('canvas_course_name')}: {e}")
        result["message"] = f"{type(e).__name__}: {e}"
    return result


def check_new_courses(cfg):
    if not get(cfg, "repo_manager.auto_create", False):
        return 0
    rows = fetch_all("SELECT id, canvas_course_name, ical_course_code, is_lab, repo_name FROM courses "
                     "WHERE active = TRUE AND mapped = FALSE AND monitor = TRUE")
    done = 0
    for c in rows:
        last = _last_attempt.get(c["id"])
        if last and time.monotonic() - last < RETRY_AFTER_S:
            continue
        _last_attempt[c["id"]] = time.monotonic()
        if create_course_repo(cfg, c)["ok"]:
            done += 1
    return done
