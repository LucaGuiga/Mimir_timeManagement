"""Tracks DeepSeek tokens and estimated dollars per billing period, warns on Telegram when the period is on track to pass
the cap, and supplies the usage lines for the email summaries. Dollar figures are estimates from the prices in config."""
from calendar import monthrange
from datetime import date, datetime, timedelta

from core import notifier
from core.config import get
from db.db import execute_query, fetch_one
from logs.error_handler import log_error

SCRIPT = "deepseek_usage"
# Dollars per million tokens. Defaults are placeholders: check platform.deepseek.com/pricing and set the deepseek.price_* keys.
DEFAULT_PRICES = {"input": 0.28, "cache_hit": 0.028, "output": 0.42}


def cap_usd(cfg):
    return float(get(cfg, "deepseek.monthly_cap_usd", 10) or 10)


def period(cfg, today=None):
    """(start, end) dates of the billing period containing today. billing_day is the day of the month a period starts."""
    today = today or date.today()
    day = max(1, min(28, int(get(cfg, "deepseek.billing_day", 1) or 1)))
    if today.day >= day:
        start = today.replace(day=day)
    else:
        prev = today.replace(day=1) - timedelta(days=1)
        start = prev.replace(day=day)
    nxt_month = start.replace(day=1) + timedelta(days=monthrange(start.year, start.month)[1])
    return start, nxt_month.replace(day=day) - timedelta(days=1)


def cost(cfg, prompt, completion, cache_hit=0):
    p_in = float(get(cfg, "deepseek.price_input_per_m", DEFAULT_PRICES["input"]))
    p_hit = float(get(cfg, "deepseek.price_cache_hit_per_m", DEFAULT_PRICES["cache_hit"]))
    p_out = float(get(cfg, "deepseek.price_output_per_m", DEFAULT_PRICES["output"]))
    hit = min(cache_hit, prompt)
    return ((prompt - hit) * p_in + hit * p_hit + completion * p_out) / 1e6


def record(cfg, kind, usage):
    """Store one call's usage (the 'usage' object from a DeepSeek response), then check the budget. Never raises."""
    try:
        if not isinstance(usage, dict):
            return
        prompt, completion = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        hit = int(usage.get("prompt_cache_hit_tokens") or 0)
        execute_query("INSERT INTO deepseek_usage (kind, prompt_tokens, completion_tokens, cache_hit_tokens, cost_usd) VALUES (%s,%s,%s,%s,%s)",
                      (kind[:40], prompt, completion, hit, round(cost(cfg, prompt, completion, hit), 6)))
        check_budget(cfg)
    except Exception as e:
        log_error(SCRIPT, type(e).__name__, "record", str(e))


def period_total(cfg, today=None):
    """Totals for the whole billing period so far (every call in it, not just one email's). Returns None if unavailable."""
    try:
        start, end = period(cfg, today)
        row = fetch_one("SELECT COALESCE(SUM(prompt_tokens),0) p, COALESCE(SUM(completion_tokens),0) c, COALESCE(SUM(cost_usd),0) usd, "
                        "COUNT(*) n FROM deepseek_usage WHERE called_at >= %s AND called_at < %s",
                        (start, end + timedelta(days=1)))
        return {"start": start, "end": end, "prompt": int(row["p"]), "completion": int(row["c"]),
                "tokens": int(row["p"]) + int(row["c"]), "usd": float(row["usd"]), "calls": int(row["n"])}
    except Exception as e:
        log_error(SCRIPT, type(e).__name__, "period_total", str(e))
        return None


def projected(total, now=None):
    """Spend at the end of the period if the pace so far continues. Needs at least one day of history to avoid wild guesses."""
    now = now or datetime.now()
    start = datetime.combine(total["start"], datetime.min.time())
    days_elapsed = max((now - start).total_seconds() / 86400, 1.0)
    days_total = (total["end"] - total["start"]).days + 1
    return total["usd"] / days_elapsed * days_total


def _alert_once(start, kind, text):
    """Send a Telegram warning at most once per period and kind. The marker is stored first so a send failure never repeats."""
    if fetch_one("SELECT 1 AS x FROM deepseek_alert WHERE period_start = %s AND kind = %s", (start, kind)):
        return False
    execute_query("INSERT INTO deepseek_alert (period_start, kind) VALUES (%s,%s)", (start, kind))
    notifier.send_warning(text)
    return True


def check_budget(cfg, now=None):
    """Warn when spend has reached the cap, or the current pace will reach it before the period ends."""
    total = period_total(cfg, now.date() if now else None)
    if not total:
        return None
    cap = cap_usd(cfg)
    span = f"{total['start']:%b %d} to {total['end']:%b %d}"
    if total["usd"] >= cap:
        _alert_once(total["start"], "reached", f"DeepSeek spend is about ${total['usd']:.2f} this billing period ({span}), "
                                                f"at or over your ${cap:.2f} limit.")
        return "reached"
    proj = projected(total, now)
    if proj >= cap:
        _alert_once(total["start"], "projected", f"DeepSeek spend is about ${total['usd']:.2f} so far this billing period ({span}). "
                                                  f"At this pace it will reach about ${proj:.2f}, over your ${cap:.2f} limit.")
        return "projected"
    return None


def email_rows(cfg):
    """Lines for the email summaries: the billing period total, not this email's usage and not a calendar month."""
    t = period_total(cfg)
    if not t:
        return ["unavailable"]
    return [f"Billing period {t['start']:%b %d} to {t['end']:%b %d} (so far)",
            f"Tokens: {t['tokens']:,} total ({t['prompt']:,} in, {t['completion']:,} out) across {t['calls']} calls",
            f"Estimated cost: ${t['usd']:.2f} of ${cap_usd(cfg):.2f} limit"]
