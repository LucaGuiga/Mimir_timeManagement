"""Reminder wording (real names, overdue, no invented numbers or folders) and Grades page statuses (missing beats
Canvas's automatic zero, statuses only move forward). Telegram, Canvas and MySQL are mocked:
    python -m unittest tests.test_reminders_grades -v"""
import os
import sys
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import notifier  # noqa: E402
from pollers import canvas_scraper as cs  # noqa: E402

NOW = datetime(2026, 10, 19, 8, 0)
CFG = {"repo_manager": {"default_branches": ["main"], "default_folders": ["HW", "Labs", "Tests", "Quizzes", "Projects", "Readings"]}}
COURSE = {"repo_name": "coursework", "repo_path_prefix": "Fall-2026/ECE221"}


def body(assignment):
    with mock.patch.object(notifier, "send_warning", return_value=True) as w, mock.patch.object(notifier, "send_critical", return_value=True) as c:
        notifier.send_assignment_reminder(assignment, COURSE, CFG, now=NOW)
    sent = w if w.called else c
    return ("critical" if c.called else "warning"), sent.call_args[0][0]


class Reminders(unittest.TestCase):
    def test_overdue_homework_says_overdue_not_due_in_zero_days(self):
        level, text = body({"title": "Homework-3", "assignment_type": "hw", "assignment_number": 3, "due_at": datetime(2026, 10, 18, 23, 59)})
        self.assertEqual(level, "warning")
        self.assertIn("ECE221: HW 3 (Homework-3) is overdue, it was due Sun Oct 18 23:59 and is not submitted yet", text)
        self.assertNotIn("0 day", text)

    def test_other_items_keep_their_own_name_and_get_no_folder(self):
        level, text = body({"title": "Writing Skills Assignment", "assignment_type": "other", "assignment_number": None,
                            "due_at": datetime(2026, 10, 21, 23, 59)})
        self.assertEqual(text, "ECE221: Writing Skills Assignment is due in 2 days (Wed Oct 21 23:59)")
        self.assertNotIn("HW 1", text)

    def test_due_today_is_critical_with_upload_steps(self):
        level, text = body({"title": "Homework-4", "assignment_type": "hw", "assignment_number": 4, "due_at": datetime(2026, 10, 19, 23, 59)})
        self.assertEqual(level, "critical")
        self.assertIn("is due today at 23:59", text)
        self.assertIn("Upload to: Fall-2026/ECE221/HW/HW4/", text)

    def test_unnumbered_reading_gets_the_folder_only(self):
        level, text = body({"title": "Week 3 reading", "assignment_type": "reading", "assignment_number": None, "due_at": datetime(2026, 10, 20, 23, 59)})
        self.assertIn("Week 3 reading is due tomorrow at 23:59", text)
        self.assertIn("Upload to: Fall-2026/ECE221/Readings/ in coursework", text)
        self.assertNotIn("Git commands", text)


class Grades(unittest.TestCase):
    def run_grades(self, rows, known):
        page = mock.Mock(url="https://canvas.ucsc.edu/courses/97243/grades")
        writes = []
        with mock.patch.object(cs, "_goto"), mock.patch.object(cs, "_looks_like_login", return_value=False), \
             mock.patch.object(cs, "read_grade_rows", return_value=rows), mock.patch.object(cs, "fetch_all", return_value=known), \
             mock.patch.object(cs, "execute_query", side_effect=lambda q, p=None: writes.append((q, p))):
            n = cs.scrape_grades(page, {"id": 3, "canvas_course_id": 97243})
        return n, writes

    def test_statuses_move_forward_and_missing_stays_open(self):
        known = [{"id": 1, "title": "2.8 Week 2 Short Essay", "status": "open", "grade_percent": None},
                 {"id": 2, "title": "3.8 Week 3 Documentary", "status": "open", "grade_percent": None},
                 {"id": 3, "title": "1.6 Week 1 Video", "status": "graded", "grade_percent": 100.0},
                 {"id": 4, "title": "\U0001f422 2.7 Group Discussion C", "status": "open", "grade_percent": None}]
        rows = [{"title": "2.8 Week 2 Short Essay", "graded": True, "submitted": True, "missing": False, "late": False, "percent": 100.0},
                {"title": "3.8 Week 3 Documentary", "graded": False, "submitted": False, "missing": True, "late": False, "percent": None},
                {"title": "1.6 Week 1 Video", "graded": False, "submitted": True, "missing": False, "late": False, "percent": None},
                {"title": "\U0001f422 2.7 Group Discussion C", "graded": False, "submitted": True, "missing": False, "late": False, "percent": None}]
        n, writes = self.run_grades(rows, known)
        self.assertEqual(n, 2)
        by_id = {p[-1]: (q, p) for q, p in writes}
        self.assertIn("status = %s", by_id[1][0]); self.assertEqual(by_id[1][1][:2], ["graded", 100.0])
        self.assertEqual(by_id[4][1][0], "submitted")            # emoji titles still match
        self.assertNotIn(2, by_id)                               # missing: left open for you to do
        self.assertNotIn(3, by_id)                               # never moved backwards

    def test_assignments_page_redirect_is_not_misread(self):
        page = mock.Mock(url="https://canvas.ucsc.edu/courses/97243")
        with mock.patch.object(cs, "_goto"):
            self.assertEqual(cs.scrape_assignments(page, {"id": 3, "canvas_course_id": 97243}), 0)
        page.locator.assert_not_called()


if __name__ == "__main__":
    unittest.main()
