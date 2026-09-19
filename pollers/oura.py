"""Oura poller cycle. Writes oura_daily."""
import time
from datetime import date, datetime, timedelta

import requests

from core import notifier
from core.config import get
from db.db import execute_query, fetch_all
from logs.error_handler import log_error

SCRIPT = "oura"
TIMEOUT = 15
API = "https://api.ouraring.com/v2/usercollection"


class AuthFailure(Exception):
    pass


def _metric(endpoint, t0, status):
    try:
        execute_query("INSERT INTO poll_metrics (api_name, endpoint, response_time_us, http_status) VALUES (%s,%s,%s,%s)",
                      ("oura", endpoint[:512], (time.perf_counter_ns() - t0) // 1000, status))
    except Exception as e:
        log_error(SCRIPT, type(e).__name__, "poll_metrics insert", str(e))


def _request(cfg, collection, start, end):
    headers = {"Authorization": f"Bearer {get(cfg, 'oura.pat')}"}
    data, token = [], None
    while True:
        params = {"start_date": start.isoformat(), "end_date": end.isoformat()}
        if token:
            params["next_token"] = token
        t0 = time.perf_counter_ns()
        try:
            r = requests.get(f"{API}/{collection}", headers=headers, params=params, timeout=TIMEOUT)
        except requests.RequestException as e:
            _metric(f"/v2/usercollection/{collection}", t0, 0)
            raise RuntimeError(f"oura request failed: {e}") from e
        _metric(f"/v2/usercollection/{collection}", t0, r.status_code)
        if r.status_code == 401:
            raise AuthFailure(f"oura 401 on {collection}")
        if r.status_code >= 400:
            raise RuntimeError(f"oura {r.status_code} on {collection}: {r.text[:200]}")
        body = r.json()
        data.extend(body.get("data") or [])
        token = body.get("next_token")
        if not token:
            return data


def _nearest_slot(cfg, now=None):
    now = now or datetime.now()
    best, best_i = None, 0
    for i, hhmm in enumerate(get(cfg, "oura.poll_times", []) or []):
        try:
            h, m = (int(x) for x in str(hhmm).split(":"))
        except ValueError:
            continue
        d = abs((now - now.replace(hour=h, minute=m, second=0, microsecond=0)).total_seconds())
        if best is None or d < best:
            best, best_i = d, i
    return best_i


def _long_sleep(docs):
    """Pick one sleep document per day: the longest long_sleep, else the longest of any type."""
    by_day = {}
    for d in docs:
        day = d.get("day")
        if not day:
            continue
        cur = by_day.get(day)
        score = (1 if d.get("type") == "long_sleep" else 0, d.get("total_sleep_duration") or 0)
        if cur is None or score > cur[0]:
            by_day[day] = (score, d)
    return {k: v[1] for k, v in by_day.items()}


def run_cycle(cfg):
    end = date.today()
    start = end - timedelta(days=int(get(cfg, "oura.backfill_days", 7)))
    sleep_scores = {d["day"]: d for d in _request(cfg, "daily_sleep", start, end) if d.get("day")}
    readiness = {d["day"]: d for d in _request(cfg, "daily_readiness", start, end) if d.get("day")}
    sleeps = _long_sleep(_request(cfg, "sleep", start, end))
    existing = {r["date"].isoformat(): r for r in fetch_all(
        "SELECT date, missing FROM oura_daily WHERE date BETWEEN %s AND %s", (start, end))}
    days_with_data = 0
    for i in range((end - start).days + 1):
        day = (start + timedelta(days=i)).isoformat()
        ss, rd, sl = sleep_scores.get(day), readiness.get(day), sleeps.get(day)
        try:
            if ss or rd or sl:
                days_with_data += 1
                was_missing = existing.get(day, {}).get("missing")
                execute_query(
                    "INSERT INTO oura_daily (date, sleep_score, readiness_score, hrv_avg, resting_hr, total_sleep_seconds, "
                    "sleep_efficiency, data_source, missing, filled_at) VALUES (%s,%s,%s,%s,%s,%s,%s,'api',FALSE,%s) "
                    "ON DUPLICATE KEY UPDATE sleep_score=VALUES(sleep_score), readiness_score=VALUES(readiness_score), "
                    "hrv_avg=VALUES(hrv_avg), resting_hr=VALUES(resting_hr), total_sleep_seconds=VALUES(total_sleep_seconds), "
                    "sleep_efficiency=VALUES(sleep_efficiency), data_source='api', missing=FALSE, "
                    "filled_at=COALESCE(filled_at, VALUES(filled_at))",
                    (day, (ss or {}).get("score"), (rd or {}).get("score"), (sl or {}).get("average_hrv"),
                     (sl or {}).get("lowest_heart_rate"), (sl or {}).get("total_sleep_duration"),
                     (sl or {}).get("efficiency"), datetime.now() if was_missing else None))
            elif day not in existing:
                execute_query("INSERT INTO oura_daily (date, data_source, missing) VALUES (%s, 'estimated', TRUE)", (day,))
        except Exception as e:
            log_error(SCRIPT, type(e).__name__, f"oura_daily upsert {day}", str(e))
    today = end.isoformat()
    if not (sleep_scores.get(today) or sleeps.get(today)) and \
            _nearest_slot(cfg) == int(get(cfg, "oura.morning_poll_index", 0)):
        notifier.send_warning("No Oura sleep data for today yet. Open the Oura app to sync the ring.")
    return None
