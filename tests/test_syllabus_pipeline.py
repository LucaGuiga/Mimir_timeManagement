"""Syllabus pipeline tests: PDF and HTML syllabi found on (fake) Canvas pages, parsed by (mocked) DeepSeek, stored as JSON.

The scraper tests drive a real headless Chromium against a small local web server that imitates Canvas pages and file
downloads. DeepSeek is mocked. They need a THROWAWAY database, same as the other tests:
    MIMIR_TEST_DB=mimir_test MIMIR_TEST_USER=root MIMIR_TEST_PASSWORD=... python -m unittest tests.test_syllabus_pipeline -v"""
import io
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = os.environ.get("MIMIR_TEST_DB")
CFG = {"_path": "x",
       "deepseek": {"enabled": True, "api_key": "k", "monthly_cap_usd": 10, "price_input_per_m": 1.0, "price_output_per_m": 2.0},
       "privacy": {"identity": {"full_name": "Luca Guiga", "first_name": "Luca", "last_name": "Guiga", "student_id": "2084136"}},
       "syllabus": {"auto_fetch": True, "recheck_hours": 24},
       "mysql": {"host": os.environ.get("MIMIR_TEST_HOST", "127.0.0.1"), "port": int(os.environ.get("MIMIR_TEST_PORT", "3306")),
                 "user": os.environ.get("MIMIR_TEST_USER", "root"), "password": os.environ.get("MIMIR_TEST_PASSWORD", ""), "db": TEST_DB},
       "paths": {"run_dir": "run"}}


