"""Run 11 tests: syllabus sources (login check, link finding, Word files), two pass parsing with class times and
Telegram gaps, per class workload, the study planner, and silent unselected iCal courses.

Everything outside Python is mocked (Canvas, DeepSeek, Telegram, MySQL), so no database or network is needed:
    python -m unittest tests.test_planner_syllabus -v"""
import io
import json
import os
import sys
import unittest
import zipfile
from datetime import date, datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import planner, syllabus_parser as sp, workload  # noqa: E402
from pollers import canvas_scraper as cs  # noqa: E402

CFG = {"quarter_label": "Fall 2026",
       "difficulty_presets": {"hw": 2, "quiz": 2, "test": 5, "lab": 3, "project_milestone": 4, "reading": 1, "other": 2},
       "stress": {"hour_scale_subjects": {"ECE": 1.0}, "hour_scale_default": 0.05},
       "deepseek": {"enabled": True, "api_key": "k", "model": "deepseek-flash"},
       "privacy": {"identity": {"full_name": "Luca Guiga"}}}


class LoginCheck(unittest.TestCase):
    def test_title_words_only(self):
        self.assertIsNone(cs.LOGIN_TITLE.search("Syllabus for Advanced Analog Integrated Circuits"))
        self.assertIsNotNone(cs.LOGIN_TITLE.search("Log In to Canvas"))
        self.assertIsNotNone(cs.LOGIN_TITLE.search("Login | UCSC"))


class Links(unittest.TestCase):
    def test_ece129_syllabus_file_on_home_page(self):
        found = cs.classify_links([("/courses/95558/files/13310619", "Capstone Project I, II, III General Syllabus"),
                                   ("/courses/95558/files/13310619/download", "Download Capstone Project I, II, III General Syllabus"),
                                   ("/courses/95558/files/1", "Lecture 3 slides")], 95558)
        self.assertEqual(found["files"], {13310619: "Capstone Project I, II, III General Syllabus"})

    def test_thea80_google_doc_embedded_without_the_word(self):
        doc = "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdEF/pre"
        self.assertEqual(cs.classify_links([(doc, "")], 97243)["gdocs"], {})                     # elsewhere it needs the word
        self.assertIn("1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdEF", cs.classify_links([(doc, "")], 97243, any_google_doc=True)["gdocs"])

    def test_syllabus_module_item_and_page_followed(self):
        found = cs.classify_links([("/courses/97243/modules/items/2056996", "Syllabus Fall 2026"),
                                   ("/courses/97243/pages/week-3", "Week 3")], 97243)
        self.assertEqual(list(found["pages"]), ["/courses/97243/modules/items/2056996"])

    def test_other_sites_never_get_canvas_links(self):
        found = cs.classify_links([("https://evil.example.com/files/5/syllabus.pdf", "Syllabus")], 1)
        self.assertEqual(found["files"], {})

    def test_combine_keeps_documents_first_and_respects_limit(self):
        with mock.patch.object(cs, "log_error") as log:
            text = cs._combine({"id": 1}, [("File: a", "x" * 50), ("Syllabus page", "y" * 100)], 80)
        self.assertIn("=== File: a ===", text)
        self.assertNotIn("Syllabus page", text)
        log.assert_called_once()


class WordFiles(unittest.TestCase):
    def test_docx_text(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", '<w:document><w:body><w:p><w:r><w:t>Lecture MWF 10:40&amp;11:45</w:t></w:r></w:p>'
                                            '<w:p><w:r><w:t>Midterm Oct 29</w:t></w:r></w:p></w:body></w:document>')
        self.assertEqual(sp.extract_text("syllabus.docx", buf.getvalue()), "Lecture MWF 10:40&11:45\nMidterm Oct 29")


class Meetings(unittest.TestCase):
    def test_days_normalised(self):
        self.assertEqual(sp.normalize_days("MWF"), "MWF")
        self.assertEqual(sp.normalize_days("TR"), "TR")
        self.assertEqual(sp.normalize_days("Tue/Thu"), "TR")          # Thu is Thursday, not Sunday
        self.assertEqual(sp.normalize_days("Monday, Wednesday"), "MW")

    def test_validate_meetings(self):
        out, errors = sp._validate_meetings([{"kind": "lecture", "days": "MWF", "start": "9:20", "end": "10:25", "location": " BE 152 "},
                                             {"kind": "lab", "days": "R", "start": "14:00", "end": "13:00"}])
        self.assertEqual(out, [{"kind": "lecture", "days": "MWF", "start": "09:20", "end": "10:25", "location": "BE 152"}])
        self.assertEqual(len(errors), 1)


