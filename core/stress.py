"""Stress ratio S_day. See calculate() for the equation."""
from datetime import date, datetime, time, timedelta

from core import workload
from core.config import get, load_config
from db.db import execute_query, fetch_all, fetch_one
from logs.error_handler import log_error

SCRIPT = "stress"
DIFFICULTY_SCALE = workload.DIFFICULTY_SCALE


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


def calculate_plan(d=None, cfg=None):
    """Plan based stress (stress.equation: plan, the default): S = study hours needed in the planning window / free hours in that window.

    needed: for each open assignment, its remaining hours (estimate from core.workload minus study blocks already done),
    spread evenly to its due date; the part that falls inside the window (study.horizon_days, default 7) counts.
    Plus the sleep penalty, alpha * hours of sleep missed last night.
    free: on each day of the window, the time between study.day_start and study.latest_end not taken by blocks other than
    study blocks (classes, blocks added by hand), minus meals, chores and commute. Today counts from now.
    S = 1.0 means the window is exactly full. t_available holds the free hours, deadline_term the hours needed."""
    from core import planner
    cfg = cfg or load_config()
    d = d or date.today()
    if isinstance(d, str):
        d = date.fromisoformat(d)
    now = datetime.now()
    ref = now if d == now.date() else datetime.combine(d, time(0, 0))
    st = planner.settings(cfg)
    window = st["horizon_days"]
    days = [d + timedelta(days=i) for i in range(window)]
    tp = get(cfg, "time_presets", {}) or {}
    s, _ = hours_slept(d, cfg)
    alpha, target = float(tp.get("alpha", 2.0)), float(tp.get("sleep_target_hours", 8))
    sleep_penalty = alpha * max(0.0, target - s)

    blocks = fetch_all("SELECT date, time_category, start_time, end_time, skipped, source FROM schedule_blocks WHERE date >= %s AND date <= %s",
                       (days[0], days[-1]))
    now_min = now.hour * 60 + now.minute if d == now.date() else 0
    free_total, today = 0.0, {}
    for day in days:
        mine = [b for b in blocks if b["date"] == day and not b["skipped"]]
        busy = [(planner._mins(b["start_time"]), planner._mins(b["end_time"])) for b in mine if b["source"] != "study"]
        lo, hi = max(st["day_start"], now_min if day == d else 0), st["latest_end"]
        open_min = sum(e - s0 for s0, e in planner._free(busy, lo, hi, 0)) if hi > lo else 0
        cats = {b["time_category"] for b in mine}
        overhead = sum(float(tp.get(k, 0)) for k in ("breakfast_minutes", "lunch_minutes", "dinner_minutes"))
        overhead += 0 if "chores" in cats else float(tp.get("chores_minutes", 0))
        overhead += 0 if "travel" in cats else float(tp.get("commute_minutes", 0))
        span = max(1, st["latest_end"] - st["day_start"])
        overhead *= max(0.0, hi - lo) / span            # today: only the share of the day that is left
        free_total += max(0.0, open_min - overhead) / 60
        if day == d:
            today = {c: _hours([b for b in mine], c) for c in ("class", "fixed", "clubs", "travel", "chores")}
    floor = float(get(cfg, "stress.min_available_hours", 0.5))
    if free_total < floor:
        log_error(SCRIPT, "AvailableFloor", f"calculate {d}", f"free hours {free_total:.2f}h floored to {floor}h")
        free_total = floor

    presets = workload.Presets(cfg)
    done = {r["assignment_id"]: float(r["h"] or 0) / 3600 for r in fetch_all(
        "SELECT assignment_id, SUM(TIME_TO_SEC(TIMEDIFF(end_time, start_time))) AS h FROM schedule_blocks "
        "WHERE source = 'study' AND skipped = FALSE AND assignment_id IS NOT NULL AND TIMESTAMP(date, end_time) <= %s GROUP BY assignment_id", (ref,))}
    min_days = float(get(cfg, "stress.min_days_remaining", 0.25))
    needed = 0.0
    for a in fetch_all("SELECT a.id, a.course_id, a.assignment_type, a.due_at FROM assignments a JOIN courses c ON c.id = a.course_id "
                       "WHERE a.status IN ('pending','open') AND a.is_event = FALSE AND a.due_at IS NOT NULL AND a.due_at > %s AND c.active = TRUE", (ref,)):
        remaining = max(0.0, presets.hours(a["course_id"], a["assignment_type"]) - done.get(a["id"], 0.0))
        days_left = max(min_days, (a["due_at"] - ref).total_seconds() / 86400)
        needed += remaining * min(1.0, window / days_left)
    needed += sleep_penalty
    ratio = needed / free_total
    return {"date": d, "ratio": ratio, "t_awake": 24.0 - s, "t_class": today.get("class", 0.0), "t_travel": today.get("travel", 0.0),
            "t_clubs": today.get("clubs", 0.0), "t_chores": today.get("chores", 0.0), "t_fixed": today.get("fixed", 0.0),
            "t_available": free_total, "deadline_term": needed,
            "sleep_penalty": sleep_penalty, "hours_slept": s, "band": band(ratio, cfg)}


