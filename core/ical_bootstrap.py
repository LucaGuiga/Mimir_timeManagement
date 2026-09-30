"""On demand course bootstrap from the Canvas iCal feed. Used only by the /setup GUI, never by the scheduler."""
import re
import zlib

import requests
from icalendar import Calendar

from core import repo_manager
from core.config import get
from logs.error_handler import log_error

SCRIPT = "ical_bootstrap"
TIMEOUT = 15
_CODE_RE = re.compile(r"\[([A-Z0-9\-]+)\]$")
_LAB_SEGMENT_RE = re.compile(r"^(\d+[A-Z]*)L$")


class BootstrapError(Exception):
    @property
    def message(self):
        return str(self.args[0]) if self.args else ""


def _detect_lab_parent(code, codes):
    """Return the parent code when `code` is a lab section (e.g. ECE-141L-01 -> ECE-141-01), else None."""
    parts = code.split("-")
    for i, seg in enumerate(parts):
        m = _LAB_SEGMENT_RE.match(seg)
        if not m:
            continue
        exact = "-".join(parts[:i] + [m.group(1)] + parts[i + 1:])
        if exact in codes:
            return exact
        # Lab and lecture often carry different section numbers; accept the only lecture with the same stem
        stem = parts[:i] + [m.group(1)]
        near = sorted(c for c in codes if c != code and c.split("-")[:i + 1] == stem)
        if near:
            return near[0]
    return None


def fetch_and_parse(cfg):
    url = (get(cfg, "ical.feed_url") or "").strip()
    if not url:
        raise BootstrapError("ical.feed_url is not configured")
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    try:
        r = requests.get(url, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise BootstrapError(f"could not fetch the iCal feed: {e}") from e
    if r.status_code >= 400:
        raise BootstrapError(f"iCal feed returned http {r.status_code}")
    try:
        cal = Calendar.from_ical(r.content)
    except Exception as e:
        raise BootstrapError(f"iCal feed could not be parsed: {e}") from e
    found = {}
    for ev in cal.walk("VEVENT"):
        summary = str(ev.get("SUMMARY") or "").strip()
        m = _CODE_RE.search(summary)
        if not m or m.group(1) in found:
            continue
        found[m.group(1)] = summary[:m.start()].strip()
    codes = set(found)
    out = []
    for code, name in found.items():
        parent = _detect_lab_parent(code, codes)
        out.append({"ical_course_code": code, "course_name": name, "is_lab": parent is not None, "lab_for": parent})
    out.sort(key=lambda c: (c["is_lab"], c["ical_course_code"]))
    return out


def repo_name_for(cfg, ical_course_code, is_lab=False):
    """Same derivation create_course_repo uses (repo_manager.course_code), applied to the code without its section number.
    A lab keeps its L so it does not collide with the lecture repo."""
    parts = (ical_course_code or "").upper().split("-")
    stem = "".join(parts[:-1] if len(parts) >= 3 else parts)
    name = repo_manager.course_code(stem, cfg)
    if not name:
        return (ical_course_code or "").replace("-", "_")
    if is_lab and not name.endswith("L"):
        name += "L"
    return name


def _synthetic_id(cur, code):
    """Deterministic negative id (python's hash() is randomised per process); probe past any collision."""
    n = zlib.crc32(code.encode("utf-8")) % 1000000 + 1
    while True:
        cur.execute("SELECT ical_course_code FROM courses WHERE canvas_course_id = %s", (-n,))
        row = cur.fetchone()
        if not row:
            return -n
        n = n % 1000000 + 1


def seed_courses(cfg, conn, selections):
    cur = conn.cursor(dictionary=True)
    try:
        touched = {}
        for s in selections:
            code = s["ical_course_code"]
            monitor = bool(s.get("monitor"))
            cur.execute("SELECT id FROM courses WHERE ical_course_code = %s LIMIT 1", (code,))
            row = cur.fetchone()
            if row:
                cur.execute("UPDATE courses SET monitor = %s, telegram_enabled = TRUE, email_enabled = TRUE WHERE id = %s", (monitor, row["id"]))
            else:
                cur.execute(
                    "INSERT INTO courses (canvas_course_id, canvas_course_name, ical_course_code, is_lab, monitor, telegram_enabled, "
                    "email_enabled, mapped, setup_complete) VALUES (%s,%s,%s,%s,%s,TRUE,TRUE,FALSE,FALSE)",
                    (_synthetic_id(cur, code), (s.get("course_name") or code)[:255], code, bool(s.get("is_lab")), monitor))
            touched[code] = s
        for code, s in touched.items():
            if s.get("is_lab") and s.get("lab_for"):
                cur.execute("SELECT id FROM courses WHERE ical_course_code = %s LIMIT 1", (s["lab_for"],))
                parent = cur.fetchone()
                if parent:
                    cur.execute("UPDATE courses SET lab_parent_id = %s WHERE ical_course_code = %s", (parent["id"], code))
        result = []
        for code, s in touched.items():
            cur.execute("SELECT id, ical_course_code, canvas_course_name, is_lab, lab_parent_id, monitor FROM courses WHERE ical_course_code = %s LIMIT 1", (code,))
            r = cur.fetchone()
            result.append({"id": int(r["id"]), "ical_course_code": r["ical_course_code"], "canvas_course_name": r["canvas_course_name"],
                           "is_lab": bool(r["is_lab"]), "lab_parent_id": r["lab_parent_id"], "monitor": bool(r["monitor"])})
        return result
    finally:
        cur.close()


def preview_repos(cfg, seeded_courses):
    branches = list(get(cfg, "repo_manager.default_branches", ["main"]) or ["main"])
    folders = list(get(cfg, "repo_manager.default_folders", []) or [])
    return [{
        "course_id": c["id"],
        "ical_course_code": c["ical_course_code"],
        "repo_name": repo_name_for(cfg, c["ical_course_code"], c.get("is_lab")),
        "is_lab": bool(c.get("is_lab")),
        "branches": branches,
        "folders": folders,
        "lab_folder_note": "Lab sections tracked in Labs/ folder" if c.get("is_lab") else None,
    } for c in seeded_courses]
