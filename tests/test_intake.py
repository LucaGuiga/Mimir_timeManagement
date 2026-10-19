"""Telegram requests: local draft, DeepSeek answer validation, the two confirmation messages, yes / no, and applying.
Telegram, DeepSeek and MySQL are mocked:
    python -m unittest tests.test_intake -v"""
import json
import os
import sys
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import intake  # noqa: E402

TODAY = date(2026, 10, 19)   # a Monday
CTX = {
    "courses": [{"id": 2, "ical_course_code": "ECE-221-01", "canvas_course_name": "ECE-221-01"},
                {"id": 3, "ical_course_code": "THEA-80U-01", "canvas_course_name": "THEA-80U-01"}],
    "assignments": [{"id": 24, "course_id": 2, "title": "Homework-3", "assignment_type": "hw", "due_at": datetime(2026, 10, 18, 23, 59),
                     "status": "open", "ical_course_code": "ECE-221-01"}],
    "events": [{"id": 90, "date": date(2026, 10, 20), "start_time": timedelta(hours=18), "end_time": timedelta(hours=19),
                "label": "Club meeting", "time_category": "clubs"}],
    "meetings": [{"course_id": 2, "kind": "lecture", "days": "TR", "start_time": timedelta(hours=11, minutes=40),
                  "end_time": timedelta(hours=13, minutes=15), "ical_course_code": "ECE-221-01"}],
}
CFG = {"difficulty_presets": {"hw": 2}, "stress": {"hour_scale_subjects": {"ECE": 1.0}, "hour_scale_default": 0.05},
       "deepseek": {"enabled": True, "api_key": "k"}, "privacy": {"identity": {"full_name": "Luca Guiga"}}}


class LocalDraft(unittest.TestCase):
    def test_event_with_weekday_and_range(self):
        d = intake.draft("dentist Tue 3 to 4pm", TODAY, CTX["courses"])
        self.assertEqual((d["kind"], d["dates"], d["start"], d["end"]), ("add_event", ["2026-10-20"], "15:00", "16:00"))

    def test_assignment_with_course_and_due_time(self):
        d = intake.draft("ECE221 HW4 due Friday 11:59pm", TODAY, CTX["courses"])
        self.assertEqual((d["kind"], d["course_code"], d["dates"], d["start"]), ("add_or_update_assignment", "ECE-221-01", ["2026-10-23"], "23:59"))

    def test_other_kinds(self):
        c = CTX["courses"]
        self.assertEqual(intake.draft("no class Thursday for ece 221", TODAY, c)["kind"], "cancel_class")
        self.assertEqual(intake.draft("club meeting moved to Thursday 6pm", TODAY, c)["kind"], "move_or_update")
        self.assertEqual(intake.draft("done with HW3", TODAY, c)["kind"], "mark_done")
        self.assertEqual(intake.draft("remember to email the TA", TODAY, c)["kind"], "note")
        self.assertEqual(intake.draft("tomorrow at 9", TODAY, c)["dates"], ["2026-10-20"])
        self.assertEqual(intake.draft("Oct 30 noon", TODAY, c)["dates"], ["2026-10-30"])


class Validation(unittest.TestCase):
    def test_good_actions_pass_and_invented_ids_do_not(self):
        data = {"actions": [
            {"type": "add_event", "title": "Dentist", "date": "2026-10-20", "start": "15:00", "end": "16:00", "category": "fixed"},
            {"type": "move_event", "block_id": 90, "date": "2026-10-22", "start": "18:00"},           # no end: one hour
            {"type": "mark_done", "assignment_id": 999},                                                # not in the list
            {"type": "cancel_class", "course_code": "ECE-221-01", "date": "2026-10-22"},
            {"type": "add_assignment", "course_code": "ECE-221-01", "title": "HW4", "assignment_type": "hw", "due_at": "2026-10-23"},
            {"type": "launch_rockets"}], "questions": []}
        actions, questions, problems = intake.validate(data, CTX)
        self.assertEqual([a["type"] for a in actions], ["add_event", "move_event", "cancel_class", "add_assignment"])
        self.assertEqual(actions[1]["end"], "19:00")
        self.assertEqual(actions[3]["due_at"], datetime(2026, 10, 23, 23, 59))
        self.assertEqual(len(problems), 2)


