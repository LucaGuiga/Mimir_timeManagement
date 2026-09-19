"""Stress ratio S_day. See calculate() for the equation."""
import math
from datetime import date, datetime, time, timedelta

from core.config import get, load_config
from db.db import execute_query, fetch_all, fetch_one
from logs.error_handler import log_error

SCRIPT = "stress"
DIFFICULTY_SCALE = 1.248


def _hours(blocks, category):
    total = 0.0
    for b in blocks:
        if b["time_category"] != category or b["skipped"]:
            continue
        span = (b["end_time"] - b["start_time"]).total_seconds()
        if span < 0:
            span += 86400
        total += span / 3600
    return total


def hours_slept(d, cfg):
    target = float(get(cfg, "time_presets.sleep_target_hours", 8))
    row = fetch_one("SELECT total_sleep_seconds, missing FROM oura_daily WHERE date = %s", (d,))
    if row and not row["missing"] and row["total_sleep_seconds"]:
        return row["total_sleep_seconds"] / 3600, "api"
    rows = fetch_all("SELECT total_sleep_seconds FROM oura_daily WHERE missing = FALSE AND total_sleep_seconds IS NOT NULL "
                     "AND date < %s ORDER BY date DESC LIMIT 7", (d,))
    if rows:
        return sum(r["total_sleep_seconds"] for r in rows) / len(rows) / 3600, "estimated"
    return target, "default"


def calculate(d=None, cfg=None):
    cfg = cfg or load_config()
    d = d or date.today()
    if isinstance(d, str):
        d = date.fromisoformat(d)
    ref = datetime.now() if d == date.today() else datetime.combine(d, time(12, 0))
    presets = get(cfg, "difficulty_presets", {}) or {}
    min_days = float(get(cfg, "stress.min_days_remaining", 0.25))
    deadline_term = 0.0
    for a in fetch_all("SELECT a.assignment_type, a.due_at FROM assignments a JOIN courses c ON c.id = a.course_id "
                       "WHERE a.status IN ('pending','open') AND a.due_at IS NOT NULL AND a.due_at > %s AND c.active = TRUE",
                       (ref - timedelta(days=1),)):
        x = float(presets.get(a["assignment_type"], presets.get("other", 2)))
        days = max(min_days, (a["due_at"] - ref).total_seconds() / 86400)
        deadline_term += math.exp(x / DIFFICULTY_SCALE) / days
    s, _ = hours_slept(d, cfg)
    tp = get(cfg, "time_presets", {}) or {}
    alpha = float(tp.get("alpha", 2.0))
    target = float(tp.get("sleep_target_hours", 8))
    sleep_penalty = alpha * max(0.0, target - s)
    blocks = fetch_all("SELECT time_category, start_time, end_time, skipped FROM schedule_blocks WHERE date = %s", (d,))
    t_awake = 24.0 - s
    t_class, t_fixed, t_clubs = _hours(blocks, "class"), _hours(blocks, "fixed"), _hours(blocks, "clubs")
    t_travel = _hours(blocks, "travel") if any(b["time_category"] == "travel" for b in blocks) else float(tp.get("commute_minutes", 0)) / 60
    t_chores = _hours(blocks, "chores") if any(b["time_category"] == "chores" for b in blocks) else float(tp.get("chores_minutes", 0)) / 60
    t_chores += sum(float(tp.get(k, 0)) for k in ("breakfast_minutes", "lunch_minutes", "dinner_minutes")) / 60
    t_available = t_awake - t_class - t_fixed - t_clubs - t_travel - t_chores
    floor = float(get(cfg, "stress.min_available_hours", 0.5))
    if t_available < floor:
        log_error(SCRIPT, "AvailableFloor", f"calculate {d}", f"t_available={t_available:.2f}h floored to {floor}h")
        t_available = floor
    ratio = (deadline_term + sleep_penalty) / t_available
    return {"date": d, "ratio": ratio, "t_awake": t_awake, "t_class": t_class, "t_travel": t_travel, "t_clubs": t_clubs,
            "t_chores": t_chores, "t_fixed": t_fixed, "t_available": t_available, "deadline_term": deadline_term,
            "sleep_penalty": sleep_penalty, "hours_slept": s, "band": band(ratio, cfg)}


def calculate_and_store(d=None, cfg=None):
    r = calculate(d, cfg)
    r["calculated_at"] = datetime.now()
    r["id"] = execute_query(
        "INSERT INTO stress_scores (date, ratio, t_awake, t_class, t_travel, t_clubs, t_chores, t_fixed, t_available, "
        "deadline_term, sleep_penalty, hours_slept, calculated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        tuple(r[k] for k in ("date", "ratio", "t_awake", "t_class", "t_travel", "t_clubs", "t_chores", "t_fixed",
                             "t_available", "deadline_term", "sleep_penalty", "hours_slept", "calculated_at")))
    return r


def band(ratio, cfg=None):
    cfg = cfg or load_config()
    if ratio < float(get(cfg, "stress.green_below", 0.8)):
        return "green"
    if ratio < float(get(cfg, "stress.amber_below", 1.0)):
        return "amber"
    return "red"


def latest_for(d=None):
    d = d or date.today()
    row = fetch_one("SELECT * FROM stress_scores WHERE date = %s ORDER BY calculated_at DESC LIMIT 1", (d,))
    if row:
        row["band"] = band(row["ratio"])
    return row
