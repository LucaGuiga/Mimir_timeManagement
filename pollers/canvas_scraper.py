"""Playwright scraper for Canvas announcements, submission status, grades, and available dates.
Uses cookies saved by `python -m pollers.canvas_scraper --login` (headed, Duo Mobile completed by hand)."""
import json
import os
import re
import sys
import time
import zlib
from datetime import datetime

import hashlib

from urllib.parse import urlsplit

from core import llm_parser, notifier, syllabus_parser
from core.config import get, load_config, repo_root
from db.db import execute_query, fetch_all, fetch_one
from logs.error_handler import log_error

SCRIPT = "scraper"
COOKIE_PATH = "run/canvas_cookies.json"
BASE_URL = "https://canvas.ucsc.edu"
LOGIN_TIMEOUT_S = 60
NAV_TIMEOUT_MS = 30000
EXECUTABLE_PATH = None   # leave None for Playwright's bundled Chromium
MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"


class AuthFailure(Exception):
    """Present so the poller job wrapper can treat this module like the other pollers."""


class SessionExpiredError(Exception):
    pass


def cookie_file(cfg=None):
    rel = (get(cfg, "scraper.cookie_path") if cfg else None) or COOKIE_PATH
    return rel if os.path.isabs(rel) else os.path.join(repo_root(), rel)


def _launch(playwright, headless=True):
    kw = {"headless": headless}
    if EXECUTABLE_PATH:
        kw["executable_path"] = EXECUTABLE_PATH
    return playwright.chromium.launch(**kw)


LOGIN_TITLE = re.compile(r"(?<![a-z])log ?in(?![a-z])", re.I)   # whole words only: "Analog Integrated Circuits" is not a login page


def _looks_like_login(page):
    try:
        if page.locator("input[type=password], form[action*='login'], #login_form").count():
            return True
    except Exception:
        pass
    parts = urlsplit(page.url)
    return bool(LOGIN_TITLE.search(page.title() or "")) or "login" in parts.netloc.lower() or "/login" in parts.path.lower()


def load_session(playwright, cookie_path=None):
    path = cookie_path or cookie_file()
    browser = _launch(playwright, headless=True)
    try:
        context = browser.new_context()
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                context.add_cookies(json.load(f))
        page = context.new_page()
        page.set_default_timeout(NAV_TIMEOUT_MS)
        page.goto(BASE_URL + "/", wait_until="domcontentloaded")
        if "dashboard" in (page.title() or "").lower():
            return browser, page
        if _looks_like_login(page):
            raise SessionExpiredError("Canvas session expired. Run: python -m pollers.canvas_scraper --login")
        return browser, page
    except Exception:
        browser.close()
        raise


AUTO_LOGIN_TIMEOUT_S = 150          # time to finish the sign in, including a Duo push approved on the phone
AUTO_LOGIN_RETRY_MIN = 30           # at most one automatic attempt per this many minutes
DUO_HOST = "duosecurity.com"


def _creds(cfg):
    c = get(cfg, "canvas", {}) or {}
    u, pw = str(c.get("username") or "").strip(), str(c.get("password") or "")
    return (u, pw) if u and pw else None


def _creds_key(creds):
    """Fingerprint of the credentials, so a rejected sign in is not retried until they change. Not the password."""
    return hashlib.sha256(f"{creds[0]}\0{creds[1]}".encode()).hexdigest()[:16]


def _write_cookies(path, cookies):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cookies, f)
    os.replace(tmp, path)


def _login_error_text(page):
    for sel in (".form-error", ".alert-danger", "#error", ".output--error", "[role=alert]"):
        loc = page.locator(sel)
        if loc.count() and loc.first.inner_text().strip():
            return loc.first.inner_text().strip()[:200]
    return None


def _click_text(page, label):
    """Clicks the first visible button or link whose text contains label (Duo's buttons are matched by their text)."""
    loc = page.locator(f"button:has-text('{label}'), a:has-text('{label}'), [role=button]:has-text('{label}')")
    for i in range(loc.count()):
        if loc.nth(i).is_visible():
            loc.nth(i).click()
            return True
    return False