class Messages(unittest.TestCase):
    def run_new(self, answer):
        sent, rows = [], []
        with mock.patch.object(intake, "context", return_value=CTX), mock.patch.object(intake.llm_parser, "enabled", return_value=True), \
             mock.patch.object(intake.llm_parser, "json_call", return_value=(answer, 0.0)) as call, \
             mock.patch.object(intake, "execute_query", side_effect=lambda q, p=None: rows.append((q, p)) or 12), \
             mock.patch.object(intake, "_send", side_effect=lambda t: sent.append(t)), \
             mock.patch("core.workload.fetch_all", return_value=[{"id": 2, "ical_course_code": "ECE-221-01", "hour_scale": None}]):
            intake.new_request(CFG, 5, "dentist Tue 3 to 4pm, and no ECE221 class Thursday", now=datetime(2026, 10, 19, 10, 0))
        return sent, rows, call

    def test_two_messages_verify_then_calendar_preview(self):
        answer = {"actions": [{"type": "add_event", "title": "Dentist", "date": "2026-10-20", "start": "15:00", "end": "16:00", "category": "fixed"},
                              {"type": "cancel_class", "course_code": "ECE-221-01", "date": "2026-10-22"}], "questions": []}
        sent, rows, call = self.run_new(answer)
        self.assertEqual(len(sent), 2)
        self.assertTrue(sent[0].startswith("Hey, can you verify this change? (request #12)"))
        self.assertIn("Add Dentist on Tue Oct 20, 15:00 to 16:00", sent[0])
        self.assertIn("Cancel ECE-221-01 class on Thu Oct 22 (11:40 to 13:15)", sent[0])
        self.assertIn("Reply yes", sent[0])
        self.assertTrue(sent[1].startswith("How it will look in Google Calendar"))
        self.assertIn("15:00 to 16:00   Dentist   (new)", sent[1])
        self.assertIn("11:40 to 13:15   ECE-221-01 lecture   (removed)", sent[1])
        self.assertIn("LOCAL DRAFT", call.call_args[0][3])                      # DeepSeek got Mimir's draft
        self.assertTrue(any("awaiting_confirm" in q for q, _ in rows))

    def test_a_question_instead_of_guessing(self):
        sent, rows, _ = self.run_new({"actions": [], "questions": ["What time is the dentist?"]})
        self.assertEqual(sent, ["Quick question about request #12: What time is the dentist?"])
        self.assertTrue(any("needs_info" in q for q, _ in rows))


class YesNo(unittest.TestCase):
    def test_yes_applies_the_latest_and_no_cancels(self):
        req = {"id": 12, "status": "awaiting_confirm", "summary": "Add Dentist on Tue Oct 20, 15:00 to 16:00",
               "proposal_json": json.dumps({"actions": [{"type": "add_event", "title": "Dentist", "date": "2026-10-20",
                                                         "start": "15:00", "end": "16:00", "category": "fixed"}]})}
        sent, rows = [], []
        with mock.patch.object(intake, "fetch_one", return_value=req), mock.patch.object(intake, "context", return_value=CTX), \
             mock.patch.object(intake, "execute_query", side_effect=lambda q, p=None: rows.append((q, p))), \
             mock.patch.object(intake, "_send", side_effect=lambda t: sent.append(t)), mock.patch("core.planner.plan_safely") as replan:
            intake.handle(CFG, {"id": 6, "text": "yes"})
        self.assertTrue(any("INSERT INTO schedule_blocks" in q and p[2] == "Dentist" for q, p in rows))
        self.assertTrue(any("status = 'applied'" in q for q, _ in rows))
        replan.assert_called_once()
        self.assertIn("Done, request #12 applied", sent[-1])
        sent.clear(); rows.clear()
        with mock.patch.object(intake, "fetch_one", return_value=req), \
             mock.patch.object(intake, "execute_query", side_effect=lambda q, p=None: rows.append((q, p))), \
             mock.patch.object(intake, "_send", side_effect=lambda t: sent.append(t)):
            intake.handle(CFG, {"id": 7, "text": "no #12"})
        self.assertTrue(any("status = 'rejected'" in q for q, _ in rows))
        self.assertFalse(any("schedule_blocks" in q for q, _ in rows))
        self.assertIn("cancelled", sent[0])

    def test_yes_with_nothing_waiting(self):
        sent = []
        with mock.patch.object(intake, "fetch_one", return_value=None), mock.patch.object(intake, "_send", side_effect=lambda t: sent.append(t)):
            intake.handle(CFG, {"id": 8, "text": "ok"})
        self.assertIn("nothing waiting", sent[0])

    def test_an_answer_goes_back_with_the_original_message(self):
        pending = {"id": 13, "status": "needs_info", "text": "dentist Tue"}
        with mock.patch.object(intake, "fetch_one", return_value=pending), mock.patch.object(intake, "execute_query"), \
             mock.patch.object(intake, "new_request") as nr:
            intake.handle(CFG, {"id": 9, "text": "3pm"})
        self.assertEqual(nr.call_args[0][2], "dentist Tue\n(Answer to your question: 3pm)")


