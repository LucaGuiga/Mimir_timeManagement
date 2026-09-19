"""Commit progress helpers and the nightly no commit sweep."""
import json
from datetime import date, datetime, time

from db.db import execute_query, fetch_all, fetch_one

SCRIPT = "progress"


def no_commit_sweep(d=None):
    d = d or date.today()
    logged_at = datetime.now() if d == date.today() else datetime.combine(d, time(23, 59, 59))
    rows = fetch_all("SELECT a.id, a.course_id, c.repo_name, c.repo_path_prefix, c.branches FROM assignments a "
                     "JOIN courses c ON c.id = a.course_id WHERE a.status IN ('pending','open') AND c.mapped = TRUE AND c.active = TRUE")
    inserted = 0
    for a in rows:
        hit = fetch_one("SELECT 1 AS x FROM github_commits WHERE assignment_id = %s AND "
                        "((no_commit = FALSE AND DATE(commit_timestamp) = %s) OR (no_commit = TRUE AND DATE(logged_at) = %s)) LIMIT 1",
                        (a["id"], d, d))
        if hit:
            continue
        branches = a["branches"]
        if isinstance(branches, str):
            try:
                branches = json.loads(branches)
            except ValueError:
                branches = None
        execute_query("INSERT INTO github_commits (course_id, assignment_id, repo_name, branch, file_path, commit_sha, no_commit, logged_at) "
                      "VALUES (%s,%s,%s,%s,%s,NULL,TRUE,%s)",
                      (a["course_id"], a["id"], a["repo_name"] or "", (branches or ["main"])[0], a["repo_path_prefix"] or "", logged_at))
        inserted += 1
    return inserted


def commits_for_day(d=None):
    d = d or date.today()
    rows = fetch_all("SELECT g.*, c.canvas_course_name AS course_name, a.title AS assignment_title FROM github_commits g "
                     "JOIN courses c ON c.id = g.course_id LEFT JOIN assignments a ON a.id = g.assignment_id "
                     "WHERE (g.no_commit = FALSE AND DATE(g.commit_timestamp) = %s) OR (g.no_commit = TRUE AND DATE(g.logged_at) = %s) "
                     "ORDER BY c.canvas_course_name, a.title, g.commit_timestamp", (d, d))
    by_course, no_commit = {}, []
    for r in rows:
        if r["no_commit"]:
            no_commit.append(r)
            continue
        by_course.setdefault(r["course_name"], {}).setdefault(r["assignment_title"] or "(unmatched)", []).append(r)
    return {"date": d, "by_course": by_course, "no_commit": no_commit}


def commit_summary_for_assignment(assignment_id):
    row = fetch_one("SELECT COUNT(DISTINCT commit_sha) AS commit_count, MAX(commit_timestamp) AS last_commit_at, "
                    "COALESCE(SUM(size_delta), 0) AS total_size_delta FROM github_commits WHERE assignment_id = %s AND no_commit = FALSE",
                    (assignment_id,))
    nc = fetch_one("SELECT COUNT(*) AS n FROM github_commits WHERE assignment_id = %s AND no_commit = TRUE", (assignment_id,))
    return {"commit_count": int(row["commit_count"] or 0), "last_commit_at": row["last_commit_at"],
            "total_size_delta": int(row["total_size_delta"] or 0), "no_commit_days": int(nc["n"] or 0)}