def auto_login(playwright, cfg, cookie_path=None):
    """Signs in to Canvas with canvas.username / canvas.password, the way a person would: the CruzID form on
    login.ucsc.edu, then Duo. Duo's saved "remember this browser" cookies are loaded first, so most days no push is
    needed; when Duo does push, Telegram says so at once. Returns "ok", "bad_credentials", "timeout" or "no_credentials".
    The password is only typed into login.ucsc.edu and is never logged."""
    creds = _creds(cfg)
    if not creds:
        return "no_credentials"
    path = cookie_path or cookie_file(cfg)
    browser = _launch(playwright, headless=True)
    try:
        ua = browser.new_page().evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome")   # look like the browser Duo remembers
        context = browser.new_context(user_agent=ua)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                context.add_cookies([c for c in json.load(f) if DUO_HOST in c.get("domain", "")])   # keep Duo's trust, not the dead session
        page = context.new_page()
        page.set_default_timeout(NAV_TIMEOUT_MS)
        page.goto(BASE_URL + "/login", wait_until="domcontentloaded")
        if "login.ucsc.edu" in urlsplit(page.url).netloc and page.locator("#password").count():
            page.fill("#username_display", creds[0])
            page.evaluate("u => { const h = document.querySelector('#username'); if (h) h.value = u; }", creds[0])
            page.fill("#password", creds[1])
            page.click("button[name=_eventId_proceed]")
        told, deadline = False, time.monotonic() + AUTO_LOGIN_TIMEOUT_S
        while time.monotonic() < deadline:
            page.wait_for_timeout(1500)
            host = urlsplit(page.url).netloc
            if host == urlsplit(BASE_URL).netloc and not _looks_like_login(page):
                _write_cookies(path, context.cookies())
                return "ok"
            if "login.ucsc.edu" in host and page.locator("#password").count():
                err = _login_error_text(page)
                if err:
                    log_error(SCRIPT, "AutoLoginRejected", "auto_login", f"UCSC rejected the sign in: {err}")
                    return "bad_credentials"
            if DUO_HOST in host:
                # "Duo Desktop needs permission to access your local network": nothing to grant on a server
                for label in ("Skip for now", "Yes, this is my device", "Trust browser"):
                    _click_text(page, label)
                if not told and page.get_by_text(re.compile(r"(push|check for a duo|sent to)", re.I)).count():
                    notifier.deliver("Canvas sign in: approve the Duo push on your phone now (Mimir is logging back in to Canvas).")
                    told = True
                if not told:
                    for label in ("Send Me a Push", "Send a Push", "Duo Push"):
                        if _click_text(page, label):
                            break
        log_error(SCRIPT, "AutoLoginTimeout", "auto_login", f"no Canvas dashboard after {AUTO_LOGIN_TIMEOUT_S}s (stopped at {urlsplit(page.url).netloc})")
        return "timeout"
    finally:
        browser.close()


def recover_session(playwright, cfg, path):
    """After Canvas says the session ended: one automatic sign in at most every AUTO_LOGIN_RETRY_MIN minutes, never again
    with credentials UCSC rejected, and one instant Telegram message per outage when a person has to step in."""
    creds = _creds(cfg)
    if creds and notifier._state_get("canvas_bad_credentials") != _creds_key(creds):
        last = notifier._state_get("canvas_login_attempt_at")
        if not last or (datetime.now() - datetime.fromisoformat(last)).total_seconds() >= AUTO_LOGIN_RETRY_MIN * 60:
            notifier._state_set("canvas_login_attempt_at", datetime.now().isoformat(timespec="seconds"))
            result = auto_login(playwright, cfg, path)
            if result == "ok":
                notifier._state_set("canvas_login_notified", "")
                return True
            if result == "bad_credentials":
                notifier._state_set("canvas_bad_credentials", _creds_key(creds))
    if not notifier._state_get("canvas_login_notified"):
        reason = ("UCSC rejected the saved CruzID sign in; fix canvas.username / canvas.password in config.yaml"
                  if creds and notifier._state_get("canvas_bad_credentials") == _creds_key(creds)
                  else "the automatic sign in did not finish" if creds else "no saved CruzID sign in")
        notifier.deliver(f"Canvas login needed ({reason}). On the server, in the Mimir folder: venv/bin/python -m pollers.canvas_scraper --login")
        notifier._state_set("canvas_login_notified", datetime.now().isoformat(timespec="seconds"))
    return False


