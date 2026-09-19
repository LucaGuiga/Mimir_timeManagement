"""GitHub poller cycle. Writes github_commits rows for mapped courses."""
import json
import re
import time
from datetime import datetime, timedelta, timezone

import requests

from core.config import get
from db.db import execute_query, fetch_all, fetch_one
from logs.error_handler import log_error

SCRIPT = "github"
TIMEOUT = 15
API = "https://api.github.com"
_etags = {}          # url -> (etag, cached json)
_seen_shas = set()   # shas handled this process lifetime (covers commits with no matching files)
_backoff_until = None


class AuthFailure(Exception):
    pass


class _Backoff(Exception):
    pass


def _to_local(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
    except ValueError:
        return None


def _metric(endpoint, t0, status):
    try:
        execute_query("INSERT INTO poll_metrics (api_name, endpoint, response_time_us, http_status) VALUES (%s,%s,%s,%s)",
                      ("github", endpoint[:512], (time.perf_counter_ns() - t0) // 1000, status))
    except Exception as e:
        log_error(SCRIPT, type(e).__name__, "poll_metrics insert", str(e))


def _request(cfg, path, params=None):
    """Returns parsed JSON, using the cached body on 304. Raises AuthFailure on 401, _Backoff when rate limited."""
    global _backoff_until
    headers = {"Authorization": f"Bearer {get(cfg, 'github.pat')}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    url = requests.Request("GET", API + path, params=params).prepare().url
    cached = _etags.get(url)
    if cached:
        headers["If-None-Match"] = cached[0]
    t0 = time.perf_counter_ns()
    try:
        r = requests.get(url, headers=headers, timeout=TIMEOUT)
    except requests.RequestException as e:
        _metric(path, t0, 0)
        raise RuntimeError(f"github request failed: {e}") from e
    _metric(path, t0, r.status_code)
    if r.status_code == 401:
        raise AuthFailure(f"github 401 on {path}")
    remaining, reset = r.headers.get("X-RateLimit-Remaining"), r.headers.get("X-RateLimit-Reset")
    if remaining is not None and reset:
        try:
            if int(remaining) < int(get(cfg, "github.rate_limit_backoff_threshold", 200)):
                _backoff_until = datetime.fromtimestamp(int(reset))
                log_error(SCRIPT, "RateLimitBackoff", "request",
                          f"remaining={remaining}, backing off until {_backoff_until.isoformat()}")
                raise _Backoff()
        except ValueError:
            pass
    if r.status_code == 304 and cached:
        return cached[1]
    if r.status_code in (403, 429):
        _backoff_until = datetime.now() + timedelta(seconds=int(r.headers.get("Retry-After") or 60))
        log_error(SCRIPT, "RateLimitBackoff", "request", f"http {r.status_code} on {path}, backing off until {_backoff_until.isoformat()}")
        raise _Backoff()
    if r.status_code >= 400:
        raise RuntimeError(f"github {r.status_code} on {path}: {r.text[:200]}")
    body = r.json()
    if r.headers.get("ETag"):
        _etags[url] = (r.headers["ETag"], body)
    return body


def _branches(course):
    b = course.get("branches")
    if isinstance(b, str):
        try:
            b = json.loads(b)
        except ValueError:
            b = None
    return [x for x in (b or ["main"]) if x]


def _match_assignment(cfg, course, rel_path):
    segs = [s for s in rel_path.split("/") if s]
    if not segs:
        return None
    atype = (get(cfg, "github.category_folders", {}) or {}).get(segs[0])
    if not atype:
        return None
    m = re.search(r"\d+", segs[1] if len(segs) > 1 else segs[0])
    if not m:
        return None
    row = fetch_one("SELECT id FROM assignments WHERE course_id=%s AND assignment_type=%s AND assignment_number=%s LIMIT 1",
                    (course["id"], atype, int(m.group())))
    return row["id"] if row else None


def _since(course):
    row = fetch_one("SELECT MAX(commit_timestamp) AS t FROM github_commits WHERE course_id=%s AND commit_sha IS NOT NULL", (course["id"],))
    t = row["t"] if row and row["t"] else datetime.now() - timedelta(days=30)
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _process_commit(cfg, owner, course, branch, prefix, sha):
    detail = _request(cfg, f"/repos/{owner}/{course['repo_name']}/commits/{sha}")
    meta = detail.get("commit") or {}
    ts = _to_local(((meta.get("committer") or meta.get("author")) or {}).get("date"))
    author = ((meta.get("author") or {}).get("name") or "")[:255]
    message = (meta.get("message") or "")[:2000]
    for f in detail.get("files") or []:
        path = f.get("filename") or ""
        if prefix and not (path == prefix or path.startswith(prefix + "/")):
            continue
        try:
            if f.get("status") == "removed":
                size = 0
            else:
                info = _request(cfg, f"/repos/{owner}/{course['repo_name']}/contents/{path}", {"ref": sha})
                size = int(info.get("size") or 0) if isinstance(info, dict) else 0
            prev = fetch_one("SELECT file_size_bytes FROM github_commits WHERE repo_name=%s AND file_path=%s AND commit_sha IS NOT NULL "
                             "ORDER BY commit_timestamp DESC, id DESC LIMIT 1", (course["repo_name"], path[:700]))
            prev_size = prev["file_size_bytes"] if prev and prev["file_size_bytes"] is not None else 0
            rel = path[len(prefix) + 1:] if prefix else path
            execute_query(
                "INSERT INTO github_commits (course_id, assignment_id, repo_name, branch, file_path, commit_sha, commit_message, "
                "commit_author, commit_timestamp, file_size_bytes, prev_file_size_bytes, size_delta, no_commit) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE)",
                (course["id"], _match_assignment(cfg, course, rel), course["repo_name"], branch, path[:700], sha,
                 message, author, ts, size, prev_size, size - prev_size))
        except (AuthFailure, _Backoff):
            raise
        except Exception as e:
            log_error(SCRIPT, type(e).__name__, f"commit {sha[:8]} file {path}", str(e))


def run_cycle(cfg):
    global _backoff_until
    if _backoff_until and datetime.now() < _backoff_until:
        return "backoff"
    _backoff_until = None
    owner = get(cfg, "github.username")
    courses = fetch_all("SELECT id, repo_name, repo_path_prefix, branches FROM courses "
                        "WHERE mapped = TRUE AND repo_name IS NOT NULL AND repo_name <> '' AND active = TRUE")
    try:
        for course in courses:
            prefix = (course.get("repo_path_prefix") or "").strip("/")
            known = {r["commit_sha"] for r in fetch_all(
                "SELECT DISTINCT commit_sha FROM github_commits WHERE repo_name=%s AND commit_sha IS NOT NULL", (course["repo_name"],))}
            for branch in _branches(course):
                try:
                    params = {"sha": branch, "since": _since(course), "per_page": 50}
                    if prefix:
                        params["path"] = prefix
                    commits = _request(cfg, f"/repos/{owner}/{course['repo_name']}/commits", params)
                    for c in reversed(commits or []):
                        sha = c.get("sha")
                        if not sha or sha in known or sha in _seen_shas:
                            continue
                        _process_commit(cfg, owner, course, branch, prefix, sha)
                        _seen_shas.add(sha)
                except (AuthFailure, _Backoff):
                    raise
                except Exception as e:
                    log_error(SCRIPT, type(e).__name__, f"repo {course['repo_name']} branch {branch}", str(e))
    except _Backoff:
        return "backoff"
    return None
