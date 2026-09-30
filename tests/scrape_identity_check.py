"""Pulls real Canvas pages with the saved scraper session, prints everything, and reports where your identity shows up.

Run after `python pollers/canvas_scraper.py --login`:
    python tests/scrape_identity_check.py            # dashboard, course list, every course you have in the DB, plus any found on the dashboard
    python tests/scrape_identity_check.py --no-dump  # findings only, without the full page text
    python tests/scrape_identity_check.py --course 12345 --course 67890

Also runs the DeepSeek anonymiser over each page and reports anything that would still leak.
Output is saved to run/identity_check_<timestamp>.txt. It contains personal data, so do not commit or share it."""
import argparse
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import llm_parser                                   # noqa: E402
from core.config import load_config, repo_root                # noqa: E402
from pollers import canvas_scraper as cs                      # noqa: E402

NEEDLES = ["Luca Guiga", "Luca", "lguiga@ucsc.edu", "lguiga", "2084136"]   # longest first so overlaps read clearly
CONTEXT = 50


def find(text, needle):
    return [m for m in re.finditer(r"(?<![A-Za-z0-9])" + re.escape(needle) + r"(?![A-Za-z0-9])", text, re.I)]


def snippet(text, m):
    return text[max(0, m.start() - CONTEXT):m.end() + CONTEXT].replace("\n", " ⏎ ")


class Report:
    def __init__(self, path):
        self.f = open(path, "w", encoding="utf-8")

    def out(self, line=""):
        print(line)
        self.f.write(line + "\n")


def check_page(rep, label, text, anon_cfg, dump):
    rep.out("=" * 78)
    rep.out(f"PAGE: {label}   ({len(text)} characters)")
    rep.out("=" * 78)
    if dump:
        rep.out(text)
        rep.out("-" * 78)
    rep.out("RAW PAGE, identity matches:")
    any_raw = False
    for n in NEEDLES:
        hits = find(text, n)
        if hits:
            any_raw = True
            rep.out(f"  FOUND  {n!r}: {len(hits)}x")
            for m in hits[:3]:
                rep.out(f"         ...{snippet(text, m)}...")
        else:
            rep.out(f"  none   {n!r}")
    anon = llm_parser.anonymize(text, anon_cfg)
    leaks = [(n, len(find(anon, n))) for n in NEEDLES if find(anon, n)]
    rep.out("AFTER ANONYMISING (what DeepSeek would receive):")
    if leaks:
        for n, c in leaks:
            rep.out(f"  LEAK   {n!r} still appears {c}x")
    else:
        rep.out("  clean, none of the identity strings remain")
    return any_raw, leaks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--course", action="append", type=int, default=[], help="Canvas course id (repeatable)")
    ap.add_argument("--no-dump", action="store_true")
    args = ap.parse_args()

    try:
        cfg = load_config()
    except Exception as e:
        print(f"note: no usable config ({e}); using built in defaults")
        cfg = {}
    # anonymiser test uses the identity from THIS script, so it checks the method, not what you typed into the config
    anon_cfg = {"privacy": {"identity": {"full_name": "Luca Guiga", "first_name": "Luca", "last_name": "Guiga",
                                         "emails": ["lguiga@ucsc.edu"], "student_id": "2084136", "usernames": ["lguiga"]}}}
    cfg_identity = (cfg.get("privacy") or {}).get("identity") or {}
    cookie = cs.cookie_file(cfg or None)
    if not os.path.exists(cookie):
        print(f"no saved session at {cookie}. Run: python pollers/canvas_scraper.py --login")
        return 1

    run_dir = os.path.join(repo_root(), "run")
    os.makedirs(run_dir, exist_ok=True)
    path = os.path.join(run_dir, f"identity_check_{datetime.now():%Y%m%d_%H%M%S}.txt")
    rep = Report(path)
    rep.out(f"Identity check {datetime.now():%Y-%m-%d %H:%M}. Looking for: {', '.join(NEEDLES)}")

    course_ids = list(args.course)
    try:
        from db.db import fetch_all
        course_ids += [r["canvas_course_id"] for r in fetch_all("SELECT canvas_course_id FROM courses WHERE canvas_course_id > 0")]
    except Exception as e:
        rep.out(f"note: could not read courses from the database ({type(e).__name__}); using dashboard links and --course only")

    from playwright.sync_api import sync_playwright
    summary = []
    with sync_playwright() as p:
        try:
            browser, page = cs.load_session(p, cookie)
        except cs.SessionExpiredError as e:
            rep.out(f"SESSION EXPIRED: {e}")
            return 1
        try:
            pages = [("dashboard", "/"), ("course list", "/courses")]
            for label, route in pages:
                cs._goto(page, route)
                text = page.inner_text("body")
                if route == "/":
                    for href in page.eval_on_selector_all("a[href*='/courses/']", "els => els.map(e => e.getAttribute('href'))"):
                        m = re.search(r"/courses/(\d+)", href or "")
                        if m:
                            course_ids.append(int(m.group(1)))
                summary.append((label, *check_page(rep, label, text, anon_cfg, not args.no_dump)))
            for cid in dict.fromkeys(course_ids):
                for kind in ("assignments", "announcements", "grades"):
                    label = f"course {cid} {kind}"
                    try:
                        cs._goto(page, f"/courses/{cid}/{kind}")
                        text = page.inner_text("body")
                    except Exception as e:
                        rep.out(f"PAGE: {label}: failed ({type(e).__name__}: {e})")
                        continue
                    summary.append((label, *check_page(rep, label, text, anon_cfg, not args.no_dump)))
        finally:
            browser.close()

    rep.out("")
    rep.out("SUMMARY")
    rep.out("-" * 78)
    for label, raw, leaks in summary:
        rep.out(f"{label:<40} identity on page: {'YES' if raw else 'no ':<4}  leaks after anonymising: {'YES' if leaks else 'no'}")
    leaking = [s for s in summary if s[2]]
    rep.out("")
    rep.out("RESULT: " + ("ANONYMISER LEAKS, fix privacy.identity / llm_parser before enabling DeepSeek." if leaking
                          else "anonymiser removed every listed identity string from every page."))
    missing = [k for k in ("full_name", "first_name", "last_name", "student_id") if not cfg_identity.get(k)]
    if not cfg_identity.get("emails") or not cfg_identity.get("usernames"):
        missing += [k for k in ("emails", "usernames") if not cfg_identity.get(k)]
    if missing:
        rep.out(f"WARNING: privacy.identity in your config.yaml is missing: {', '.join(missing)}")
    rep.out(f"Saved to {path} (contains personal data, do not commit)")
    return 2 if leaking else 0


if __name__ == "__main__":
    sys.exit(main())