def interactive_login(playwright, cookie_path=None):
    path = cookie_path or cookie_file()
    browser = _launch(playwright, headless=False)
    try:
        context = browser.new_context()
        page = context.new_page()
        page.goto(BASE_URL + "/login", wait_until="domcontentloaded")
        print("Complete the UCSC login and approve the Duo Mobile push in the browser window.")
        deadline = time.monotonic() + LOGIN_TIMEOUT_S
        while time.monotonic() < deadline:
            if "dashboard" in page.url.lower() or "dashboard" in (page.title() or "").lower():
                break
            time.sleep(1)
        else:
            print(f"Timed out after {LOGIN_TIMEOUT_S}s without reaching the Canvas dashboard; cookies not saved.")
            return False
        _write_cookies(path, context.cookies())   # owner only: these cookies are a logged in Canvas session
        print(f"Canvas session saved to {path}")
        return True
    finally:
        browser.close()


def _metric(page_path, t0, status):
    try:
        execute_query("INSERT INTO poll_metrics (api_name, endpoint, response_time_us, http_status) VALUES (%s,%s,%s,%s)",
                      ("scraper", page_path[:512], (time.perf_counter_ns() - t0) // 1000, status))
    except Exception as e:
        log_error(SCRIPT, type(e).__name__, "poll_metrics insert", str(e))


def _goto(page, path):
    t0 = time.perf_counter_ns()
    resp = page.goto(BASE_URL + path, wait_until="networkidle")
    _metric(path, t0, resp.status if resp else 0)
    return resp


def parse_canvas_date(text):
    """Parses 'Sep 30 at 11:59pm', 'Sep 30, 2026 at 11:59pm', 'Oct 3 by 5pm', or ISO strings. Returns naive datetime or None."""
    if not text:
        return None
    text = text.strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
    except ValueError:
        pass
    m = re.search(rf"\b({MONTHS})[a-z]*\.?\s+(\d{{1,2}})(?:,?\s+(\d{{4}}))?(?:\s+(?:at|by)\s+(\d{{1,2}})(?::(\d{{2}}))?\s*(am|pm)?)?", text, re.I)
    if not m:
        return None
    mon = m.group(1)[:3].lower()
    month = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"].index(mon) + 1
    now = datetime.now()
    year = int(m.group(3)) if m.group(3) else now.year
    hour, minute = 23, 59
    if m.group(4):
        hour, minute = int(m.group(4)), int(m.group(5) or 0)
        ap = (m.group(6) or "").lower()
        if ap == "pm" and hour < 12:
            hour += 12
        if ap == "am" and hour == 12:
            hour = 0
    try:
        dt = datetime(year, month, int(m.group(2)), hour, minute)
    except ValueError:
        return None
    return dt


def parse_grade_percent(text):
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:/|out of)\s*(\d+(?:\.\d+)?)", text or "", re.I)
    if m and float(m.group(2)) > 0:
        return round(100 * float(m.group(1)) / float(m.group(2)), 2)
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text or "")
    return float(m.group(1)) if m else None


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


_RANK = {"pending": 0, "open": 1, "submitted": 2, "graded": 3}


def _hash(text):
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _page_changed(key, text):
    """True when this page's text differs from the last successful parse. Does not record anything."""
    row = fetch_one("SELECT content_hash FROM scrape_cache WHERE page_key = %s", (key,))
    return not (row and row["content_hash"] == _hash(text))


def _mark_parsed(key, text):
    """Called only after a parse succeeded and its results were written, so a failed page is retried next cycle."""
    execute_query("INSERT INTO scrape_cache (page_key, content_hash, updated_at) VALUES (%s,%s,NOW()) "
                  "ON DUPLICATE KEY UPDATE content_hash = VALUES(content_hash), updated_at = NOW()", (key, _hash(text)))


def _llm_page_text(page):
    if _looks_like_login(page):
        raise SessionExpiredError("Canvas session expired. Run: python -m pollers.canvas_scraper --login")
    return page.inner_text("body")


def _apply_assignment(a, available_from, new_status, pct):
    """Update one known assignment row. Only fills blanks and moves status forward. Returns True if it changed."""
    sets, params = [], []
    if a["available_from"] is None and available_from:
        sets.append("available_from = %s"); params.append(available_from)
    if new_status and _RANK[new_status] > _RANK.get(a["status"], 0):
        sets.append("status = %s"); params.append(new_status)
        if new_status == "graded" and pct is not None and a["grade_percent"] is None:
            sets.append("grade_percent = %s"); params.append(pct)
            sets.append("grade_detected_at = %s"); params.append(datetime.now())
    if sets:
        execute_query(f"UPDATE assignments SET {', '.join(sets)} WHERE id = %s", params + [a["id"]])
    return bool(sets)