class TwoPassParse(unittest.TestCase):
    def run_parse(self, draft, verified):
        calls, sent, inserts = [], [], []

        def json_call(cfg, kind, system, text, max_tokens=None, max_chars=None):
            calls.append((kind, text))
            return (draft if kind == "syllabus" else verified), 0.001

        course = {"id": 2, "canvas_course_name": "ECE-221-01", "ical_course_code": "ECE-221-01"}
        with mock.patch.object(sp, "_course", return_value=course), mock.patch.object(sp.llm_parser, "enabled", return_value=True), \
             mock.patch.object(sp.llm_parser, "json_call", json_call), mock.patch.object(sp, "fetch_all", return_value=[]), \
             mock.patch.object(sp, "fetch_one", return_value=None), \
             mock.patch.object(sp, "execute_query", side_effect=lambda q, p=None: inserts.append((q, p)) or 7), \
             mock.patch("core.notifier.send_warning", side_effect=lambda t: sent.append(t)), \
             mock.patch("core.planner.plan_safely") as replan:
            r = sp.parse(CFG, 2, "=== Syllabus page ===\nLectures TuTh 1:30-3:05 ...")
        return r, calls, sent, inserts, replan

    def test_second_pass_checks_the_draft_and_reports_conflicts(self):
        draft = {"professor_name": "X", "items": [{"title": "Midterm", "assignment_type": "test", "assignment_number": None,
                                                   "predicted_open": None, "predicted_due": "2026-10-29", "notes": ""}], "meetings": []}
        verified = dict(draft, meetings=[{"kind": "lecture", "days": "TR", "start": "13:30", "end": "15:05", "location": None}],
                        conflicts=["Syllabus page says the midterm is 2026-10-29, the PDF says 2026-10-30"])
        r, calls, sent, inserts, replan = self.run_parse(draft, verified)
        self.assertEqual(r["status"], "ok", r)
        self.assertEqual([k for k, _ in calls], ["syllabus", "syllabus_verify"])
        self.assertIn("DRAFT:", calls[1][1])
        self.assertTrue(any("INSERT INTO class_meetings" in q and p[2] == "TR" for q, p in inserts))
        self.assertEqual(len(sent), 1)
        self.assertIn("sources disagree", sent[0])
        self.assertNotIn("no class times", sent[0])
        replan.assert_called_once()

    def test_telegram_when_no_class_times(self):
        empty = {"professor_name": "X", "items": [], "meetings": [], "conflicts": []}
        r, calls, sent, inserts, _ = self.run_parse(empty, empty)
        self.assertEqual(r["status"], "ok", r)
        self.assertEqual(len(sent), 1)
        self.assertIn("has no class times and no assignments", sent[0])


class Workload(unittest.TestCase):
    def test_ece_full_other_subjects_five_percent_and_per_class_override(self):
        courses = [{"id": 1, "ical_course_code": "ECE-221-01", "hour_scale": None},
                   {"id": 3, "ical_course_code": "THEA-80U-01", "hour_scale": None},
                   {"id": 4, "ical_course_code": "MATH-19A-01", "hour_scale": 0.5}]
        p = workload.Presets(CFG, courses=courses, rows=[{"course_id": 3, "assignment_type": "reading", "difficulty": 3}])
        self.assertAlmostEqual(p.hours(1, "test"), workload.hours_for(5), places=6)          # ECE at 100%
        self.assertAlmostEqual(p.hours(3, "hw"), workload.hours_for(2) * 0.05, places=6)     # other subjects at 5%
        self.assertAlmostEqual(p.hours(3, "reading"), workload.hours_for(3) * 0.05, places=6)  # this class's own difficulty
        self.assertAlmostEqual(p.hours(4, "hw"), workload.hours_for(2) * 0.5, places=6)      # hour scale set by hand


ST = {"horizon_days": 7, "day_start": 8 * 60, "latest_end": 22 * 60, "hard_latest_end": 23 * 60 + 45,
      "min_block": 30, "max_block": 180, "break": 15, "max_per_day": 8 * 60}
TODAY = date(2026, 10, 19)   # a Monday