def calculate_daily(d=None, cfg=None):
    """Daily stress (stress.equation: daily), the original equation:
    S = (sum over open assignments of hours / days left + alpha * max(0, sleep target - hours slept)) / T_available
    T_available = (24 - hours slept) - class - fixed - clubs - travel - chores and meals, all for this one day."""
    cfg = cfg or load_config()
    d = d or date.today()
    if isinstance(d, str):
        d = date.fromisoformat(d)
    ref = datetime.now() if d == date.today() else datetime.combine(d, time(12, 0))
    presets = workload.Presets(cfg)   # per class difficulty and hour scale
    min_days = float(get(cfg, "stress.min_days_remaining", 0.25))
    deadline_term = 0.0
    for a in fetch_all("SELECT a.course_id, a.assignment_type, a.due_at FROM assignments a JOIN courses c ON c.id = a.course_id "
                       "WHERE a.status IN ('pending','open') AND a.is_event = FALSE AND a.due_at IS NOT NULL AND a.due_at > %s AND c.active = TRUE",
                       (ref - timedelta(days=1),)):
        days = max(min_days, (a["due_at"] - ref).total_seconds() / 86400)
        deadline_term += presets.hours(a["course_id"], a["assignment_type"]) / days
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


EQUATIONS = {"plan": "calculate_plan", "daily": "calculate_daily"}   # looked up when called


def calculate(d=None, cfg=None):
    """Both equations; the result is the primary one (stress.equation, default plan) with the other one's ratio
    alongside as alt_ratio / alt_band, so they can be compared."""
    cfg = cfg or load_config()
    primary = get(cfg, "stress.equation", "plan")
    primary = primary if primary in EQUATIONS else "plan"
    other = "daily" if primary == "plan" else "plan"
    r = globals()[EQUATIONS[primary]](d, cfg)
    try:
        alt = globals()[EQUATIONS[other]](d, cfg)["ratio"]
    except Exception as e:   # the comparison must never break the primary number
        log_error(SCRIPT, type(e).__name__, f"{other} equation", str(e))
        alt = None
    r.update(equation=primary, alt_equation=other, alt_ratio=alt, alt_band=band(alt, cfg) if alt is not None else None)
    return r



def calculate_and_store(d=None, cfg=None):
    r = calculate(d, cfg)
    r["calculated_at"] = datetime.now()
    r["id"] = execute_query(
        "INSERT INTO stress_scores (date, ratio, t_awake, t_class, t_travel, t_clubs, t_chores, t_fixed, t_available, "
        "deadline_term, sleep_penalty, hours_slept, calculated_at, equation, alt_ratio) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        tuple(r[k] for k in ("date", "ratio", "t_awake", "t_class", "t_travel", "t_clubs", "t_chores", "t_fixed",
                             "t_available", "deadline_term", "sleep_penalty", "hours_slept", "calculated_at", "equation", "alt_ratio")))
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
        row["equation"] = row.get("equation") or "daily"   # rows from before both equations existed
        row["alt_equation"] = "daily" if row["equation"] == "plan" else "plan"
        row["alt_band"] = band(row["alt_ratio"]) if row.get("alt_ratio") is not None else None
    return row
