"""Filename schema for course repos: {Type}{number}_q{question}_part{part}_{COURSECODE}[.ext]"""
import os
import re

from core.config import get

TYPES = ("Lab", "HW", "Test", "Quiz", "Project", "Reading")
_TYPE_RE = "|".join(TYPES)
_RE = re.compile(rf"^(?P<type>{_TYPE_RE})(?P<number>\d+)_q(?P<question>\d+)_part(?P<part>\d+)_(?P<code>[A-Za-z0-9]+)(?P<ext>\.[A-Za-z0-9.]+)?$", re.I)
DEFAULT_CODE_PATTERN = r"[A-Z]+\d+"


class NamingSchemaError(Exception):
    def __init__(self, message, filename=None):
        super().__init__(message)
        self.filename = filename

    @property
    def user_message(self):
        prefix = f"{self.filename}: " if self.filename else ""
        return (f"{prefix}{self.args[0]}. Expected {{Type}}{{number}}_q{{question}}_part{{part}}_{{COURSECODE}}, "
                f"for example Lab1_q1_part0_ECE1 (types: {', '.join(TYPES)}; part0 means a single file).")


def _normalise_type(t):
    return next(x for x in TYPES if x.lower() == t.lower())


def parse_filename(filename):
    base = os.path.basename(filename)
    m = _RE.match(base)
    if not m:
        if "_" not in base:
            raise NamingSchemaError("name has no underscore separated parts", base)
        head = base.split("_", 1)[0]
        if not re.match(rf"^({_TYPE_RE})\d+$", head, re.I):
            raise NamingSchemaError(f"'{head}' is not a recognised type followed by a number", base)
        raise NamingSchemaError("name does not match the schema", base)
    number, question, part = m.group("number"), m.group("question"), m.group("part")
    for label, v, allow_zero in (("number", number, False), ("question", question, False), ("part", part, True)):
        if len(v) > 1 and v.startswith("0"):
            raise NamingSchemaError(f"{label} '{v}' has a leading zero", base)
        if not allow_zero and int(v) == 0:
            raise NamingSchemaError(f"{label} must be 1 or higher, got 0", base)
    return {"type": _normalise_type(m.group("type")), "number": int(number), "question": int(question), "part": int(part),
            "course_code": m.group("code"), "ext": m.group("ext") or "", "stem": base[: len(base) - len(m.group("ext") or "")]}


def format_expected(assignment_type, number, question, part, course_code):
    t = assignment_type if assignment_type in TYPES else next((x for x in TYPES if x.lower() == str(assignment_type).lower()), "HW")
    return f"{t}{number}_q{question}_part{part}_{course_code}"


def prefix_of(parsed):
    """Everything that stays constant across parts of one answer."""
    return f"{parsed['type']}{parsed['number']}_q{parsed['question']}"


def validate_filename(filename, cfg, siblings=None):
    """Returns {valid, errors, warnings}. siblings: other filenames (staged or in the repo) used for the part companion check;
    when None the part warning is emitted for any part >= 1."""
    result = {"valid": True, "errors": [], "warnings": []}
    try:
        p = parse_filename(filename)
    except NamingSchemaError as e:
        result["valid"] = False
        result["errors"].append(e.user_message)
        return result
    pattern = get(cfg, "repo_manager.course_code_pattern", DEFAULT_CODE_PATTERN) if cfg else DEFAULT_CODE_PATTERN
    if not re.fullmatch(pattern, p["course_code"]):
        result["valid"] = False
        result["errors"].append(f"{os.path.basename(filename)}: course code '{p['course_code']}' does not match the pattern {pattern}")
    if p["part"] >= 1:
        companion = False
        if siblings is not None:
            for other in siblings:
                try:
                    q = parse_filename(other)
                except NamingSchemaError:
                    continue
                if q["stem"] != p["stem"] and prefix_of(q) == prefix_of(p) and q["course_code"] == p["course_code"] and q["part"] != p["part"]:
                    companion = True
                    break
        if not companion:
            result["warnings"].append(f"{os.path.basename(filename)}: part{p['part']} indicates a multi part answer but no other part of "
                                      f"{prefix_of(p)} is staged or present; use part0 for a single file")
    return result
