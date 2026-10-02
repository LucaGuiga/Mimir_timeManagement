"""DeepSeek spend tracking tests. Pure maths tests always run. The rest need a THROWAWAY database, same as test_oura_auth:
    MIMIR_TEST_DB=mimir_test MIMIR_TEST_USER=root MIMIR_TEST_PASSWORD=... python -m unittest tests.test_deepseek_usage -v"""
import os
import sys
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import deepseek_usage as U  # noqa: E402

TEST_DB = os.environ.get("MIMIR_TEST_DB")
CFG = {"_path": "x", "deepseek": {"monthly_cap_usd": 10, "billing_day": 1, "price_input_per_m": 1.0,
                                  "price_cache_hit_per_m": 0.1, "price_output_per_m": 2.0},
       "mysql": {"host": os.environ.get("MIMIR_TEST_HOST", "127.0.0.1"), "port": int(os.environ.get("MIMIR_TEST_PORT", "3306")),
                 "user": os.environ.get("MIMIR_TEST_USER", "root"), "password": os.environ.get("MIMIR_TEST_PASSWORD", ""), "db": TEST_DB},
       "paths": {"run_dir": "run"}}


def cfg_day(day, cap=10):
    return {"deepseek": {**CFG["deepseek"], "billing_day": day, "monthly_cap_usd": cap}}


class MathTests(unittest.TestCase):
    def test_period_default_is_calendar_month(self):
        self.assertEqual(U.period(cfg_day(1), date(2026, 10, 17)), (date(2026, 10, 1), date(2026, 10, 31)))
        self.assertEqual(U.period(cfg_day(1), date(2026, 2, 5)), (date(2026, 2, 1), date(2026, 2, 28)))

    def test_period_with_a_mid_month_billing_day(self):
        self.assertEqual(U.period(cfg_day(15), date(2026, 10, 17)), (date(2026, 10, 15), date(2026, 11, 14)))
        self.assertEqual(U.period(cfg_day(15), date(2026, 10, 3)), (date(2026, 9, 15), date(2026, 10, 14)))
        self.assertEqual(U.period(cfg_day(15), date(2026, 1, 3)), (date(2025, 12, 15), date(2026, 1, 14)))   # across a year
        self.assertEqual(U.period(cfg_day(15), date(2026, 10, 15)), (date(2026, 10, 15), date(2026, 11, 14)))  # start day itself

    def test_billing_day_is_clamped(self):
        self.assertEqual(U.period(cfg_day(31), date(2026, 10, 30))[0], date(2026, 10, 28))

    def test_cost_uses_cache_hit_price(self):
        # 1M prompt of which 400k cached, 500k completion: 600k*1 + 400k*0.1 + 500k*2 = 0.6 + 0.04 + 1.0
        self.assertAlmostEqual(U.cost(CFG, 1_000_000, 500_000, 400_000), 1.64)
        self.assertAlmostEqual(U.cost(CFG, 1000, 0, 5000), 1000 * 0.1 / 1e6)     # cache hits cannot exceed the prompt

    def test_projection(self):
        total = {"start": date(2026, 10, 1), "end": date(2026, 10, 31), "usd": 3.0}
        self.assertAlmostEqual(U.projected(total, datetime(2026, 10, 11)), 3.0 / 10 * 31)
        self.assertAlmostEqual(U.projected(total, datetime(2026, 10, 1, 6)), 3.0 * 31)   # day one is floored at one full day


