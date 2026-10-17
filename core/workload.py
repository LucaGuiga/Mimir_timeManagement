"""Estimated hours per assignment, per class. Used by the stress ratio and the study planner so both agree.

hours = e^(difficulty / curve) * hour_scale, curve = stress.difficulty_curve (default 1.248: difficulty 5 is about 55 hours)
difficulty: course_presets row for (class, type), else difficulty_presets in config.yaml (the start of quarter default).
hour_scale: courses.hour_scale when set, else stress.hour_scale_subjects[subject], else stress.hour_scale_default."""
import math
import re

from core.config import get
from db.db import fetch_all

DIFFICULTY_SCALE = 1.248
TYPES = ("hw", "quiz", "test", "lab", "project_milestone", "reading", "other")


def curve(cfg):
    return max(0.1, float(get(cfg or {}, "stress.difficulty_curve", DIFFICULTY_SCALE) or DIFFICULTY_SCALE))


def hours_for(difficulty, scale=1.0, k=DIFFICULTY_SCALE):
    return math.exp(float(difficulty) / k) * float(scale)


def subject(course):
    """Leading letters of the course code: ECE-129A-01 -> ECE."""
    m = re.match(r"[A-Za-z]+", (course.get("ical_course_code") or course.get("canvas_course_name") or "").strip())
    return m.group().upper() if m else ""


def default_scale(cfg, course):
    subjects = {str(k).upper(): float(v) for k, v in (get(cfg, "stress.hour_scale_subjects", {}) or {}).items()}
    return subjects.get(subject(course), float(get(cfg, "stress.hour_scale_default", 1.0)))


def scale_for(cfg, course):
    v = course.get("hour_scale")
    return float(v) if v is not None else default_scale(cfg, course)


def global_difficulty(cfg, atype):
    presets = get(cfg, "difficulty_presets", {}) or {}
    return float(presets.get(atype, presets.get("other", 2)))


class Presets:
    """Per class difficulty and hour scale, loaded once per calculation."""

    def __init__(self, cfg, courses=None, rows=None):
        self.cfg = cfg
        if courses is None:
            courses = fetch_all("SELECT id, ical_course_code, canvas_course_name, hour_scale FROM courses")
        if rows is None:
            rows = fetch_all("SELECT course_id, assignment_type, difficulty FROM course_presets")
        self.courses = {c["id"]: c for c in courses}
        self.difficulty_rows = {(r["course_id"], r["assignment_type"]): float(r["difficulty"]) for r in rows}

    def difficulty(self, course_id, atype):
        return self.difficulty_rows.get((course_id, atype), global_difficulty(self.cfg, atype))

    def scale(self, course_id):
        c = self.courses.get(course_id)
        return scale_for(self.cfg, c) if c else float(get(self.cfg, "stress.hour_scale_default", 1.0))

    def hours(self, course_id, atype):
        return hours_for(self.difficulty(course_id, atype or "other"), self.scale(course_id), curve(self.cfg))