def _llm_assignments(cfg, page, course):
    text = _llm_page_text(page)
    key = f"assignments:{course['id']}"
    if not _page_changed(key, text):
        return 0
    known = {_norm(a["title"]): a for a in fetch_all("SELECT id, title, status, available_from, grade_percent FROM assignments WHERE course_id = %s", (course["id"],))}
    updated = 0
    for it in llm_parser.parse_page(cfg, "assignments", text):
        a = known.get(_norm(it["title"]))
        if a and _apply_assignment(a, it["available_from"], it["status"], it["grade_percent"]):
            updated += 1
    _mark_parsed(key, text)
    return updated


def _llm_announcements(cfg, page, course):
    text = _llm_page_text(page)
    key = f"announcements:{course['id']}"
    if not _page_changed(key, text):
        return 0
    new = 0
    for it in llm_parser.parse_page(cfg, "announcements", text):
        cid = -(zlib.crc32(f"{course['id']}|{it['title']}|{it['posted_at']}".encode()) or 1)
        if fetch_one("SELECT id FROM announcements WHERE canvas_announcement_id = %s OR (course_id = %s AND title = %s AND posted_at <=> %s)",
                     (cid, course["id"], it["title"], it["posted_at"])):
            continue
        execute_query("INSERT INTO announcements (course_id, canvas_announcement_id, title, body, posted_at, profiled) VALUES (%s,%s,%s,%s,%s,FALSE)",
                      (course["id"], cid, it["title"], it["body"], it["posted_at"]))
        new += 1
    _mark_parsed(key, text)
    return new


def scrape_announcements(page, course, cfg=None):
    _goto(page, f"/courses/{course['canvas_course_id']}/announcements")
    rows = page.locator(".ic-announcement-row, [data-testid='announcement-row'], .announcement-row")
    n = rows.count()
    if n == 0 and cfg is not None and llm_parser.enabled(cfg):
        return _llm_announcements(cfg, page, course)
    if n == 0:
        log_error(SCRIPT, "NoRows", f"announcements course {course['canvas_course_id']}", "no announcement rows found (page layout may have changed)")
        return 0
    new = 0
    for i in range(n):
        row = rows.nth(i)
        title_el = row.locator("h3, .ic-item-row__content h3, a.ic-item-row__content-link").first
        title = (title_el.inner_text() if title_el.count() else "").strip()[:512]
        if not title:
            continue
        href = (row.locator("a[href*='discussion_topics']").first.get_attribute("href") if row.locator("a[href*='discussion_topics']").count() else "") or ""
        m = re.search(r"/discussion_topics/(\d+)", href)
        cid = int(m.group(1)) if m else -(zlib.crc32(f"{course['id']}|{title}".encode()) or 1)
        body_el = row.locator(".ic-announcement-row__content, .ic-item-row__content p, .announcement-body").first
        body = body_el.inner_text().strip() if body_el.count() else None
        time_el = row.locator("time").first
        posted = parse_canvas_date(time_el.get_attribute("datetime") or time_el.inner_text()) if time_el.count() else None
        if fetch_one("SELECT id FROM announcements WHERE canvas_announcement_id = %s OR (course_id = %s AND title = %s AND posted_at <=> %s)",
                     (cid, course["id"], title, posted)):
            continue
        execute_query("INSERT INTO announcements (course_id, canvas_announcement_id, title, body, posted_at, profiled) VALUES (%s,%s,%s,%s,%s,FALSE)",
                      (course["id"], cid, title, body, posted))
        new += 1
    return new


