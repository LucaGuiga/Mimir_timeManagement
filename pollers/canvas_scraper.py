"""Playwright scraper for Canvas announcements, submission status, grades, and available dates.
Uses cookies saved by `python pollers/canvas_scraper.py --login` (headed, Duo Mobile completed by hand)."""
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


def _looks_like_login(page):
    try:
        if page.locator("input[type=password], form[action*='login'], #login_form").count():
            return True
    except Exception:
        pass
    return "log in" in (page.title() or "").lower() or "login" in page.url.lower()


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
            raise SessionExpiredError("Canvas session expired. Run: python pollers/canvas_scraper.py --login")
        return browser, page
    except Exception:
        browser.close()
        raise


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
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(context.cookies(), f)
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
        raise SessionExpiredError("Canvas session expired. Run: python pollers/canvas_scraper.py --login")
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


SYLLABUS_HINT = re.compile(r"syllabus", re.I)
MAX_PDF_BYTES = 15 * 1024 * 1024
MAX_PDFS = 3
MIN_PAGE_CHARS = 300           # below this a syllabus page is treated as empty and the course home page is used too


def _check_age_hours(key):
    row = fetch_one("SELECT TIMESTAMPDIFF(HOUR, updated_at, NOW()) AS h FROM scrape_cache WHERE page_key = %s", (key,))
    return None if not row else row["h"]


def _visible_text(page, selector):
    loc = page.locator(selector)
    return loc.first.inner_text().strip() if loc.count() else ""


def _syllabus_file_links(page):
    """(file_id, name) for same site file links on the current page whose text or address mentions the syllabus."""
    host = urlsplit(BASE_URL).netloc
    found = {}
    pairs = page.eval_on_selector_all("a[href*='/files/'], a[href$='.pdf']",
                                      "els => els.map(e => [e.getAttribute('href') || '', (e.innerText || e.getAttribute('title') || '').trim()])")
    for href, text in pairs:
        parts = urlsplit(href)
        if parts.netloc and parts.netloc != host:
            continue                                   # never send the Canvas login cookies to another site
        m = re.search(r"/files/(\d+)", parts.path)
        if m and (SYLLABUS_HINT.search(text) or SYLLABUS_HINT.search(parts.path)):
            found.setdefault(int(m.group(1)), text or f"file {m.group(1)}")
    return list(found.items())


def _download_pdf(page, course_id, file_id):
    """Bytes of a PDF file using the logged in browser session, or None if it is not a PDF or too big."""
    r = page.context.request.get(f"{BASE_URL}/courses/{course_id}/files/{file_id}/download?download_frd=1", timeout=NAV_TIMEOUT_MS)
    if r.status != 200:
        return None
    body = r.body()
    return body if body[:5] == b"%PDF-" and len(body) <= MAX_PDF_BYTES else None


def _syllabus_sources(page, course):
    """[(label, text)]: the course syllabus page text (HTML syllabi), the home page when that is empty, and any syllabus PDFs."""
    cid = course["canvas_course_id"]
    sources, files = [], {}

    def visit(path):
        _goto(page, path)
        if _looks_like_login(page):
            raise SessionExpiredError("Canvas session expired. Run: python pollers/canvas_scraper.py --login")
        for fid, name in _syllabus_file_links(page):
            files.setdefault(fid, name)

    visit(f"/courses/{cid}/assignments/syllabus")
    body = _visible_text(page, "#course_syllabus")
    pages = [("Syllabus page", body)] if len(body) >= MIN_PAGE_CHARS else []
    for path in (f"/courses/{cid}", f"/courses/{cid}/files", f"/courses/{cid}/modules"):
        visit(path)
        if not pages and path == f"/courses/{cid}":
            home = _visible_text(page, "#content")
            if SYLLABUS_HINT.search(home) and len(home) >= MIN_PAGE_CHARS:
                pages = [("Course home page", home)]
    for fid, name in list(files.items())[:MAX_PDFS]:
        data = _download_pdf(page, cid, fid)
        if data is None:
            continue
        text = syllabus_parser.pdf_text(data)
        if text:
            sources.append((f"File: {name}", text))
        else:
            log_error(SCRIPT, "ScannedPdf", f"syllabus course {course['id']}", f"'{name}' has no extractable text (scanned PDF); Mimir cannot read it without OCR")
    return sources + pages


def scrape_syllabus(page, course, cfg=None):
    """Find this course's syllabus (page and/or PDFs), parse it with DeepSeek into the syllabus_parsed JSON, once per change.
    Checks each course at most every syllabus.recheck_hours (default 24). Returns 1 when a new parse was stored."""
    if cfg is None or not llm_parser.enabled(cfg) or not get(cfg, "syllabus.auto_fetch", True):
        return 0
    age = _check_age_hours(f"syllabus_check:{course['id']}")
    if age is not None and age < int(get(cfg, "syllabus.recheck_hours", 24)):
        return 0
    sources = _syllabus_sources(page, course)
    _mark_parsed(f"syllabus_check:{course['id']}", "checked")
    if not sources:
        return 0
    text = "\n\n".join(f"=== {label} ===\n{body}" for label, body in sources)
    key = f"syllabus:{course['id']}"
    if not _page_changed(key, text):
        return 0
    result = syllabus_parser.parse(cfg, course["id"], text)
    if result["status"] == "ok":
        _mark_parsed(key, text)
        return 1
    log_error(SCRIPT, "SyllabusParse", f"course {course['id']}", f"{result['status']}: {result['message']}")
    return 0


def run_cycle(cfg):
    path = cookie_file(cfg)
    if not os.path.exists(path):
        log_error(SCRIPT, "NoSession", "run_cycle", f"{path} not found; run: python pollers/canvas_scraper.py --login")
        return "paused"
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        try:
            browser, page = load_session(p, path)
        except SessionExpiredError as e:
            log_error(SCRIPT, "SessionExpired", "load_session", str(e))
            notifier.send_session_expired()
            return "auth_failed"
        except Exception as e:
            log_error(SCRIPT, type(e).__name__, "load_session", str(e))
            return None
        courses = fetch_all("SELECT id, canvas_course_id, canvas_course_name FROM courses WHERE active = TRUE AND monitor = TRUE AND canvas_course_id > 0 ORDER BY id")
        try:
            for i, course in enumerate(courses):
                for fn in (scrape_announcements, scrape_assignments, scrape_syllabus):
                    try:
                        fn(page, course, cfg)
                    except SessionExpiredError:
                        raise
                    except llm_parser.LLMParseError as e:   # one bad model answer must not abort the other pages
                        log_error(SCRIPT, "LLMParseError", f"{fn.__name__} course {course['id']}", str(e))
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
    print("usage: python pollers/canvas_scraper.py --login")
    sys.exit(1)