@unittest.skipUnless(TEST_DB, "set MIMIR_TEST_DB to a throwaway database to run these tests")
class DbTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import core.config as C
        C._cache[os.path.abspath(C.default_config_path())] = CFG
        import db.db as D
        D.apply_schema()
        cls.D = D

    def setUp(self):
        for t in ("deepseek_usage", "deepseek_alert", "error_log"):
            self.D.execute_query(f"DELETE FROM {t}")
        self.sent = []
        self.p = mock.patch("core.deepseek_usage.notifier.send_warning", side_effect=lambda t: self.sent.append(t) or True)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def add(self, prompt, completion, days_ago=0, usd=None):
        usd = U.cost(CFG, prompt, completion) if usd is None else usd
        self.D.execute_query("INSERT INTO deepseek_usage (called_at, kind, prompt_tokens, completion_tokens, cost_usd) VALUES (%s,'t',%s,%s,%s)",
                             (datetime.now() - timedelta(days=days_ago), prompt, completion, usd))

    def test_record_stores_tokens_and_cost(self):
        U.record(CFG, "assignments", {"prompt_tokens": 2000, "completion_tokens": 500, "prompt_cache_hit_tokens": 1000})
        t = U.period_total(CFG)
        self.assertEqual((t["prompt"], t["completion"], t["tokens"], t["calls"]), (2000, 500, 2500, 1))
        self.assertAlmostEqual(t["usd"], U.cost(CFG, 2000, 500, 1000), places=5)

    def test_record_ignores_missing_usage(self):
        U.record(CFG, "x", None)
        U.record(CFG, "x", "garbage")
        self.assertEqual(U.period_total(CFG)["calls"], 0)

    def test_period_total_is_the_whole_period_not_one_call(self):
        start, _ = U.period(CFG)
        for _ in range(3):
            U.record(CFG, "assignments", {"prompt_tokens": 1000, "completion_tokens": 100})
        rows = U.email_rows(CFG)
        self.assertIn("3 calls", rows[1])
        self.assertIn("3,300 total", rows[1])
        self.assertIn(f"{start:%b %d}", rows[0])

    def test_calls_before_the_period_are_excluded(self):
        self.add(1_000_000, 1_000_000, days_ago=400)
        self.add(1000, 100)
        self.assertEqual(U.period_total(CFG)["tokens"], 1100)

    def test_alert_when_projected_over_cap_and_only_once(self):
        start, end = U.period(CFG)
        # enough spend that the pace passes the cap but the cap itself is not reached yet
        days = max((date.today() - start).days + 1, 1)
        total_days = (end - start).days + 1
        per_period_target = 14.0
        spend = per_period_target * days / total_days
        spend = min(spend, 9.5)
        if spend * total_days / days < 10:
            self.skipTest("too early or too late in the period to hit the projection case with this cap")
        self.add(1000, 100, usd=spend)
        self.assertEqual(U.check_budget(CFG), "projected")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("will reach about", self.sent[0])
        self.assertIn("$10.00", self.sent[0])
        U.check_budget(CFG)
        U.check_budget(CFG)
        self.assertEqual(len(self.sent), 1, "must not repeat within the period")

    def test_alert_when_cap_reached_and_only_once(self):
        self.add(1000, 100, usd=10.5)
        self.assertEqual(U.check_budget(CFG), "reached")
        U.check_budget(CFG)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("at or over your $10.00 limit", self.sent[0])

    def test_no_alert_when_well_under_the_cap(self):
        self.add(1000, 100, usd=0.01)
        self.assertIsNone(U.check_budget(CFG))
        self.assertEqual(self.sent, [])

    def test_alert_resets_for_the_next_period(self):
        self.add(1000, 100, usd=10.5)
        U.check_budget(CFG, datetime.now())
        later = datetime.now() + timedelta(days=40)
        self.assertEqual(U.period_total(CFG, later.date())["usd"], 0.0)   # new period starts empty
        self.assertIsNone(U.check_budget(CFG, later))

    def test_email_rows_say_unavailable_not_zero_when_the_query_fails(self):
        with mock.patch("core.deepseek_usage.fetch_one", side_effect=RuntimeError("db down")), \
             mock.patch("core.deepseek_usage.log_error"):
            self.assertEqual(U.email_rows(CFG), ["unavailable"])

    def test_both_emails_include_the_usage_section(self):
        from core import email_sender
        U.record(CFG, "assignments", {"prompt_tokens": 1234, "completion_tokens": 100})
        captured = []
        with mock.patch.object(email_sender, "_send", side_effect=lambda cfg, subj, parts: captured.append("".join(t for _, t in parts)) or True):
            for fn in (email_sender.send_morning, email_sender.send_evening):
                try:
                    fn(CFG)
                except Exception as e:                       # other sections may need data; the usage section is what matters here
                    self.fail(f"{fn.__name__} raised {type(e).__name__}: {e}")
        self.assertEqual(len(captured), 2)
        for text in captured:
            self.assertIn("DeepSeek usage (billing period)", text)
            self.assertIn("1,334 total", text)

    def test_llm_call_records_usage_even_when_the_answer_is_unusable(self):
        from core import llm_parser
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"choices": [{"message": {"content": "not json"}}], "usage": {"prompt_tokens": 500, "completion_tokens": 50}}
        with mock.patch("core.llm_parser._post", return_value=resp):
            with self.assertRaises(llm_parser.LLMParseError):
                llm_parser._call(CFG, "sys", "user", "assignments")
        self.assertEqual(U.period_total(CFG)["tokens"], 550)

    def test_anthropic_key_is_not_required(self):
        from core.config import validate_config, REQUIRED_KEYS
        self.assertNotIn("anthropic.api_key", REQUIRED_KEYS)
        full = {"ical": {"feed_url": "x"}, "github": {"pat": "x"}, "telegram": {"bot_token": "x", "chat_id": "1"},
                "mysql": {"host": "h", "port": 1, "user": "u", "password": "p", "db": "d"}}
        self.assertTrue(validate_config(full))


if __name__ == "__main__":
    unittest.main()
