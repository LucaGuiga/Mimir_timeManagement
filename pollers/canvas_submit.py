"""Submits an assignment to Canvas through the logged in web browser (UCSC Canvas API tokens need professor sponsorship).

The files come from the assignment's folder in the course repo, e.g. coursework/Fall-2026/ECE221/HW/HW3/, the folder
the reminders tell you to commit to. preview() reads the Canvas assignment page and the repo folder without changing
anything: attempts allowed, file types permitted, missing or late, the files that would go. submit() is only called
after you confirm on Telegram: it uploads the files into the page's upload box, presses Submit Assignment, and then
reads the page again to prove Canvas recorded it ("SUBMITTED: ..."). Nothing here ever presses Submit without files."""
import os
import re
import tempfile
from datetime import datetime

import requests

from core import notifier
from core.config import get, load_config
from db.db import fetch_one
from logs.error_handler import log_error
from pollers import canvas_scraper as cs

SCRIPT = "canvas_submit"
GITHUB = "https://api.github.com"
MAX_FILE_BYTES = 50 * 1024 * 1024
SUBMIT_WAIT_S = 90


def _assignment(assignment_id):
    row = fetch_one("SELECT a.*, c.canvas_course_id, c.repo_name, c.repo_path_prefix, c.ical_course_code, c.canvas_course_name "
                    "FROM assignments a JOIN courses c ON c.id = a.course_id WHERE a.id = %s", (int(assignment_id),))
    if not row or not row["canvas_assignment_id"] or row["canvas_assignment_id"] <= 0:
        raise ValueError("this assignment is not a Canvas assignment (it was added by hand or is a calendar event)")
    return row


def repo_folder(cfg, a):
    """The folder the reminders point at: {course folder}/{category folder}/{Label}{number}."""
    label = notifier.TYPE_LABELS.get(a["assignment_type"] or "other", "HW")
    _, folder = notifier.branch_and_folder(a["assignment_type"] or "other", cfg)
    prefix = (a.get("repo_path_prefix") or "").strip("/")
    base = f"{prefix}/{folder}" if prefix else folder
    return f"{base}/{label}{a.get('assignment_number') or 1}"


def repo_files(cfg, a):
    """[(name, size, download_url)] in the assignment's repo folder on GitHub (main branch)."""
    owner, repo = get(cfg, "github.username"), a.get("repo_name")
    if not repo:
        raise ValueError(f"{a['ical_course_code']} has no repo yet")
    path = repo_folder(cfg, a)
    r = requests.get(f"{GITHUB}/repos/{owner}/{repo}/contents/{path}", params={"ref": "main"}, timeout=20,
                     headers={"Authorization": f"Bearer {get(cfg, 'github.pat')}", "Accept": "application/vnd.github+json"})
    if r.status_code == 404:
        return path, []
    r.raise_for_status()
    files = [(f["name"], int(f["size"]), f["download_url"]) for f in r.json() if f.get("type") == "file" and not f["name"].startswith(".")]
    return path, files


def read_page(page, a):
    """What the Canvas assignment page says, read without clicking anything."""
    page.goto(f"{cs.BASE_URL}/courses/{a['canvas_course_id']}/assignments/{a['canvas_assignment_id']}", wait_until="networkidle")
    page.wait_for_timeout(1500)
    if cs._looks_like_login(page):
        raise cs.SessionExpiredError("Canvas session expired")
    text = page.locator("#content").inner_text()
    m = re.search(r"(Unlimited|\d+)\s+Attempts?\s+Allowed", text, re.I)
    attempts = None if not m or m.group(1).lower() == "unlimited" else int(m.group(1))
    m = re.search(r"File permitted:\s*([^\n]+)", text, re.I)
    permitted = [x.strip().lower().lstrip(".") for x in m.group(1).split(",")] if m else None
    m = re.search(r"SUBMITTED:\s*([^\n]+)", text)
    used = len(re.findall(r"Attempt \d+", text)) if m else 0
    return {"attempts_allowed": attempts, "permitted": permitted, "submitted": m.group(1).strip() if m else None,
            "missing": bool(re.search(r"^\s*Missing\s*$", text, re.M)), "late_pill": bool(re.search(r"^\s*Late\s*$", text, re.M)),
            "upload_box": page.locator("#inputFileDrop").count() > 0,
            "new_attempt": page.locator("button:has-text('New Attempt')").count() > 0, "attempts_used": used}