def scrape_assignments(page, course, cfg=None):
    _goto(page, f"/courses/{course['canvas_course_id']}/assignments")
    if not urlsplit(page.url).path.rstrip("/").endswith("/assignments"):
        # the course hides its Assignments page (THEA80 does) and Canvas sent us to the home page, whose Modules rows look
        # like assignment rows but are pages and links; statuses for this course come from scrape_grades instead
        return 0
    rows = page.locator(".ig-row, [data-testid='assignment-row'], .assignment-row")
    n = rows.count()
    if n == 0 and cfg is not None and llm_parser.enabled(cfg):
        return _llm_assignments(cfg, page, course)
    if n == 0:
        log_error(SCRIPT, "NoRows", f"assignments course {course['canvas_course_id']}", "no assignment rows found (page layout may have changed)")
        return 0
    known = {_norm(a["title"]): a for a in fetch_all("SELECT id, title, status, available_from, grade_percent FROM assignments WHERE course_id = %s", (course["id"],))}
    updated = 0
    for i in range(n):
        row = rows.nth(i)
        title_el = row.locator(".ig-title, a.ig-title, .assignment-title, h3 a").first
        title = (title_el.inner_text() if title_el.count() else "").strip()
        a = known.get(_norm(title))
        if not a:
            continue
        text = row.inner_text()
        sets, params = [], []
        if a["available_from"] is None:
            m = re.search(r"Available\s+(?:from|after|on)?\s*([^\n|]+?)(?:\s*\||\n|$)", text, re.I)
            av = parse_canvas_date(m.group(1)) if m else None
            if av:
                sets.append("available_from = %s"); params.append(av)
        low = text.lower()
        new_status = "graded" if "graded" in low else "submitted" if "submitted" in low and "not submitted" not in low else None
        if new_status and {"pending": 0, "open": 1, "submitted": 2, "graded": 3}[new_status] > {"pending": 0, "open": 1, "submitted": 2, "graded": 3}.get(a["status"], 0):
            sets.append("status = %s"); params.append(new_status)
            if new_status == "graded":
                pct = parse_grade_percent(text)
                if pct is not None and a["grade_percent"] is None:
                    sets.append("grade_percent = %s"); params.append(pct)
                    sets.append("grade_detected_at = %s"); params.append(datetime.now())
        if sets:
            execute_query(f"UPDATE assignments SET {', '.join(sets)} WHERE id = %s", params + [a["id"]])
            updated += 1
    return updated


def read_grade_rows(page):
    """[{title, graded, submitted, missing, late, percent}] from the course Grades page (tr.student_assignment rows).
    Works when a course hides its Assignments page. Read only: the What-If score inputs are never touched."""
    out = []
    rows = page.locator("tr.student_assignment")
    for i in range(rows.count()):
        r = rows.nth(i)
        title_el = r.locator("th.title a")
        title = (title_el.first.inner_text() if title_el.count() else r.locator("th.title").first.inner_text()).split("\n")[0].strip()
        cls = r.get_attribute("class") or ""
        status = (r.locator("td.status").first.inner_text().strip().lower() if r.locator("td.status").count() else "")
        score = r.locator("td.assignment_score").first.inner_text() if r.locator("td.assignment_score").count() else ""
        score = score.replace("Click to test a different score", " ")
        missing = status == "missing"
        # Canvas gives missing work an automatic 0 and marks it graded; missing wins, it is still not handed in
        graded = "assignment_graded" in cls and not missing
        submitted = not missing and (graded or r.locator(".submission_icon").count() > 0 or "Submission" in score or status == "late")
        out.append({"title": title, "graded": graded, "submitted": submitted, "missing": missing, "late": status == "late",
                    "percent": parse_grade_percent(score) if graded else None})
    return out


def scrape_grades(page, course, cfg=None):
    """Submission status and grades for every assignment from the Grades page. Moves a status forward only
    (open/pending -> submitted -> graded); calendar events are left alone. Returns how many assignments changed."""
    _goto(page, f"/courses/{course['canvas_course_id']}/grades")
    if _looks_like_login(page):
        raise SessionExpiredError("Canvas session expired. Run: python -m pollers.canvas_scraper --login")
    if "/grades" not in urlsplit(page.url).path:
        return 0
    known = {_norm(a["title"]): a for a in fetch_all("SELECT id, title, status, grade_percent FROM assignments "
                                                     "WHERE course_id = %s AND is_event = FALSE", (course["id"],))}
    updated = 0
    for g in read_grade_rows(page):
        a = known.get(_norm(g["title"]))
        if not a:
            continue
        new_status = "graded" if g["graded"] else "submitted" if g["submitted"] else None
        sets, params = [], []
        if new_status and _RANK[new_status] > _RANK.get(a["status"], 0):
            sets.append("status = %s"); params.append(new_status)
        if g["percent"] is not None and a["grade_percent"] is None:
            sets.append("grade_percent = %s"); params.append(g["percent"])
            sets.append("grade_detected_at = %s"); params.append(datetime.now())
        if sets:
            execute_query(f"UPDATE assignments SET {', '.join(sets)} WHERE id = %s", params + [a["id"]])
            updated += 1
    return updated


