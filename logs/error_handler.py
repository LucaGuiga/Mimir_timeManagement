"""Central error log. Writes to error_log, escalates criticals to Telegram."""
import json
from datetime import date, datetime, timedelta

from core import notifier
from db.db import DBError, execute_query, fetch_all, fetch_one


def log_error(script_name, error_type, operation, raw_message, severity="warning"):
    severity = severity if severity in ("warning", "critical") else "warning"
    entry = {"timestamp": datetime.now().isoformat(timespec="microseconds"),
             "script_name": script_name, "error_type": str(error_type),
             "operation": operation, "raw_message": str(raw_message), "severity": severity}
    row_id = None
    try:
        row_id = execute_query(
            "INSERT INTO error_log (script_name, error_type, operation, raw_message, severity) "
            "VALUES (%s, %s, %s, %s, %s)",
            (script_name, str(error_type)[:128], operation[:255], str(raw_message), severity))
    except DBError as e:
        entry["db_write_error"] = str(e)
        notifier.write_fallback(entry)
        notifier.send_critical(f"error_log write failed ({e}); original: [{script_name}] {operation}: {str(raw_message)[:300]}")
        return None
    if severity == "critical":
        notifier.send_critical(f"[{script_name}] {operation}: {str(raw_message)[:300]}")
    return row_id


def log_assertion(script_name, operation, condition, message):
    if condition:
        return True
    log_error(script_name, "AssertionFailed", operation, message, "warning")
    return False


def get_todays_errors():
    return errors_for_date(date.today())


def errors_for_date(d):
    if isinstance(d, str):
        d = date.fromisoformat(d)
    start = datetime.combine(d, datetime.min.time())
    return fetch_all(
        "SELECT * FROM error_log WHERE timestamp >= %s AND timestamp < %s ORDER BY timestamp",
        (start, start + timedelta(days=1)))


def get_unacknowledged(severity=None, script_name=None, date_from=None, date_to=None, page=1, per_page=50):
    where, params = ["acknowledged = FALSE"], []
    if severity:
        where.append("severity = %s"); params.append(severity)
    if script_name:
        where.append("script_name = %s"); params.append(script_name)
    if date_from:
        where.append("timestamp >= %s"); params.append(date_from)
    if date_to:
        where.append("timestamp < %s"); params.append(date_to)
    clause = " AND ".join(where)
    total = fetch_one(f"SELECT COUNT(*) AS n FROM error_log WHERE {clause}", params)["n"]
    page, per_page = max(1, int(page)), max(1, int(per_page))
    rows = fetch_all(
        f"SELECT * FROM error_log WHERE {clause} ORDER BY timestamp DESC LIMIT %s OFFSET %s",
        params + [per_page, (page - 1) * per_page])
    return rows, total


def acknowledge_error(error_id):
    return execute_query("UPDATE error_log SET acknowledged = TRUE WHERE id = %s", (int(error_id),))


def unacknowledged_critical_count():
    row = fetch_one("SELECT COUNT(*) AS n FROM error_log WHERE acknowledged = FALSE AND severity = 'critical'")
    return row["n"] if row else 0


def to_json(rows):
    return json.dumps(rows, default=str)