def _session(p, cfg):
    path = cs.cookie_file(cfg)
    try:
        return cs.load_session(p, path)
    except cs.SessionExpiredError:
        if not cs.recover_session(p, cfg, path):
            raise
        return cs.load_session(p, path)


def preview(cfg, assignment_id, now=None):
    """Everything the confirmation message needs. problems: reasons it cannot be submitted (shown instead of asking)."""
    from playwright.sync_api import sync_playwright
    now = now or datetime.now()
    a = _assignment(assignment_id)
    folder, files = repo_files(cfg, a)
    with sync_playwright() as p:
        browser, page = _session(p, cfg)
        try:
            info = read_page(page, a)
        finally:
            browser.close()
    problems = []
    if not files:
        problems.append(f"there are no files in {a['repo_name']}/{folder}/ yet; commit them there first")
    if info["permitted"]:
        bad = [n for n, _, _ in files if n.rsplit(".", 1)[-1].lower() not in info["permitted"]]
        if bad:
            problems.append(f"Canvas only takes {', '.join(x.upper() for x in info['permitted'])} here, not {', '.join(bad)}")
    if any(size > MAX_FILE_BYTES for _, size, _ in files):
        problems.append("a file is over 50 MB")
    if info["submitted"] and info["attempts_allowed"] is not None and info["attempts_used"] >= info["attempts_allowed"]:
        problems.append(f"it is already submitted ({info['submitted']}) and no attempts are left")
    if not info["upload_box"] and not info["new_attempt"]:
        problems.append("the Canvas page has no file upload for this assignment")
    single = info["attempts_allowed"] == 1 or (info["attempts_allowed"] is not None and info["attempts_allowed"] - info["attempts_used"] == 1)
    return {"assignment": a, "folder": folder, "files": [(n, s) for n, s, _ in files], "page": info, "problems": problems,
            "late": bool(a["due_at"] and now > a["due_at"]), "single_attempt": single}


def submit(cfg, assignment_id, expect_files):
    """Uploads the repo files and submits. Returns {ok, submitted, message}. Only called after a confirmed yes."""
    from playwright.sync_api import sync_playwright
    a = _assignment(assignment_id)
    folder, files = repo_files(cfg, a)
    names = sorted(n for n, _, _ in files)
    if not names:   # never press Submit without files
        return {"ok": False, "submitted": None, "message": f"there are no files in {folder}/"}
    if names != sorted(expect_files):
        return {"ok": False, "submitted": None, "message": f"the files in {folder}/ changed since you confirmed ({', '.join(names) or 'none'}); ask again"}
    with tempfile.TemporaryDirectory(prefix="mimir_submit_") as tmp:
        paths = []
        for name, size, url in files:
            r = requests.get(url, timeout=60, headers={"Authorization": f"Bearer {get(cfg, 'github.pat')}"})
            r.raise_for_status()
            path = os.path.join(tmp, name)
            with open(path, "wb") as f:
                f.write(r.content)
            paths.append(path)
        with sync_playwright() as p:
            browser, page = _session(p, cfg)
            try:
                info = read_page(page, a)
                if info["new_attempt"] and not info["upload_box"]:
                    cs._click_text(page, "New Attempt")
                    page.wait_for_timeout(2000)
                box = page.locator("#inputFileDrop")
                if not box.count():
                    return {"ok": False, "submitted": None, "message": "the Canvas page shows no upload box"}
                box.set_input_files(paths)
                for name in names:   # each file shows up in the list once Canvas has it
                    page.get_by_text(name).first.wait_for(timeout=120000)
                page.wait_for_timeout(1500)
                page.locator("#submit-button").click()
                deadline = datetime.now().timestamp() + SUBMIT_WAIT_S
                while datetime.now().timestamp() < deadline:
                    page.wait_for_timeout(3000)
                    m = re.search(r"SUBMITTED:\s*([^\n]+)", page.locator("#content").inner_text())
                    if m:
                        break
                after = read_page(page, a)   # reload: what Canvas has recorded, not what the page shows mid-way
            finally:
                browser.close()
    if after["submitted"]:
        return {"ok": True, "submitted": after["submitted"], "message": f"Canvas shows SUBMITTED: {after['submitted']}"}
    log_error(SCRIPT, "NotConfirmed", f"submit assignment {assignment_id}", "Canvas did not show SUBMITTED after uploading")
    return {"ok": False, "submitted": None, "message": "Canvas did not show it as submitted; check the assignment page"}