SYLLABUS_HINT = re.compile(r"syllabus", re.I)
GDOC_RE =re.compile(r"docs\.google\.com/document/d/([A-Za-z0-9_-]{20,})")
GDRIVE_RE = re.compile(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?(?:[^\"' ]*&)?id=)([A-Za-z0-9_-]{20,})")
MAX_PDF_BYTES = 15 * 1024 * 1024
MAX_PDFS = 3                   # most files (and Google documents) read per course
MAX_SYLLABUS_PAGES = 3         # most "Syllabus" Pages / module items followed per course
MIN_PAGE_CHARS = 300           # below this a page is treated as empty
GOOGLE_TIMEOUT_S = 30
UNBILLED_FAILURE = re.compile(r"deepseek http (402|429|5\d\d)|deepseek request failed|needs deepseek\.enabled")


def _check_age_hours(key):
    row = fetch_one("SELECT TIMESTAMPDIFF(HOUR, updated_at, NOW()) AS h FROM scrape_cache WHERE page_key = %s", (key,))
    return None if not row else row["h"]


def _visible_text(page, selector):
    loc = page.locator(selector)
    return loc.first.inner_text().strip() if loc.count() else ""


def _links(page, scope="#content"):
    """(href, text) for every link and embedded frame inside scope on the current page."""
    return page.eval_on_selector_all(f"{scope} a[href], {scope} iframe[src]",
                                     "els => els.map(e => [e.getAttribute('href') || e.getAttribute('src') || '', (e.innerText || e.getAttribute('title') || '').trim()])")


def classify_links(pairs, canvas_course_id, any_google_doc=False):
    """Sorts links into Canvas files, Canvas pages, Google Docs and Google Drive files that look like the syllabus.
    A link counts when its text or address says syllabus. any_google_doc: the links sit on a syllabus page, so an
    embedded or linked Google Doc counts even without the word (THEA80 embeds its syllabus document)."""
    host = urlsplit(BASE_URL).netloc
    out = {"files": {}, "pages": {}, "gdocs": {}, "gdrive": {}}
    for href, text in pairs:
        href = href or ""
        text = " ".join((text or "").replace("Links to an external site.", "").split())   # Canvas adds that to external links
        hinted = bool(SYLLABUS_HINT.search(text) or SYLLABUS_HINT.search(href))
        m = GDOC_RE.search(href)
        if m:
            if hinted or any_google_doc:
                out["gdocs"].setdefault(m.group(1), text or "Google Doc")
            continue
        m = GDRIVE_RE.search(href)
        if m:
            if hinted:
                out["gdrive"].setdefault(m.group(1), text or "Google Drive file")
            continue
        parts = urlsplit(href)
        if not hinted or (parts.netloc and parts.netloc != host):
            continue                                   # never send the Canvas login cookies to another site
        m = re.search(r"/files/(\d+)", parts.path)
        if m:
            out["files"].setdefault(int(m.group(1)), text or f"file {m.group(1)}")
        elif re.match(rf"/courses/{canvas_course_id}/(pages/|modules/items/\d+)", parts.path):
            out["pages"].setdefault(parts.path, text or parts.path)
    return out


def _download_file(page, course_id, file_id):
    """Bytes of a PDF or Word file using the logged in browser session, or None if it is another type or too big."""
    r = page.context.request.get(f"{BASE_URL}/courses/{course_id}/files/{file_id}/download?download_frd=1", timeout=NAV_TIMEOUT_MS)
    if r.status != 200:
        return None
    body = r.body()
    return body if (body[:5] == b"%PDF-" or body[:2] == b"PK") and len(body) <= MAX_PDF_BYTES else None


def _google_doc_text(doc_id):
    """Plain text of a Google Doc shared by link. Fetched without any Canvas cookies. None when the doc is private."""
    import requests
    r = requests.get(f"https://docs.google.com/document/d/{doc_id}/export?format=txt", timeout=GOOGLE_TIMEOUT_S)
    if r.status_code != 200 or "accounts.google.com" in r.url or "text/html" in r.headers.get("content-type", ""):
        return None
    return r.text.strip() or None


def _google_drive_bytes(file_id):
    """Bytes of a Google Drive file shared by link (PDF or Word). Fetched without any Canvas cookies."""
    import requests
    r = requests.get(f"https://drive.google.com/uc?export=download&id={file_id}", timeout=GOOGLE_TIMEOUT_S)
    if r.status_code != 200 or "text/html" in r.headers.get("content-type", "") or len(r.content) > MAX_PDF_BYTES:
        return None
    return r.content


def _document_text(course, name, data):
    try:
        text = syllabus_parser.extract_text(name if name.lower().endswith((".pdf", ".docx")) else "file", data)
    except Exception as e:
        log_error(SCRIPT, type(e).__name__, f"syllabus course {course['id']}", f"cannot read '{name}': {e}")
        return None
    if not text:
        log_error(SCRIPT, "ScannedPdf", f"syllabus course {course['id']}", f"'{name}' has no extractable text (scanned PDF); Mimir cannot read it without OCR")
    return text or None


def _syllabus_sources(page, course):
    """[(label, text)] for everything that looks like this course's syllabus, documents first:
    syllabus files on Canvas (PDF, Word), Google Docs and Drive files linked or embedded, the syllabus page text, pages
    named Syllabus (a Pages page or module item) and the course home page when the syllabus page has nothing.
    Canvas sends the syllabus link to the home page when a course turned the syllabus tab off (ECE129 does), so that
    text is labelled as the home page, and the syllabus file it links to is read too."""
    cid = course["canvas_course_id"]
    found = {"files": {}, "pages": {}, "gdocs": {}, "gdrive": {}}

    def visit(path):
        _goto(page, path)
        if _looks_like_login(page):
            raise SessionExpiredError("Canvas session expired. Run: python -m pollers.canvas_scraper --login")

    def collect(any_google_doc=False):
        for kind, items in classify_links(_links(page), cid, any_google_doc).items():
            for k, v in items.items():
                found[kind].setdefault(k, v)

    texts = []
    visit(f"/courses/{cid}/assignments/syllabus")
    on_syllabus = "/assignments/syllabus" in urlsplit(page.url).path
    body = _visible_text(page, "#course_syllabus") if on_syllabus else ""
    if len(body) >= MIN_PAGE_CHARS:
        texts.append(("Syllabus page", body))
    collect(any_google_doc=on_syllabus)
    for path in (f"/courses/{cid}", f"/courses/{cid}/files", f"/courses/{cid}/modules"):
        visit(path)
        if path == f"/courses/{cid}" and not texts:
            home = _visible_text(page, "#content")
            if SYLLABUS_HINT.search(home) and len(home) >= MIN_PAGE_CHARS:
                texts.append(("Course home page", home))
        collect()
    for path, name in list(found["pages"].items())[:MAX_SYLLABUS_PAGES]:
        visit(path)
        text = _visible_text(page, "#content")
        if len(text) >= MIN_PAGE_CHARS:
            texts.append((f"Page: {name}", text))
        collect(any_google_doc=True)

    docs = []
    for fid, name in list(found["files"].items())[:MAX_PDFS]:
        data = _download_file(page, cid, fid)
        text = _document_text(course, name, data) if data else None
        if text:
            docs.append((f"File: {name}", text))
    for doc_id, name in list(found["gdocs"].items())[:MAX_PDFS]:
        text = _google_doc_text(doc_id)
        if text:
            docs.append((f"Google Doc: {name}", text))
        else:
            log_error(SCRIPT, "GoogleDocPrivate", f"syllabus course {course['id']}", f"'{name}' is not shared by link, Mimir cannot read it")
    for file_id, name in list(found["gdrive"].items())[:MAX_PDFS]:
        data = _google_drive_bytes(file_id)
        text = _document_text(course, name, data) if data else None
        if text:
            docs.append((f"Google Drive: {name}", text))
    return docs + texts


def _combine(course, sources, limit):
    """Joins sources in order, dropping any that would push the text past the parser's limit (logged)."""
    parts, used = [], 0
    for label, body in sources:
        chunk = f"=== {label} ===\n{body}"
        if used + len(chunk) + 2 > limit:
            log_error(SCRIPT, "SyllabusTooLong", f"syllabus course {course['id']}", f"left out '{label}' ({len(body)} characters) to stay under {limit}")
            continue
        parts.append(chunk)
        used += len(chunk) + 2
    return "\n\n".join(parts)


def scrape_syllabus(page, course, cfg=None):
    """Find this course's syllabus (pages, files and linked documents), parse it with DeepSeek into the syllabus_parsed
    JSON, once per change. Checks each course at most every syllabus.recheck_hours (default 24). Returns 1 when a new
    parse was stored. With nothing found, sends one Telegram message per course per quarter."""
    if cfg is None or not llm_parser.enabled(cfg) or not get(cfg, "syllabus.auto_fetch", True):
        return 0
    check_key = f"syllabus_check:{course['id']}"
    age = _check_age_hours(check_key)
    if age is not None and age < int(get(cfg, "syllabus.recheck_hours", 24)):
        return 0
    sources = _syllabus_sources(page, course)
    _mark_parsed(check_key, "checked")
    if not sources:
        quarter = get(cfg, "quarter_label", "") or "this quarter"
        missing_key = f"syllabus_missing:{course['id']}"
        if _page_changed(missing_key, quarter):
            notifier.send_warning(f"No syllabus found on Canvas for {course.get('canvas_course_name')}, so Mimir has no class times "
                                  f"or syllabus dates for it. Upload it on the Syllabus page: http://localhost:5000/syllabus")
            _mark_parsed(missing_key, quarter)
        return 0
    text = _combine(course, sources, syllabus_parser.MAX_CHARS)
    key = f"syllabus:{course['id']}"
    if not _page_changed(key, text):
        return 0
    result = syllabus_parser.parse(cfg, course["id"], text)
    if result["status"] == "ok":
        _mark_parsed(key, text)
        return 1
    if result["status"] == "error" and UNBILLED_FAILURE.search(result["message"] or ""):
        # DeepSeek unreachable or out of credit: nothing was billed, so look again next cycle. A failure DeepSeek billed
        # for (an unusable answer) waits for the normal recheck instead of paying again every 15 minutes.
        execute_query("DELETE FROM scrape_cache WHERE page_key = %s", (check_key,))
    log_error(SCRIPT, "SyllabusParse", f"course {course['id']}", f"{result['status']}: {result['message']}")
    return 0


def run_cycle(cfg):
    path = cookie_file(cfg)
    if not os.path.exists(path):
        log_error(SCRIPT, "NoSession", "run_cycle", f"{path} not found; run: python -m pollers.canvas_scraper --login")
        return "paused"
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        try:
            browser, page = load_session(p, path)
        except SessionExpiredError:
            # UCSC ends every Canvas session 24 hours after sign in; sign in again automatically when credentials are saved
            if not recover_session(p, cfg, path):
                return "auth_failed"
            try:
                browser, page = load_session(p, path)
            except SessionExpiredError as e:
                log_error(SCRIPT, "SessionExpired", "load_session after sign in", str(e))
                return "auth_failed"
        except Exception as e:
            log_error(SCRIPT, type(e).__name__, "load_session", str(e))
            return None
        courses = fetch_all("SELECT id, canvas_course_id, canvas_course_name FROM courses WHERE active = TRUE AND monitor = TRUE AND canvas_course_id > 0 ORDER BY id")
        try:
            for i, course in enumerate(courses):
                for fn in (scrape_announcements, scrape_assignments, scrape_grades, scrape_syllabus):
                    try:
                        fn(page, course, cfg)
                    except SessionExpiredError:
                        raise
                    except llm_parser.LLMParseError as e:   # one bad model answer must not abort the other pages
                        log_error(SCRIPT, "LLMParseError", f"{fn.__name__} course {course['id']}", str(e))
                    except Exception as e:                  # nor may one page that fails (a timeout, a layout change) skip the other courses
                        log_error(SCRIPT, type(e).__name__, f"{fn.__name__} course {course['id']} at {page.url}", str(e))
                if i < len(courses) - 1:
                    time.sleep(2)
        except Exception as e:
            log_error(SCRIPT, type(e).__name__, f"scrape {page.url}", str(e))
            try:
                shot = os.path.join(repo_root(), "logs", f"scraper_error_{datetime.now():%Y%m%d_%H%M%S}.png")
                page.screenshot(path=shot)
            except Exception as e2:
                log_error(SCRIPT, type(e2).__name__, "screenshot", str(e2))
        finally:
            browser.close()
    return None


if __name__ == "__main__":
    if "--login" in sys.argv:
        from playwright.sync_api import sync_playwright
        print("A Chromium window will open on the UCSC Canvas login page.")
        print(f"Log in, approve the Duo Mobile push, and wait for the dashboard. You have {LOGIN_TIMEOUT_S} seconds.")
        try:
            cfg = load_config()
        except Exception:
            cfg = None
        with sync_playwright() as p:
            ok = interactive_login(p, cookie_file(cfg))
        sys.exit(0 if ok else 1)
    print("usage: python -m pollers.canvas_scraper --login")
    sys.exit(1)