def text_pdf(lines):
    """A minimal valid one page PDF that contains the given lines of text."""
    stream = "BT /F1 11 Tf 72 740 Td 14 TL " + " ".join(f"({l}) Tj T*" for l in lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    x = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + "".join(f"{o:010d} 00000 n \n" for o in offs).encode()
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF\n".encode()


def blank_pdf():
    from pypdf import PdfWriter
    w, buf = PdfWriter(), io.BytesIO()
    w.add_blank_page(612, 792)
    w.write(buf)
    return buf.getvalue()


SYLLABUS_LINES = ["ECE 141 Syllabus", "Instructor: Prof Smith", "Student record: Luca Guiga 2084136",
                  "Homework 1 due 2026-10-10", "Midterm exam 2026-11-05"]
HTML_SYLLABUS = ("<p>Welcome to the course. " + "Please read this syllabus carefully. " * 12 + "</p>"
                 "<p>Lab 1 is due 2026-10-12. Quiz 1 is on 2026-10-20.</p>")
HITS = []


def page(title, body):
    return f"<html><head><title>{title}</title></head><body><div id='content'>{body}</div></body></html>".encode()


class FakeCanvas(BaseHTTPRequestHandler):
    html_syllabus = HTML_SYLLABUS

    def log_message(self, *a):
        pass

    def reply(self, body, ctype="text/html"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        HITS.append(path)
        # course 1: syllabus is a PDF linked from the modules page
        if path == "/courses/1/assignments/syllabus":
            return self.reply(page("Syllabus", "<div id='course_syllabus'></div>"))
        if path == "/courses/1/modules":
            return self.reply(page("Modules", "<a href='/courses/1/files/77?wrap=1'>ECE 141 Syllabus.pdf</a> "
                                              "<a href='/courses/1/files/78?wrap=1'>Lecture slides.pdf</a>"))
        if path == "/courses/1/files/77/download":
            return self.reply(text_pdf(SYLLABUS_LINES), "application/pdf")
        # course 2: syllabus is built into the Canvas page
        if path == "/courses/2/assignments/syllabus":
            return self.reply(page("Syllabus", f"<div id='course_syllabus'>{self.html_syllabus}</div>"))
        # course 3: syllabus is a scanned PDF
        if path == "/courses/3/files":
            return self.reply(page("Files", "<a href='/courses/3/files/90?wrap=1'>Syllabus scan.pdf</a>"))
        if path == "/courses/3/files/90/download":
            return self.reply(blank_pdf(), "application/pdf")
        # course 4: only a syllabus link to another website, which must never be fetched with the login cookies
        if path == "/courses/4/modules":
            return self.reply(page("Modules", f"<a href='http://127.0.0.1:{OTHER_PORT[0]}/files/5/x.pdf'>Syllabus.pdf</a>"))
        self.reply(page("Course", "<p>Home</p>"))


OTHER_HITS = []
OTHER_PORT = [0]


class OtherSite(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        OTHER_HITS.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.end_headers()
        self.wfile.write(text_pdf(["should never be read"]))


def start(handler):
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def ds_response(items=None, professor="Prof Smith", usage=None):
    items = items if items is not None else [
        {"title": "Homework 1", "assignment_type": "hw", "assignment_number": 1, "predicted_open": None, "predicted_due": "2026-10-10", "notes": ""},
        {"title": "Midterm", "assignment_type": "test", "assignment_number": None, "predicted_open": None, "predicted_due": "2026-11-05", "notes": ""}]
    r = mock.Mock(status_code=200)
    r.json.return_value = {"choices": [{"message": {"content": json.dumps({"professor_name": professor, "items": items})}}],
                           "usage": usage or {"prompt_tokens": 800, "completion_tokens": 120}}
    return r


class PdfTextTests(unittest.TestCase):
    def test_text_pdf_is_read(self):
        from core import syllabus_parser
        t = syllabus_parser.pdf_text(text_pdf(SYLLABUS_LINES))
        self.assertIn("Homework 1 due 2026-10-10", t)
        self.assertIn("Prof Smith", t)

    def test_scanned_pdf_gives_empty_text(self):
        from core import syllabus_parser
        self.assertEqual(syllabus_parser.pdf_text(blank_pdf()), "")


@unittest.skipUnless(TEST_DB, "set MIMIR_TEST_DB to a throwaway database to run these tests")
class SyllabusPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import core.config as C
        C._cache[os.path.abspath(C.default_config_path())] = CFG
        import db.db as D
        D.apply_schema()
        from pollers import canvas_scraper as cs
        from playwright.sync_api import sync_playwright
        cls.D, cls.cs = D, cs
        cls.canvas, cls.other = start(FakeCanvas), start(OtherSite)
        OTHER_PORT[0] = cls.other.server_address[1]
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cs._launch(cls.pw, headless=True)
        except Exception as first:
            # the installed browser build may not match this Playwright version: try any Chromium already on the machine
            import glob
            found = (glob.glob(os.environ.get("MIMIR_TEST_CHROMIUM", "")) or glob.glob("/opt/pw-browsers/chromium*/chrome-linux*/chrome")
                     or glob.glob("/usr/bin/chromium*") or glob.glob("/usr/bin/google-chrome*"))
            cls.browser = None
            for path in found:
                try:
                    cls.browser = cls.pw.chromium.launch(headless=True, executable_path=path)
                    break
                except Exception:
                    continue
            if cls.browser is None:
                cls.pw.stop()
                raise unittest.SkipTest(f"no Chromium available: {str(first)[:120]}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.canvas.shutdown()
        cls.other.shutdown()

    def setUp(self):
        D = self.D
        for t in ("syllabus_parsed", "assignments", "courses", "scrape_cache", "deepseek_usage", "deepseek_alert", "error_log", "poll_metrics"):
            D.execute_query(f"DELETE FROM {t}")
        self.cid = {}
        for n in (1, 2, 3, 4):
            self.cid[n] = D.execute_query("INSERT INTO courses (canvas_course_id, canvas_course_name) VALUES (%s,%s)", (n, f"Course {n}"))
        self.D.execute_query("INSERT INTO assignments (course_id, canvas_assignment_id, title, assignment_type, assignment_number, status) "
                             "VALUES (%s, 5001, 'Homework 1', 'hw', 1, 'pending')", (self.cid[1],))
        HITS.clear()
        OTHER_HITS.clear()
        FakeCanvas.html_syllabus = HTML_SYLLABUS
        self.sent = []

        def fake_post(url, cfg, system, user, max_tokens=None):
            self.sent.append((system, user))
            return ds_response()
        self.patches = [mock.patch.object(self.cs, "BASE_URL", f"http://127.0.0.1:{self.canvas.server_address[1]}"),
                        mock.patch("core.llm_parser._post", side_effect=fake_post),
                        mock.patch("core.deepseek_usage.notifier.send_warning", return_value=True)]
        for p in self.patches:
            p.start()
        self.context = self.browser.new_context()
        self.page = self.context.new_page()

    def tearDown(self):
        self.context.close()
        for p in self.patches:
            p.stop()

    def run_scrape(self, n, cfg=CFG):
        return self.cs.scrape_syllabus(self.page, {"id": self.cid[n], "canvas_course_id": n, "canvas_course_name": f"Course {n}"}, cfg)

    def rows(self, n):
        return self.D.fetch_all("SELECT * FROM syllabus_parsed WHERE course_id = %s ORDER BY id", (self.cid[n],))

    def test_pdf_syllabus_is_downloaded_parsed_and_stored(self):
        self.assertEqual(self.run_scrape(1), 1)
        row = self.rows(1)[0]
        data = json.loads(row["parsed_json"])
        self.assertEqual(data["professor_name"], "Prof Smith")
        self.assertEqual([i["title"] for i in data["items"]], ["Homework 1", "Midterm"])
        self.assertIn("=== File: ECE 141 Syllabus.pdf ===", row["raw_text"])
        self.assertIn("Homework 1 due 2026-10-10", row["raw_text"])
        self.assertNotIn("Lecture slides", row["raw_text"])                 # only files that mention the syllabus
        self.assertTrue(row["validated"])
        self.assertGreater(float(row["parse_cost_usd"]), 0)

    def test_name_is_swapped_before_sending_and_restored_in_storage(self):
        self.run_scrape(1)
        system, user = self.sent[0]
        for secret in ("Luca Guiga", "Luca", "2084136"):
            self.assertNotIn(secret, system + user)
        self.assertIn("John Doe", user)
        self.assertIn("Luca Guiga", self.rows(1)[0]["raw_text"])           # what we store keeps your real data, it never left the machine

    def test_parsed_dates_are_linked_to_existing_assignments(self):
        self.run_scrape(1)
        a = self.D.fetch_one("SELECT syllabus_predicted_due FROM assignments WHERE canvas_assignment_id = 5001")
        self.assertEqual(a["syllabus_predicted_due"].date().isoformat(), "2026-10-10")

    def test_html_syllabus_on_the_canvas_page_is_parsed_into_the_same_json(self):
        self.assertEqual(self.run_scrape(2), 1)
        row = self.rows(2)[0]
        self.assertIn("=== Syllabus page ===", row["raw_text"])
        self.assertIn("Quiz 1 is on 2026-10-20", row["raw_text"])
        self.assertEqual(set(json.loads(row["parsed_json"])), {"professor_name", "items"})

    def test_tokens_are_tracked_as_deepseek_usage(self):
        self.run_scrape(1)
        t = self.D.fetch_one("SELECT kind, prompt_tokens, completion_tokens FROM deepseek_usage")
        self.assertEqual((t["kind"], t["prompt_tokens"], t["completion_tokens"]), ("syllabus", 800, 120))

    def test_unchanged_syllabus_is_not_parsed_again(self):
        self.run_scrape(2)
        HITS.clear()
        self.assertEqual(self.run_scrape(2), 0)                              # within recheck_hours: no pages are even visited
        self.assertEqual(HITS, [])
        self.D.execute_query("DELETE FROM scrape_cache WHERE page_key = %s", (f"syllabus_check:{self.cid[2]}",))   # a day later
        self.assertEqual(self.run_scrape(2), 0)                              # pages are checked, text unchanged, no model call
        self.assertGreater(len(HITS), 0)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.rows(2)), 1)

    def test_changed_syllabus_is_parsed_again(self):
        self.run_scrape(2)
        FakeCanvas.html_syllabus = HTML_SYLLABUS + "<p>Project 1 is due 2026-11-30.</p>"
        self.D.execute_query("DELETE FROM scrape_cache WHERE page_key = %s", (f"syllabus_check:{self.cid[2]}",))
        self.assertEqual(self.run_scrape(2), 1)
        self.assertEqual(len(self.rows(2)), 2)

    def test_scanned_pdf_is_skipped_with_a_clear_warning(self):
        self.assertEqual(self.run_scrape(3), 0)
        self.assertEqual(self.rows(3), [])
        err = self.D.fetch_one("SELECT raw_message FROM error_log WHERE error_type = 'ScannedPdf'")
        self.assertIn("scanned PDF", err["raw_message"])
        self.assertEqual(self.sent, [])

    def test_links_to_other_sites_are_never_fetched(self):
        self.assertEqual(self.run_scrape(4), 0)
        self.assertEqual(OTHER_HITS, [])

    def test_nothing_happens_when_deepseek_is_off_or_identity_is_empty(self):
        for cfg in ({**CFG, "deepseek": {**CFG["deepseek"], "enabled": False}}, {**CFG, "privacy": {"identity": {}}},
                    {**CFG, "syllabus": {"auto_fetch": False}}):
            HITS.clear()
            self.assertEqual(self.run_scrape(1, cfg), 0)
            self.assertEqual(HITS, [])
        self.assertEqual(self.sent, [])

    def test_failed_parse_is_retried_next_time(self):
        with mock.patch("core.llm_parser._post", return_value=mock.Mock(status_code=500, text="down")):
            self.assertEqual(self.run_scrape(2), 0)
        self.D.execute_query("DELETE FROM scrape_cache WHERE page_key = %s", (f"syllabus_check:{self.cid[2]}",))
        self.assertEqual(self.run_scrape(2), 1)                              # same text, but the failed one was never marked as done

    def test_invalid_json_from_the_model_is_stored_for_review_not_trusted(self):
        bad = ds_response(items=[{"title": "HW", "assignment_type": "banana", "assignment_number": None, "predicted_due": "2026-10-10", "notes": ""}])
        with mock.patch("core.llm_parser._post", return_value=bad):
            self.assertEqual(self.run_scrape(2), 0)
        row = self.rows(2)[0]
        self.assertFalse(row["validated"])
        self.assertIn("validation_errors", json.loads(row["parsed_json"]))


if __name__ == "__main__":
    unittest.main()