class Apply(unittest.TestCase):
    def test_each_change_writes_the_right_row(self):
        rows = []
        actions, _, _ = intake.validate({"actions": [
            {"type": "update_assignment", "assignment_id": 24, "due_at": "2026-10-20 23:59"},
            {"type": "mark_done", "assignment_id": 24},
            {"type": "cancel_event", "block_id": 90},
            {"type": "add_assignment", "course_code": "THEA-80U-01", "title": "Essay 2", "assignment_type": "other", "due_at": "2026-10-30 12:00"}]}, CTX)
        with mock.patch.object(intake, "execute_query", side_effect=lambda q, p=None: rows.append((q, p))):
            intake.apply(actions, CTX)
        qs = [q for q, _ in rows]
        self.assertTrue(any("manual_due = TRUE" in q for q in qs))                 # the iCal feed will not undo it
        self.assertTrue(any("status = 'submitted'" in q for q in qs))
        self.assertTrue(any(q.startswith("DELETE FROM schedule_blocks") and "source IS NULL OR source = 'telegram'" in q for q in qs))
        add = next(p for q, p in rows if q.startswith("INSERT INTO assignments"))
        self.assertLess(add[1], 0)                                                   # synthetic Canvas id
        self.assertEqual(add[4], 2)                                                  # "Essay 2" -> number 2


CANVAS_CTX = dict(CTX, assignments=[dict(CTX["assignments"][0], canvas_assignment_id=904973)])
PREVIEW = {"files": [("HW3_q1_part1_ECE221.pdf", 412000)], "folder": "Fall-2026/ECE221/HW/HW3", "late": True, "single_attempt": False,
           "page": {"attempts_allowed": None, "submitted": None}, "problems": []}