class Planner(unittest.TestCase):
    def test_class_blocks_on_their_weekdays(self):
        m = [{"course_id": 2, "days": "TR", "start_time": "13:30", "end_time": "15:05", "label": "ECE-221-01 lecture"}]
        days = [TODAY + timedelta(days=i) for i in range(7)]
        got = planner.class_blocks(m, days)
        self.assertEqual([d.strftime("%a") for d in days if got[d]], ["Tue", "Thu"])
        self.assertEqual((got[TODAY + timedelta(days=1)][0]["start"], got[TODAY + timedelta(days=1)][0]["end"]), (13 * 60 + 30, 15 * 60 + 5))

    def plan(self, assignments, busy=None, now_min=0, st=ST):
        out = planner.study_blocks(assignments, busy or {}, TODAY, now_min, st)
        return {d: [(b["start"], b["end"]) for b in v] for d, v in out.items() if v}, out

    def test_hours_spread_over_days_before_due_and_sized_by_workload(self):
        big = {"id": 1, "course_id": 2, "label": "Study: ECE midterm", "due_at": datetime(2026, 10, 23, 23, 59), "hours": 20.0}
        small = {"id": 2, "course_id": 3, "label": "Study: THEA reading", "due_at": datetime(2026, 10, 23, 23, 59), "hours": 0.11}
        spans, _ = self.plan([big, small])
        total = {aid: 0 for aid in (1, 2)}
        for d, v in planner.study_blocks([big, small], {}, TODAY, 0, ST).items():
            for b in v:
                total[b["assignment_id"]] += b["end"] - b["start"]
        self.assertEqual(total[1], 20 * 60)          # all 20 hours placed, Monday to Friday
        self.assertEqual(total[2], 15)               # 7 minutes of reading becomes one 15 minute block, not 90
        self.assertTrue(all(e <= 22 * 60 for v in spans.values() for _, e in v))
        self.assertTrue(all(e - s <= 180 for v in spans.values() for s, e in v))

    def test_avoids_classes_and_keeps_a_break(self):
        a = {"id": 1, "course_id": 2, "label": "Study", "due_at": datetime(2026, 10, 20, 23, 59), "hours": 2.0}
        spans, _ = self.plan([a], busy={TODAY: [(8 * 60, 9 * 60)]})
        s, e = spans[TODAY][0]
        self.assertGreaterEqual(s, 9 * 60 + 15)

    def test_after_22_only_when_necessary(self):
        busy = {TODAY: [(8 * 60, 21 * 60)]}   # Monday is full until 21:00
        due_tonight = {"id": 1, "course_id": 2, "label": "Study", "due_at": datetime(2026, 10, 19, 23, 59), "hours": 2.0}
        spans, _ = self.plan([due_tonight], busy=busy)
        self.assertTrue(any(e > 22 * 60 for _, e in spans[TODAY]))      # due tonight: it goes past 22:00
        later = {"id": 2, "course_id": 2, "label": "Study", "due_at": datetime(2026, 11, 30, 23, 59), "hours": 2.0}
        spans, _ = self.plan([later], busy=busy)
        self.assertTrue(all(e <= 22 * 60 for _, e in spans.get(TODAY, [])))   # due in weeks: it waits

    def test_long_sessions_are_split(self):
        a = {"id": 1, "course_id": 2, "label": "Study", "due_at": datetime(2026, 10, 19, 23, 59), "hours": 5.0}
        spans, _ = self.plan([a])
        self.assertTrue(len(spans[TODAY]) >= 2 and all(e - s <= 180 for s, e in spans[TODAY]))

    def test_done_work_is_not_planned_again(self):
        a = {"id": 1, "course_id": 2, "label": "Study", "due_at": datetime(2026, 10, 20, 23, 59), "hours": 2.0, "done": 2.0}
        spans, _ = self.plan([a])
        self.assertEqual(spans, {})


class ICalSilence(unittest.TestCase):
    def test_unselected_course_is_skipped_without_a_warning(self):
        from pollers import ical_poller as ip
        feed = (b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:x1\r\nSUMMARY:Homework 1 [ECE-141-01]\r\n"
                b"DTSTART:20261020T235900Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        with mock.patch.object(ip, "_fetch", return_value=feed), mock.patch.object(ip, "fetch_all", return_value=[]), \
             mock.patch.object(ip, "log_error") as log, mock.patch.object(ip, "_upsert_event") as upsert:
            ip.run_cycle({"ical": {"feed_url": "x"}})
        log.assert_not_called()      # the feed parsed and the event was matched, silently, against no selected course
        upsert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
