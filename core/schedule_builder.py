"""Daily schedule re evaluation. Never rebuilds blocks, only skips or shifts them."""
from datetime import date, datetime, time, timedelta

from core import stress
from core.config import get, load_config
from db.db import execute_query, fetch_all
from logs.error_handler import log_error

SCRIPT = "schedule_builder"
HARD = ("class", "fixed", "clubs")


def _mins(td):
    return int(td.total_seconds() // 60)


def _td(mins):
    return timedelta(minutes=mins)


def skip_block(block_id, reason):
    log_error(SCRIPT, "BlockSkipped", f"skip_block {block_id}", reason)
    return execute_query("UPDATE schedule_blocks SET skipped = TRUE, skip_reason = %s WHERE id = %s", (reason[:255], int(block_id)))


def unskip_block(block_id):
    return execute_query("UPDATE schedule_blocks SET skipped = FALSE, skip_reason = NULL WHERE id = %s", (int(block_id),))


def _waking_window(blocks, s):
    classes = [b for b in blocks if b["time_category"] == "class" and not b["skipped"]]
    wake = 7 * 60
    if classes:
        wake = min(wake, _mins(min(b["start_time"] for b in classes)) - 60)
    wake = max(0, wake)
    return wake, min(24 * 60, wake + int(round((24 - s) * 60)))


def _overlaps(a0, a1, b0, b1):
    return a0 < b1 and b0 < a1


def run_for_today(d=None, cfg=None):
    cfg = cfg or load_config()
    d = d or date.today()
    result = stress.calculate_and_store(d, cfg)
    blocks = fetch_all("SELECT * FROM schedule_blocks WHERE date = %s ORDER BY start_time, id", (d,))
    skipped, moved = [], []
    if result["ratio"] > float(get(cfg, "stress.amber_below", 1.0)):
        for b in blocks:
            if b["time_category"] == "clubs" and b["moveable"] and not b["skipped"]:
                skip_block(b["id"], "stress_overloaded")
                b["skipped"], b["skip_reason"] = True, "stress_overloaded"
                skipped.append(b["id"])
        if skipped:
            result = stress.calculate_and_store(d, cfg)
    wake, bed = _waking_window(blocks, result["hours_slept"])
    occupied = [(_mins(b["start_time"]), _mins(b["end_time"]), b["id"]) for b in blocks if not b["skipped"]]
    for b in sorted(blocks, key=lambda x: (x["start_time"], x["id"])):
        if b["time_category"] != "flexible" or b["skipped"]:
            continue
        start, end = _mins(b["start_time"]), _mins(b["end_time"])
        hard = [(s0, e0) for s0, e0, i in occupied if i != b["id"] and next(x for x in blocks if x["id"] == i)["time_category"] in HARD]
        if not any(_overlaps(start, end, s0, e0) for s0, e0 in hard):
            continue
        length = end - start
        others = [(s0, e0) for s0, e0, i in occupied if i != b["id"]]
        candidates = sorted({start, wake} | {e0 for _, e0 in others if e0 >= start})
        new_start = next((c for c in candidates if c >= start and c + length <= bed
                          and not any(_overlaps(c, c + length, s0, e0) for s0, e0 in others)), None)
        if new_start is None:
            skip_block(b["id"], "no_gap")
            b["skipped"] = True
            skipped.append(b["id"])
            occupied = [o for o in occupied if o[2] != b["id"]]
            continue
        execute_query("UPDATE schedule_blocks SET original_start_time = COALESCE(original_start_time, start_time), "
                      "start_time = %s, end_time = %s WHERE id = %s",
                      (str(_td(new_start)), str(_td(new_start + length)), b["id"]))
        occupied = [o for o in occupied if o[2] != b["id"]] + [(new_start, new_start + length, b["id"])]
        moved.append(b["id"])
    if skipped or moved:
        result = stress.calculate_and_store(d, cfg)
    return {"date": d, "ratio": result["ratio"], "band": result["band"], "skipped": skipped, "moved": moved}