class Submission(unittest.TestCase):
    def run_new(self, preview):
        sent, rows = [], []
        answer = {"actions": [{"type": "submit_assignment", "assignment_id": 24}], "questions": []}
        with mock.patch.object(intake, "context", return_value=CANVAS_CTX), mock.patch.object(intake.llm_parser, "enabled", return_value=True), \
             mock.patch.object(intake.llm_parser, "json_call", return_value=(answer, 0.0)), \
             mock.patch.object(intake, "execute_query", side_effect=lambda q, p=None: rows.append((q, p)) or 21), \
             mock.patch.object(intake, "_send", side_effect=lambda t: sent.append(t)), \
             mock.patch("pollers.canvas_submit.preview", return_value=preview):
            intake.new_request(CFG, 5, "submit ECE221 HW3", now=datetime(2026, 10, 19, 11, 0))
        return sent, rows

    def test_confirmation_lists_files_attempts_and_lateness(self):
        sent, rows = self.run_new(PREVIEW)
        self.assertEqual(len(sent), 2)
        self.assertIn("Submit ECE-221-01 Homework-3 on Canvas: HW3_q1_part1_ECE221.pdf (402 KB)", sent[0])
        self.assertIn("Attempts: unlimited", sent[0])
        self.assertIn("Late: yes, it was due Sun Oct 18 23:59", sent[0])
        self.assertIn("Reply yes to apply", sent[0])
        self.assertIn("(marked submitted)", sent[1])
        stored = json.loads(next(p[0] for q, p in rows if "awaiting_confirm" in q))
        self.assertEqual(stored["actions"][0]["preview"]["files"], [["HW3_q1_part1_ECE221.pdf", 412000]])

    def test_problems_are_reported_instead_of_asking(self):
        sent, rows = self.run_new(dict(PREVIEW, files=[], problems=["there are no files in coursework/Fall-2026/ECE221/HW/HW3/ yet"]))
        self.assertEqual(len(sent), 1)
        self.assertIn("I can't submit Homework-3", sent[0])
        self.assertFalse(any("awaiting_confirm" in q for q, _ in rows))

    def test_single_attempt_needs_yes_final(self):
        sent, _ = self.run_new(dict(PREVIEW, single_attempt=True, page={"attempts_allowed": 1, "submitted": None}))
        self.assertIn("Reply yes final", sent[0])
        req = {"id": 21, "status": "awaiting_confirm", "summary": "Submit ...", "proposal_json": json.dumps(
            {"needs_final": True, "actions": [{"type": "submit_assignment", "assignment_id": 24, "preview": {"files": [["a.pdf", 1]]}}]})}
        sent2 = []
        with mock.patch.object(intake, "fetch_one", return_value=req), mock.patch.object(intake, "_send", side_effect=lambda t: sent2.append(t)), \
             mock.patch.object(intake, "apply") as ap:
            intake.handle(CFG, {"id": 9, "text": "yes"})
            ap.assert_not_called()                         # a plain yes is not enough
            self.assertIn("yes final", sent2[0])
            with mock.patch.object(intake, "context", return_value=CANVAS_CTX), mock.patch.object(intake, "execute_query"), \
                 mock.patch("core.planner.plan_safely"):
                ap.return_value = ["submitted: Canvas shows SUBMITTED: Oct 19, 2026 11:02am"]
                intake.handle(CFG, {"id": 10, "text": "yes final"})
            ap.assert_called_once()
        self.assertIn("Canvas shows SUBMITTED: Oct 19, 2026 11:02am", sent2[-1])

    def test_apply_submits_only_the_confirmed_files_and_stops_on_failure(self):
        actions = [{"type": "submit_assignment", "assignment_id": 24, "preview": {"files": [("HW3.pdf", 10)]}}]
        with mock.patch("pollers.canvas_submit.submit", return_value={"ok": True, "message": "Canvas shows SUBMITTED: x"}) as sub, \
             mock.patch.object(intake, "execute_query") as q, mock.patch.object(intake, "load_config", return_value=CFG):
            self.assertEqual(intake.apply(actions, CANVAS_CTX), ["submitted: Canvas shows SUBMITTED: x"])
        self.assertEqual(sub.call_args[0][2], ["HW3.pdf"])
        self.assertIn("status = 'submitted'", q.call_args[0][0])
        with mock.patch("pollers.canvas_submit.submit", return_value={"ok": False, "message": "Canvas did not show it as submitted"}), \
             mock.patch.object(intake, "execute_query") as q, mock.patch.object(intake, "load_config", return_value=CFG):
            with self.assertRaises(RuntimeError):
                intake.apply(actions, CANVAS_CTX)
        q.assert_not_called()                              # not marked submitted when Canvas did not confirm it

    def test_submit_never_opens_canvas_without_files_or_when_they_changed(self):
        from pollers import canvas_submit
        row = {"id": 24, "canvas_assignment_id": 904973}
        with mock.patch.object(canvas_submit, "_assignment", return_value=row), \
             mock.patch.object(canvas_submit, "repo_files", return_value=("Fall-2026/ECE221/HW/HW3", [])), \
             mock.patch("playwright.sync_api.sync_playwright") as pw:
            r = canvas_submit.submit(CFG, 24, [])
        self.assertFalse(r["ok"])
        pw.assert_not_called()
        with mock.patch.object(canvas_submit, "_assignment", return_value=row), \
             mock.patch.object(canvas_submit, "repo_files", return_value=("f", [("new.pdf", 5, "u")])), \
             mock.patch("playwright.sync_api.sync_playwright") as pw:
            r = canvas_submit.submit(CFG, 24, ["old.pdf"])
        self.assertIn("changed since you confirmed", r["message"])
        pw.assert_not_called()


if __name__ == "__main__":
    unittest.main()
