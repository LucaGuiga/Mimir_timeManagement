"""Syllabus text extraction and Claude based parsing. Called only from the GUI."""
import io
import json
import re
from datetime import date, datetime, time, timedelta
from html.parser import HTMLParser

import anthropic
import requests

from core.config import get, load_config
from db.db import execute_query, fetch_all, fetch_one
from logs.error_handler import log_error
from pollers.canvas import AuthFailure, _request as canvas_request, find_syllabus_sources

SCRIPT = "syllabus_parser"
TYPES = ("hw", "quiz", "test", "lab", "project_milestone", "reading", "other")
MAX_CHARS = 400_000
EST_OUTPUT_TOKENS = 4000
# USD per one million tokens: (input, output). Unknown models fall back to the Opus row.
PRICES = {
    "claude-sonnet-5": (2.00, 10.00), "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-5": (5.00, 25.00), "claude-opus-4-8": (5.00, 25.00), "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00), "claude-haiku-4-5": (1.00, 5.00),
    "claude-fable-5-1": (10.00, 50.00), "claude-fable-5": (10.00, 50.00),
}
SYSTEM_PROMPT = """You extract the assignment calendar from a university course syllabus.
Respond with one JSON object only. No prose, no markdown fences, no comments.
Shape:
{"professor_name": string,
 "items": [{"title": string,
            "assignment_type": one of "hw","quiz","test","lab","project_milestone","reading","other",
            "assignment_number": integer or null,
            "predicted_open": "YYYY-MM-DD" or null,
            "predicted_due": "YYYY-MM-DD" or null,
            "notes": string}]}
Rules: one item per graded deliverable or reading with a date. assignment_number is the number in the title
(Homework 3 -> 3), null when there is none. Resolve relative dates such as "Week 4 Friday" using the quarter
start date given in the user message. Use null for any date you cannot determine. Keep notes under 200 characters."""


class _Stripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "table"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html):
    s = _Stripper()
    s.feed(html or "")
    return re.sub(r"\n{3,}", "\n\n", "".join(s.parts)).strip()


def quarter_window(cfg, today=None):
    """(start, end) dates for the configured quarter label, generous at both ends."""
    today = today or date.today()
    label = (get(cfg, "quarter_label", "") or "").lower()
    m = re.search(r"(20\d\d)", label)
    year = int(m.group(1)) if m else today.year
    seasons = {"fall": ((9, 1), (12, 31)), "winter": ((1, 1), (3, 31)), "spring": ((3, 20), (6, 30)), "summer": ((6, 10), (9, 15))}
    for name, ((m0, d0), (m1, d1)) in seasons.items():
        if name in label:
            return date(year, m0, d0) - timedelta(days=14), date(year, m1, d1) + timedelta(days=14)
    return today - timedelta(days=180), today + timedelta(days=180)


def _course(course_id):
    row = fetch_one("SELECT * FROM courses WHERE id = %s", (int(course_id),))
    if not row:
        raise ValueError(f"course {course_id} not found")
    return row


def fetch_source(cfg, course_id, source):
    """source is 'body' or a Canvas file id. Returns plain text."""
    course = _course(course_id)
    if source == "body":
        body = find_syllabus_sources(cfg, course["canvas_course_id"]).get("syllabus_body")
        if not body:
            raise ValueError("this course has no syllabus body on Canvas")
        return html_to_text(body)
    meta = canvas_request(cfg, f"/api/v1/files/{int(source)}").json()
    r = requests.get(meta["url"], headers={"Authorization": f"Bearer {get(cfg, 'canvas.token')}"}, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"file download failed with http {r.status_code}")
    ctype = (meta.get("content-type") or meta.get("content_type") or r.headers.get("Content-Type") or "").lower()
    name = (meta.get("display_name") or "").lower()
    if "pdf" in ctype or name.endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(r.content))
        return "\n".join((p.extract_text() or "") for p in reader.pages).strip()
    if "html" in ctype or name.endswith((".html", ".htm")):
        return html_to_text(r.content.decode(r.encoding or "utf-8", errors="replace"))
    if ctype.startswith("text/") or name.endswith((".txt", ".md")):
        return r.content.decode(r.encoding or "utf-8", errors="replace")
    raise ValueError(f"unsupported syllabus file type: {ctype or name or 'unknown'}")


