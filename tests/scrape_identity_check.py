"""Pulls real Canvas pages with the saved scraper session, prints everything, and reports where your identity shows up
and whether the anonymiser removes it.

Your identity is read from privacy.identity in config/config.yaml (never hard coded here). Extra strings can be added
with --needle. Run after `python pollers/canvas_scraper.py --login`:
    python tests/scrape_identity_check.py
    python tests/scrape_identity_check.py --no-dump                 # findings only
    python tests/scrape_identity_check.py --course 12345 --course 67890
    python tests/scrape_identity_check.py --needle "Some Other Spelling"

Output is saved to run/identity_check_<timestamp>.txt (gitignored). It contains personal data; do not share it.
Exit code: 0 clean, 2 anonymiser leaked, 1 could not run."""
import argparse
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import llm_parser                                   # noqa: E402
from core.config import load_config, repo_root                # noqa: E402
from pollers import canvas_scraper as cs                      # noqa: E402

CONTEXT = 50


def needles_from_identity(ident):
    vals = [ident.get("full_name"), ident.get("first_name"), ident.get("last_name"), ident.get("student_id")]
    vals += list(ident.get("emails") or []) + list(ident.get("usernames") or [])
    return sorted({str(v).strip() for v in vals if v and str(v).strip()}, key=lambda s: (-len(s), s))


def find(text, needle):
    """Substring, case-insensitive. Deliberately stricter than the anonymiser so glued or encoded forms show up."""
    return list(re.finditer(re.escape(needle), text, re.I))


def snippet(text, m):
    return text[max(0, m.start() - CONTEXT):m.end() + CONTEXT].replace("\n", " / ")


class Report:
    def __init__(self, path):
        self.f = open(path, "w", encoding="utf-8")

    def out(self, line=""):
        print(line)
        self.f.write(line + "\n")


def check_page(rep, label, text, cfg, needles, dump):
    rep.out("=" * 78)
    rep.out(f"PAGE: {label}   ({len(text)} characters)")
    rep.out("=" * 78)
    if dump:
        rep.out(text)
        rep.out("-" * 78)
    rep.out("RAW PAGE, identity matches:")
    any_raw = False
    for n in needles:
        hits = find(text, n)
        if hits:
            any_raw = True
            rep.out(f"  FOUND  {n!r}: {len(hits)}x")
            for m in hits[:3]:
                rep.out(f"         ...{snippet(text, m)}...")
        else:
            rep.out(f"  none   {n!r}")
    anon = llm_parser.anonymize(text, cfg)
    leaks = [(n, len(find(anon, n))) for n in needles if find(anon, n)]
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
    ap.add_argument("--needle", action="append", default=[], help="extra string to look for (repeatable)")
    ap.add_argument("--no-dump", action="store_true")
    args = ap.parse_args()

    try:
        cfg = load_config()
    except Exception as e:
        print(f"cannot load config/config.yaml ({e}). Fill in privacy.identity first.")
        return 1
    ident = (cfg.get("privacy") or {}).get("identity") or {}
    needles = needles_from_identity(ident)
    needles += [n for n in args.needle if n not in needles]
    if not needles:
        print("privacy.identity in config/config.yaml is empty and no --needle given: nothing to look for.")
        return 1
    cookie = cs.cookie_file(cfg)
    if not os.path.exists(cookie):
        print(f"no saved session at {cookie}. Run: python pollers/canvas_scraper.py --login")
        return 1

    run_dir = os.path.join(repo_root(), "run")
    os.makedirs(run_dir, exist_ok=True)
    path = os.path.join(run_dir, f"identity_check_{datetime.now():%Y%m%d_%H%M%S}.txt")
    rep = Report(path)
    rep.out(f"Identity check {datetime.now():%Y-%m-%d %H:%M}. Looking for {len(needles)} strings from privacy.identity"
            f"{' plus --needle values' if args.needle else ''}.")
    if not llm_parser.enabled({**cfg, "deepseek": {**(cfg.get("deepseek") or {}), "enabled": True, "api_key": "x"}}):
        rep.out("WARNING: privacy.identity has no full_name (or first and last name); DeepSeek would stay disabled.")

    from playwright.sync_api import sync_playwright
    summary, course_ids = [], list(args.course)

    def visit(page, route):
        # plain navigation: does not touch the database or poll_metrics
        page.goto(cs.BASE_URL + route, wait_until="networkidle")
        return page.inner_text("body")

    with sync_playwright() as p:
        try:
            browser, page = cs.load_session(p, cookie)
        except cs.SessionExpiredError as e:
            rep.out(f"SESSION EXPIRED: {e}")
            return 1
        try:
            for label, route in (("dashboard", "/"), ("course list", "/courses")):
                text = visit(page, route)
                if route == "/":
                    hrefs = page.eval_on_selector_all("a[href*='/courses/']", "els => els.map(e => e.getAttribute('href'))")
                    course_ids += [int(m.group(1)) for h in hrefs for m in [re.search(r"^/courses/(\d+)(?:/|$)", h or "")] if m]
                summary.append((label, *check_page(rep, label, text, cfg, needles, not args.no_dump)))
            for cid in dict.fromkeys(course_ids):
                for kind in ("assignments", "announcements", "grades", "assignments/syllabus"):
                    label = f"course {cid} {kind}"
                    try:
                        text = visit(page, f"/courses/{cid}/{kind}")
                    except Exception as e:
                        rep.out(f"PAGE: {label}: failed ({type(e).__name__}: {e})")
                        continue
                    summary.append((label, *check_page(rep, label, text, cfg, needles, not args.no_dump)))
        finally:
            browser.close()

    rep.out("")
    rep.out("SUMMARY")
    rep.out("-" * 78)
    for label, raw, leaks in summary:
        rep.out(f"{label:<40} identity on page: {'YES' if raw else 'no ':<4}  leaks after anonymising: {'YES' if leaks else 'no'}")
    leaking = [s for s in summary if s[2]]
    rep.out("")
    rep.out("RESULT: " + ("ANONYMISER LEAKS. Add the leaked spelling to privacy.identity or fix core/llm_parser.py before enabling DeepSeek."
                          if leaking else "anonymiser removed every listed identity string from every page."))
    rep.out(f"Saved to {path} (contains personal data, do not commit)")
    return 2 if leaking else 0


if __name__ == "__main__":
    sys.exit(main())
