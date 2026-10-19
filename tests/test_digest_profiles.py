"""Run 12 tests: pooled Telegram digest, Telegram replies, syllabus matching to Canvas, professor syllabus statistics,
and the tunable difficulty curve. Telegram, DeepSeek and MySQL are mocked:
    python -m unittest tests.test_digest_profiles -v"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import notifier, professor_profiler as pp, syllabus_parser as sp, workload  # noqa: E402

CFG = {"telegram": {"bot_token": "t", "chat_id": "42", "digest_minutes": 30},
       "deepseek": {"enabled": True, "api_key": "k"}, "privacy": {"identity": {"full_name": "Luca Guiga"}}}
NOW = datetime(2026, 10, 17, 12, 0)


class Pooling(unittest.TestCase):
    def test_messages_queue_instead_of_sending(self):
        with mock.patch.object(notifier, "load_config", return_value=CFG), mock.patch("db.db.execute_query") as q, \
             mock.patch.object(notifier, "deliver") as send:
            notifier.send_warning("ECE221 syllabus has no class times")
        self.assertIn("INSERT INTO notification_queue", q.call_args[0][0])
        self.assertEqual(q.call_args[0][1][0], "warning")
        send.assert_not_called()

    def test_zero_minutes_sends_at_once_and_db_down_still_sends(self):
        with mock.patch.object(notifier, "load_config", return_value={"telegram": {"digest_minutes": 0}}), \
             mock.patch.object(notifier, "deliver", return_value=True) as send:
            notifier.send_message("now")
        send.assert_called_once_with("now")
        with mock.patch.object(notifier, "load_config", return_value=CFG), mock.patch("db.db.execute_query", side_effect=RuntimeError("db down")), \
             mock.patch.object(notifier, "write_fallback"), mock.patch.object(notifier, "deliver", return_value=True) as send:
            notifier.send_message("still goes out")
        send.assert_called_once_with("still goes out")


class Digest(unittest.TestCase):
    def run_flush(self, queued, errors, now=NOW, summary=None, last="10"):
        state = {"digest_last_error_id": last}
        updates = []
        with mock.patch.object(notifier, "_state_get", side_effect=lambda n, d=None: state.get(n, d)), \
             mock.patch.object(notifier, "_state_set", side_effect=lambda n, v: state.__setitem__(n, str(v))), \
             mock.patch("db.db.fetch_all", side_effect=lambda q, p=None: queued if "notification_queue" in q else errors), \
             mock.patch("db.db.execute_query", side_effect=lambda q, p=None: updates.append((q, p))), \
             mock.patch("core.llm_parser.enabled", return_value=True), \
             mock.patch("core.llm_parser.json_call", side_effect=summary or (lambda *a, **k: ({"message": "Summary text"}, 0.0))), \
             mock.patch.object(notifier, "deliver", return_value=True) as send:
            sent = notifier.flush_digest(CFG, now)
        return sent, send, updates, state

    def test_waits_thirty_minutes_after_the_first_item(self):
        q = [{"id": 1, "created_at": NOW - timedelta(minutes=10), "level": "warning", "text": "a"}]
        sent, send, _, _ = self.run_flush(q, [])
        self.assertFalse(sent)
        send.assert_not_called()

    def test_one_summary_for_messages_and_errors(self):
        q = [{"id": 1, "created_at": NOW - timedelta(minutes=31), "level": "warning", "text": "THEA80 syllabus has no class times"},
             {"id": 2, "created_at": NOW - timedelta(minutes=5), "level": "info", "text": "Added ECE129 folder"}]
        e = [{"id": 11, "timestamp": NOW - timedelta(minutes=20), "script_name": "email_sender", "error_type": "SMTPServerDisconnected",
              "operation": "send", "severity": "warning", "raw_message": "Connection unexpectedly closed"}]
        sent, send, updates, state = self.run_flush(q, e)
        self.assertTrue(sent)
        text = send.call_args[0][0]
        self.assertIn("2 updates, 1 error", text)
        self.assertIn("Summary text", text)
        self.assertTrue(any("SET sent_at" in u[0] and u[1] == [1, 2] for u in updates))
        self.assertEqual(state["digest_last_error_id"], "11")

    def test_plain_summary_when_deepseek_fails(self):
        q = [{"id": 1, "created_at": NOW - timedelta(minutes=40), "level": "info", "text": "same"},
             {"id": 2, "created_at": NOW - timedelta(minutes=39), "level": "info", "text": "same"}]
        def boom(*a, **k):
            raise RuntimeError("deepseek down")
        with mock.patch.object(notifier, "write_fallback"):
            sent, send, _, _ = self.run_flush(q, [], summary=boom)
        self.assertTrue(sent)
        self.assertIn("(2x) same", send.call_args[0][0])


class Replies(unittest.TestCase):
    def test_only_your_chat_is_saved_and_offset_advances(self):
        body = {"ok": True, "result": [
            {"update_id": 5, "message": {"chat": {"id": 42}, "date": 1792270000, "text": "skip the club block today"}},
            {"update_id": 6, "message": {"chat": {"id": 999}, "date": 1792270001, "text": "spam from someone else"}}]}
        resp = mock.Mock(status_code=200); resp.json.return_value = body
        saved, state = [], {}
        with mock.patch.object(notifier.requests, "get", return_value=resp) as get, \
             mock.patch.object(notifier, "_state_get", return_value="4"), \
             mock.patch.object(notifier, "_state_set", side_effect=lambda n, v: state.__setitem__(n, v)), \
             mock.patch("db.db.execute_query", side_effect=lambda q, p=None: saved.append(p) or 1):
            n = notifier.poll_replies(CFG)
        self.assertEqual(n, 1)
        self.assertEqual([p[2] for p in saved], ["skip the club block today"])
        self.assertEqual(get.call_args.kwargs["params"]["offset"], 5)
        self.assertEqual(state["telegram_last_update_id"], 6)


class CanvasMatching(unittest.TestCase):
    def test_canvas_ids_checked_against_the_course(self):
        item = {"title": "Homework 3", "assignment_type": "hw", "assignment_number": 3, "predicted_open": None, "predicted_due": None, "notes": ""}
        data = {"professor_name": "Prof", "items": [dict(item, canvas_assignment_id=21), dict(item, canvas_assignment_id=21),
                                                    dict(item, canvas_assignment_id=999), dict(item, canvas_assignment_id="21")],
                "meetings": [], "conflicts": []}
        errors, cleaned = sp._validate(data, (datetime(2026, 9, 1).date(), datetime(2026, 12, 31).date()), {21, 22})
        self.assertEqual(errors, [])
        self.assertEqual([i["canvas_assignment_id"] for i in cleaned["items"]], [21, None, None, None])   # once, own course, real ints only

    def test_match_prefers_the_verified_canvas_id(self):
        rows = [{"id": 21, "title": "Homework-3", "assignment_type": "hw", "assignment_number": 3},
                {"id": 30, "title": "Week 3 reading", "assignment_type": "reading", "assignment_number": None}]
        items = [{"title": "Folklorico Reading", "assignment_type": "reading", "assignment_number": None, "canvas_assignment_id": 30,
                  "predicted_open": None, "predicted_due": "2026-10-16"}]
        with mock.patch.object(sp, "fetch_all", return_value=rows), mock.patch.object(sp, "execute_query") as q:
            self.assertEqual(sp._match_items(3, items), 1)
        self.assertEqual(items[0]["matched_assignment_id"], 30)
        self.assertEqual(q.call_args[0][1][2], 30)

    def test_professor_linked_and_history_replayed(self):
        qs = []
        with mock.patch.object(sp, "execute_query", side_effect=lambda q, p=None: qs.append(q)), \
             mock.patch.object(sp, "fetch_one", return_value={"id": 4}):
            self.assertEqual(sp.link_professor({"id": 2, "professor_id": None}, "  Dr.  Ada   Lovelace "), 4)
            self.assertIsNone(sp.link_professor({"id": 2, "professor_id": 9}, "Someone Else"))   # an existing link is kept
        self.assertTrue(any("INSERT IGNORE INTO professor_profiles" in q for q in qs))
        self.assertEqual(sum("profiled = FALSE" in q for q in qs), 3)


class ProfessorStats(unittest.TestCase):
    def test_due_and_open_accuracy(self):
        d = lambda day, h=23, m=59: datetime(2026, 10, day, h, m)
        rows = [{"available_from": d(2), "syllabus_predicted_open": d(4), "due_at": d(9), "syllabus_predicted_due": d(9)},    # opened 2 days early, due on time
                {"available_from": d(6), "syllabus_predicted_open": d(5), "due_at": d(16), "syllabus_predicted_due": d(13)},  # 1 day late, due 3 days later
                {"available_from": None, "syllabus_predicted_open": None, "due_at": d(20), "syllabus_predicted_due": None}]
        st = pp.syllabus_stats(rows)
        self.assertEqual(st["syllabus_matched_count"], 3)
        self.assertAlmostEqual(st["avg_due_shift_days"], 1.5)
        self.assertAlmostEqual(st["due_on_time_share"], 0.5)
        self.assertAlmostEqual(st["avg_early_post_days"], 2.0)
        self.assertAlmostEqual(st["avg_late_post_days"], 1.0)
        self.assertAlmostEqual(st["syllabus_adherence_score"], ((1 - 2 / 7) + (1 - 1 / 7) + 1 + (1 - 3 / 7)) / 4)

    def test_no_matches_means_no_numbers(self):
        st = pp.syllabus_stats([])
        self.assertIsNone(st["syllabus_adherence_score"])
        self.assertIsNone(st["avg_due_shift_days"])


class Curve(unittest.TestCase):
    def test_curve_flattens_the_estimates(self):
        p = workload.Presets({"stress": {"difficulty_curve": 2.0}, "difficulty_presets": {"test": 5}}, courses=[{"id": 1, "ical_course_code": "ECE-221-01", "hour_scale": 1.0}], rows=[])
        self.assertAlmostEqual(p.hours(1, "test"), 2.718281828 ** 2.5 * 1, places=4)   # difficulty 5: about 12 h instead of 55
        self.assertAlmostEqual(workload.curve({}), 1.248)


if __name__ == "__main__":
    unittest.main()


class PlanStress(unittest.TestCase):
    def test_needed_over_free_in_the_window(self):
        from datetime import date, timedelta
        from core import stress
        day = date.today() + timedelta(days=30)   # a future day: the whole window is free time
        cfg = {"study": {"horizon_days": 7, "day_start": "08:00", "latest_end": "22:00"},
               "time_presets": {"sleep_target_hours": 8, "alpha": 2, "breakfast_minutes": 0, "lunch_minutes": 0, "dinner_minutes": 0,
                                "chores_minutes": 0, "commute_minutes": 0},
               "difficulty_presets": {"test": 5}, "stress": {"difficulty_curve": 1.248}}
        due = datetime.combine(day, datetime.min.time()) + timedelta(days=14)
        class_block = {"date": day, "time_category": "class", "start_time": timedelta(hours=10), "end_time": timedelta(hours=12),
                       "skipped": False, "source": "class"}
        def fetch_all(q, p=None):
            if "FROM schedule_blocks WHERE date >=" in q:
                return [class_block]
            if "source = 'study'" in q:
                return [{"assignment_id": 1, "h": 4 * 3600}]   # 4 hours already studied
            if "FROM assignments a JOIN courses" in q:
                return [{"id": 1, "course_id": 2, "assignment_type": "test", "due_at": due}]
            return [{"id": 2, "ical_course_code": "ECE-221-01", "hour_scale": 1.0}] if "FROM courses" in q else []
        with mock.patch.object(stress, "fetch_all", side_effect=fetch_all), mock.patch.object(stress, "fetch_one", return_value=None), \
             mock.patch("core.workload.fetch_all", side_effect=fetch_all):
            r = stress.calculate(day, cfg)
        self.assertAlmostEqual(r["t_available"], 7 * 14 - 2)                       # 14 h a day, minus the 2 h class
        self.assertAlmostEqual(r["deadline_term"], (workload.hours_for(5) - 4) * 7 / 14, places=4)   # half of what is left falls in the window
        self.assertAlmostEqual(r["ratio"], r["deadline_term"] / r["t_available"])


class CanvasSignIn(unittest.TestCase):
    CFG = {"canvas": {"username": "someone", "password": "pw"}}

    def run_recover(self, state, result="ok"):
        from pollers import canvas_scraper as cs
        sent = []
        with mock.patch.object(cs.notifier, "_state_get", side_effect=lambda n, d=None: state.get(n, d)), \
             mock.patch.object(cs.notifier, "_state_set", side_effect=lambda n, v: state.__setitem__(n, v)), \
             mock.patch.object(cs.notifier, "deliver", side_effect=lambda t: sent.append(t)), \
             mock.patch.object(cs, "auto_login", return_value=result) as login:
            ok = cs.recover_session(None, self.CFG, "x")
        return ok, login, sent

    def test_signs_in_and_clears_the_notice(self):
        state = {"canvas_login_notified": "earlier"}
        ok, login, sent = self.run_recover(state)
        self.assertTrue(ok)
        login.assert_called_once()
        self.assertEqual(state["canvas_login_notified"], "")
        self.assertEqual(sent, [])

    def test_rejected_password_is_never_retried_and_says_so_once(self):
        state = {}
        ok, login, sent = self.run_recover(state, "bad_credentials")
        self.assertFalse(ok)
        self.assertIn("rejected", sent[0])
        state["canvas_login_attempt_at"] = "2000-01-01T00:00:00"   # long ago: the time limit would allow another try
        ok, login, sent = self.run_recover(state, "ok")
        self.assertFalse(ok)
        login.assert_not_called()
        self.assertEqual(sent, [])                                  # no second message for the same outage

    def test_at_most_one_attempt_per_half_hour(self):
        state = {"canvas_login_attempt_at": datetime.now().isoformat(timespec="seconds"), "canvas_login_notified": "x"}
        ok, login, sent = self.run_recover(state)
        self.assertFalse(ok)
        login.assert_not_called()


class BothEquations(unittest.TestCase):
    def test_plan_is_primary_and_daily_rides_along(self):
        from core import stress
        with mock.patch.object(stress, "calculate_plan", return_value={"ratio": 1.25, "band": "red"}), \
             mock.patch.object(stress, "calculate_daily", return_value={"ratio": 1.56, "band": "red"}):
            r = stress.calculate(None, {"stress": {"amber_below": 1.0}})
            self.assertEqual((r["ratio"], r["equation"], r["alt_equation"], r["alt_ratio"]), (1.25, "plan", "daily", 1.56))
            r = stress.calculate(None, {"stress": {"equation": "daily"}})
            self.assertEqual((r["ratio"], r["equation"], r["alt_ratio"]), (1.56, "daily", 1.25))

    def test_daily_is_the_original_formula(self):
        from datetime import date, timedelta
        from core import stress
        day = date.today() + timedelta(days=30)
        due = datetime.combine(day, datetime.min.time()) + timedelta(days=12, hours=12)   # 12 days after noon of `day`
        cfg = {"time_presets": {"sleep_target_hours": 8, "alpha": 2, "breakfast_minutes": 30, "lunch_minutes": 30, "dinner_minutes": 60,
                                "chores_minutes": 30, "commute_minutes": 30}, "difficulty_presets": {"hw": 2}}
        def fetch_all(q, p=None):
            if "FROM assignments a JOIN courses" in q:
                return [{"course_id": 2, "assignment_type": "hw", "due_at": due}]
            if "FROM schedule_blocks WHERE date = %s" in q:
                return [{"time_category": "class", "start_time": timedelta(hours=10), "end_time": timedelta(hours=12), "skipped": False}]
            return [{"id": 2, "ical_course_code": "ECE-221-01", "hour_scale": 1.0}] if "FROM courses" in q else []
        with mock.patch.object(stress, "fetch_all", side_effect=fetch_all), mock.patch.object(stress, "fetch_one", return_value=None), \
             mock.patch("core.workload.fetch_all", side_effect=fetch_all):
            r = stress.calculate_daily(day, cfg)
        available = (24 - 8) - 2 - 0.5 - (0.5 + 2.0)          # awake - class - commute - (chores + meals)
        self.assertAlmostEqual(r["t_available"], available)
        self.assertAlmostEqual(r["ratio"], (workload.hours_for(2) / 12) / available)