def spent_this_quarter(course_id, quarter):
    row = fetch_one("SELECT COALESCE(SUM(parse_cost_usd), 0) AS c FROM syllabus_parsed WHERE course_id = %s AND quarter <=> %s",
                    (int(course_id), quarter))
    return float(row["c"] or 0)


def _price(model):
    return PRICES.get(model) or PRICES["claude-opus-5"]


def _validate(data, window):
    """Returns (errors, cleaned) for the parsed JSON object."""
    errors = []
    if not isinstance(data, dict):
        return ["response is not a JSON object"], None
    prof = data.get("professor_name")
    if not isinstance(prof, str):
        errors.append("professor_name must be a string")
    items = data.get("items")
    if not isinstance(items, list):
        return errors + ["items must be a list"], None
    cleaned = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            errors.append(f"items[{i}] is not an object"); continue
        if not isinstance(it.get("title"), str) or not it["title"].strip():
            errors.append(f"items[{i}].title must be a non empty string")
        if it.get("assignment_type") not in TYPES:
            errors.append(f"items[{i}].assignment_type must be one of {', '.join(TYPES)}")
        n = it.get("assignment_number")
        if n is not None and (not isinstance(n, int) or isinstance(n, bool)):
            errors.append(f"items[{i}].assignment_number must be an integer or null")
        for key in ("predicted_open", "predicted_due"):
            v = it.get(key)
            if v is None:
                continue
            try:
                d = date.fromisoformat(v) if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) else None
            except ValueError:
                d = None
            if d is None:
                errors.append(f"items[{i}].{key} must be YYYY-MM-DD or null")
            elif not (window[0] <= d <= window[1]):
                errors.append(f"items[{i}].{key} {v} is outside the quarter window {window[0]}..{window[1]}")
        if not isinstance(it.get("notes", ""), str):
            errors.append(f"items[{i}].notes must be a string")
        cleaned.append({"title": (it.get("title") or "").strip(), "assignment_type": it.get("assignment_type"),
                        "assignment_number": n, "predicted_open": it.get("predicted_open"),
                        "predicted_due": it.get("predicted_due"), "notes": it.get("notes") or "",
                        "matched_assignment_id": None})
    return errors, {"professor_name": prof, "items": cleaned}


def _norm(title):
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def _match_items(course_id, items):
    rows = fetch_all("SELECT id, title, assignment_type, assignment_number FROM assignments WHERE course_id = %s", (int(course_id),))
    by_key = {(r["assignment_type"], r["assignment_number"]): r["id"] for r in rows if r["assignment_number"] is not None}
    by_title = {_norm(r["title"]): r["id"] for r in rows}
    matched = 0
    for it in items:
        aid = by_key.get((it["assignment_type"], it["assignment_number"])) if it["assignment_number"] is not None else None
        aid = aid or by_title.get(_norm(it["title"]))
        it["matched_assignment_id"] = aid
        if aid:
            matched += 1
            execute_query("UPDATE assignments SET syllabus_predicted_open = %s, syllabus_predicted_due = %s WHERE id = %s",
                          (_at(it["predicted_open"]), _at(it["predicted_due"]), aid))
    return matched


def _at(day, hour=23, minute=59):
    return datetime.combine(date.fromisoformat(day), time(hour, minute)) if day else None


def _extract_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise
        return json.loads(m.group())


