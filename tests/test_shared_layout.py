"""Shared repo layout tests: one repo, a folder per course ({term}/{COURSE}/{category}/), tracked per course.

GitHub and MySQL are mocked, so no database or network is needed:
    python -m unittest tests.test_shared_layout -v"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import ical_bootstrap, notifier, repo_manager  # noqa: E402
from pollers import github as gh  # noqa: E402
from scripts.validate_hook import category_index  # noqa: E402

FOLDERS = ["HW", "Labs", "Tests", "Quizzes", "Projects", "Readings"]


def cfg(**repo):
    return {"quarter_label": "Fall 2026",
            "github": {"pat": "x", "username": "luca", "category_folders": {"HW": "hw", "Labs": "lab", "Tests": "test", "Readings": "reading"}},
            "repo_manager": {"layout": "shared", "shared_repo": "coursework", "use_term_folder": True,
                             "default_branches": ["main"], "default_folders": FOLDERS, "course_code_pattern": "[A-Z]+\\d+", **repo}}


class Naming(unittest.TestCase):
    def test_course_path_uses_term_and_code(self):
        self.assertEqual(repo_manager.course_path(cfg(), {"ical_course_code": "ECE-129A-01"}), "Fall-2026/ECE129")

    def test_lab_keeps_its_l(self):
        self.assertEqual(repo_manager.course_path(cfg(), {"ical_course_code": "ECE-129L-01", "is_lab": True}), "Fall-2026/ECE129L")

    def test_term_folder_off_or_empty(self):
        self.assertEqual(repo_manager.course_path(cfg(use_term_folder=False), {"ical_course_code": "THEA-80U-01"}), "THEA80")
        c = cfg(); c["quarter_label"] = ""
        self.assertEqual(repo_manager.course_path(c, {"ical_course_code": "THEA-80U-01"}), "THEA80")

    def test_repo_name_for_unchanged(self):
        self.assertEqual(ical_bootstrap.repo_name_for(cfg(), "ECE-221-01"), "ECE221")
        self.assertEqual(ical_bootstrap.repo_name_for(cfg(), "ECE-141L-01", True), "ECE141L")

    def test_preview_shared(self):
        p = ical_bootstrap.preview_repos(cfg(), [{"id": 1, "ical_course_code": "ECE-129A-01", "is_lab": False}])[0]
        self.assertEqual((p["repo_name"], p["path"], p["branches"]), ("coursework", "Fall-2026/ECE129", ["main"]))
        self.assertEqual(p["folders"][0], "Fall-2026/ECE129/HW")

    def test_preview_per_course(self):
        p = ical_bootstrap.preview_repos(cfg(layout="per_course"), [{"id": 1, "ical_course_code": "ECE-129A-01", "is_lab": False}])[0]
        self.assertEqual((p["repo_name"], p["path"], p["folders"]), ("ECE129", None, FOLDERS))


class FakeGitHub:
    """Enough of the GitHub REST API for create_course_repo. The repo exists after the first POST."""
    def __init__(self):
        self.exists, self.files, self.calls = False, set(), []

    def __call__(self, cfg_, method, path, **kw):
        self.calls.append((method, path))
        if method == "POST" and path == "/user/repos":
            if self.exists:
                raise RuntimeError("github POST /user/repos -> 422: name already exists")
            self.exists = True
            return {"default_branch": "main", "html_url": "https://github.com/luca/" + kw["json"]["name"]}
        if method == "GET" and path.endswith("/git/ref/heads/main"):
            return {"object": {"sha": "abc"}}
        if method == "GET" and "/contents/" in path:
            if path.split("/contents/", 1)[1] in self.files:
                return {}
            raise RuntimeError("github GET -> 404: Not Found")
        if method == "PUT" and "/contents/" in path:
            self.files.add(path.split("/contents/", 1)[1])
            return {}
        if method == "GET" and path.endswith("/branches"):
            return [{"name": "main"}]
        if method == "GET" and path.startswith("/repos/luca/"):
            return {"default_branch": "main", "html_url": "https://github.com/luca/" + path.split("/")[3]}
        raise AssertionError(f"unexpected call {method} {path}")


class CreateRepo(unittest.TestCase):
    def run_create(self, c, courses):
        gh_api, updates = FakeGitHub(), []
        with mock.patch.object(repo_manager, "_call", gh_api), \
             mock.patch.object(repo_manager, "execute_query", lambda q, p=None: updates.append(p)), \
             mock.patch.object(repo_manager.notifier, "send_message"), mock.patch.object(repo_manager.notifier, "send_warning"):
            results = [repo_manager.create_course_repo(c, course) for course in courses]
        return gh_api, updates, results

    def test_shared_one_repo_folder_per_course(self):
        courses = [{"id": 1, "canvas_course_name": "ECE-129A-01", "ical_course_code": "ECE-129A-01", "is_lab": False, "repo_name": "coursework"},
                   {"id": 3, "canvas_course_name": "THEA-80U-01", "ical_course_code": "THEA-80U-01", "is_lab": False}]
        api, updates, results = self.run_create(cfg(), courses)
        self.assertTrue(all(r["ok"] for r in results), results)
        self.assertEqual([m for m, p in api.calls if p == "/user/repos"], ["POST", "POST"])   # second one hits 422, reuses the repo
        self.assertFalse(any(p == "/repos/luca/coursework/git/refs" for _, p in api.calls))   # no extra branches
        self.assertIn("Fall-2026/ECE129/HW/.gitkeep", api.files)
        self.assertIn("Fall-2026/THEA80/Readings/.gitkeep", api.files)
        self.assertEqual(len(api.files), 12)
        self.assertEqual(updates, [("coursework", "Fall-2026/ECE129", json.dumps(["main"]), 1),
                                   ("coursework", "Fall-2026/THEA80", json.dumps(["main"]), 3)])
        self.assertEqual((results[0]["created"], results[1]["created"]), (True, False))

    def test_per_course_unchanged(self):
        api, updates, results = self.run_create(cfg(layout="per_course"),
                                                [{"id": 2, "canvas_course_name": "ECE-221-01", "repo_name": "ECE221"}])
        self.assertTrue(results[0]["ok"], results)
        self.assertIn("HW/.gitkeep", api.files)
        self.assertEqual(updates, [("ECE221", None, json.dumps(["main"]), 2)])


class Reminder(unittest.TestCase):
    def body(self, course):
        a = {"assignment_type": "hw", "assignment_number": 3, "due_at": datetime.now() + timedelta(days=2)}
        with mock.patch.object(notifier, "send_warning", return_value=True) as w:
            notifier.send_assignment_reminder(a, course, cfg())
        return w.call_args[0][0]

    def test_shared_path_and_file_code(self):
        b = self.body({"repo_name": "coursework", "repo_path_prefix": "Fall-2026/ECE129"})
        self.assertIn("Upload to: Fall-2026/ECE129/HW/HW3/ in coursework (branch main)", b)
        self.assertIn("git add Fall-2026/ECE129/HW/HW3/", b)
        self.assertIn("HW3_qn_partn_ECE129", b)   # course folder, not the repo name
        self.assertNotIn("_coursework", b)

    def test_per_course_path(self):
        b = self.body({"repo_name": "ECE221", "repo_path_prefix": None})
        self.assertIn("git add HW/HW3/", b)
        self.assertIn("_ECE221", b)


class Hook(unittest.TestCase):
    def test_category_found_at_any_depth(self):
        f = {x.lower() for x in FOLDERS}
        self.assertEqual(category_index("HW/HW1/HW1_q1_part1_ECE129.pdf".split("/"), f), 0)
        self.assertEqual(category_index("Fall-2026/ECE129/HW/HW1/HW1_q1_part1_ECE129.pdf".split("/"), f), 2)
        self.assertIsNone(category_index("README.md".split("/"), f))
        self.assertIsNone(category_index("Fall-2026/notes.txt".split("/"), f))


class Poller(unittest.TestCase):
    def test_commit_touching_two_courses_counts_for_both(self):
        courses = [{"id": 1, "repo_name": "coursework", "repo_path_prefix": "Fall-2026/ECE129", "branches": '["main"]'},
                   {"id": 3, "repo_name": "coursework", "repo_path_prefix": "Fall-2026/THEA80", "branches": '["main"]'}]
        files = ["Fall-2026/ECE129/HW/HW1/HW1_q1_part1_ECE129.pdf", "Fall-2026/THEA80/Readings/Reading2/notes.pdf", "README.md"]
        inserts, asked = [], []

        def request(cfg_, path, params=None):
            asked.append((path, (params or {}).get("path")))
            if path.endswith("/commits"):
                return [{"sha": "s1"}]
            if path.endswith("/commits/s1"):
                return {"commit": {"committer": {"date": "2026-10-16T20:00:00Z"}, "author": {"name": "Luca"}, "message": "work"},
                        "files": [{"filename": f, "status": "added"} for f in files]}
            return {"size": 100}

        def fetch_all(q, p=None):
            return courses if "FROM courses" in q else []

        def fetch_one(q, p=None):
            if "FROM assignments" in q:
                return {"id": 40 + p[0]}
            return None

        with mock.patch.object(gh, "_request", request), mock.patch.object(gh, "fetch_all", fetch_all), \
             mock.patch.object(gh, "fetch_one", fetch_one), mock.patch.object(gh, "execute_query", lambda q, p=None: inserts.append(p)), \
             mock.patch.object(gh, "_seen_shas", set()):
            gh.run_cycle(cfg())
        self.assertEqual(sorted((p[0], p[4]) for p in inserts), [(1, files[0]), (3, files[1])])   # README.md belongs to neither
        self.assertEqual({p[1] for p in inserts}, {41, 43})   # matched to each course's assignment
        self.assertIn(("/repos/luca/coursework/commits", "Fall-2026/ECE129"), asked)   # commits are filtered by course folder


if __name__ == "__main__":
    unittest.main()