def parse(cfg, course_id, raw_text):
    """Never raises. Returns {status, message, cost, matched, unmatched, parsed_id}."""
    result = {"status": "error", "message": "", "cost": 0.0, "matched": 0, "unmatched": 0, "parsed_id": None}
    try:
        course = _course(course_id)
        quarter = get(cfg, "quarter_label", "") or None
        model = get(cfg, "anthropic.parse_model", "claude-sonnet-5")
        if not raw_text or not raw_text.strip():
            result["message"] = "syllabus text is empty"
            return result
        if len(raw_text) > MAX_CHARS:
            result["message"] = f"syllabus text is {len(raw_text)} characters, above the {MAX_CHARS} limit; trim it first"
            return result
        pin, pout = _price(model)
        est = (len(raw_text) / 4 + len(SYSTEM_PROMPT) / 4) * pin / 1e6 + EST_OUTPUT_TOKENS * pout / 1e6
        budget = (get(cfg, "anthropic.per_class_claude_budget_usd", {}) or {}).get(course["canvas_course_name"])
        spent = spent_this_quarter(course_id, quarter)
        if budget is not None and spent + est > float(budget):
            result["status"] = "refused"
            result["message"] = (f"budget for {course['canvas_course_name']} is ${float(budget):.2f}, "
                                 f"${spent:.4f} already spent and this parse is estimated at ${est:.4f}")
            return result
        window = quarter_window(cfg)
        client = anthropic.Anthropic(api_key=get(cfg, "anthropic.api_key"))
        response = client.messages.create(
            model=model, max_tokens=16000, system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"Quarter: {quarter or 'unknown'} (starts about {window[0] + timedelta(days=14)}).\n"
                                                  f"Course: {course['canvas_course_name']}\n\nSYLLABUS:\n{raw_text}"}])
        usage = response.usage
        cost = (usage.input_tokens * pin + usage.output_tokens * pout) / 1e6
        result["cost"] = round(cost, 4)
        if response.stop_reason == "refusal":
            result["message"] = "the model declined to process this syllabus"
            return result
        text = "".join(b.text for b in response.content if b.type == "text")
        try:
            data = _extract_json(text)
            errors, cleaned = _validate(data, window)
        except ValueError as e:
            errors, cleaned, data = [f"response was not valid JSON: {e}"], None, {"raw": text[:5000]}
        validated = not errors
        stored = cleaned if cleaned is not None else data
        if not validated:
            stored = dict(stored or {}, validation_errors=errors)
        pid = execute_query(
            "INSERT INTO syllabus_parsed (course_id, raw_text, parsed_json, parse_model, parse_cost_usd, validated, human_reviewed, quarter) "
            "VALUES (%s,%s,%s,%s,%s,%s,FALSE,%s)",
            (course["id"], raw_text, json.dumps(stored), model, round(cost, 4), validated, quarter))
        result["parsed_id"] = pid
        if not validated:
            result["status"] = "invalid"
            result["message"] = "parsed output failed validation and was stored for review: " + "; ".join(errors[:5])
            log_error(SCRIPT, "ValidationFailed", f"parse course {course_id}", "; ".join(errors))
            return result
        matched = _match_items(course["id"], cleaned["items"])
        execute_query("UPDATE syllabus_parsed SET parsed_json = %s WHERE id = %s", (json.dumps(cleaned), pid))
        result.update(status="ok", matched=matched, unmatched=len(cleaned["items"]) - matched,
                      message=f"parsed {len(cleaned['items'])} items, {matched} matched, cost ${cost:.4f}")
        return result
    except AuthFailure as e:
        log_error(SCRIPT, "AuthFailure", f"parse course {course_id}", str(e), "critical")
        result["message"] = f"Canvas rejected the token: {e}"
    except anthropic.AuthenticationError as e:
        log_error(SCRIPT, "AuthenticationError", f"parse course {course_id}", str(e), "critical")
        result["message"] = "Anthropic API key was rejected"
    except anthropic.RateLimitError as e:
        log_error(SCRIPT, "RateLimitError", f"parse course {course_id}", str(e))
        result["message"] = "Anthropic rate limit hit, try again in a minute"
    except anthropic.APIStatusError as e:
        log_error(SCRIPT, "APIStatusError", f"parse course {course_id}", f"{e.status_code}: {e.message}")
        result["message"] = f"Anthropic API error {e.status_code}"
    except anthropic.APIConnectionError as e:
        log_error(SCRIPT, "APIConnectionError", f"parse course {course_id}", str(e))
        result["message"] = "could not reach the Anthropic API"
    except Exception as e:
        log_error(SCRIPT, type(e).__name__, f"parse course {course_id}", str(e))
        result["message"] = f"{type(e).__name__}: {e}"
    return result
