"""Telegram alerts. Never raises; failures go to logs/fallback.log."""
import json
import os
from datetime import datetime

import requests

from core.config import get, load_config, repo_root

MAX_LEN = 4000
TIMEOUT = 10
WARNING_MARK = "⚠️ WARNING: "
CRITICAL_MARK = "\U0001f6a8 CRITICAL: "


def fallback_path():
    try:
        log_dir = get(load_config(), "paths.log_dir", "logs")
    except Exception:
        log_dir = "logs"
    return os.path.join(repo_root(), log_dir, "fallback.log")


def write_fallback(entry):
    entry = dict(entry)
    entry.setdefault("timestamp", datetime.now().isoformat(timespec="microseconds"))
    try:
        path = fallback_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass


def send_message(text):
    text = str(text)
    if len(text) > MAX_LEN:
        text = text[:MAX_LEN - 3] + "..."
    try:
        cfg = load_config()
        token, chat_id = get(cfg, "telegram.bot_token"), get(cfg, "telegram.chat_id")
        if not token or not chat_id:
            raise ValueError("telegram.bot_token or telegram.chat_id not configured")
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": str(chat_id), "text": text, "disable_web_page_preview": True},
            timeout=TIMEOUT,
        )
        if r.status_code != 200 or not r.json().get("ok"):
            raise RuntimeError(f"telegram http {r.status_code}: {r.text[:200]}")
        return True
    except Exception as e:
        write_fallback({"script_name": "notifier", "error_type": type(e).__name__,
                        "operation": "send_message", "raw_message": str(e),
                        "severity": "warning", "unsent_text": text[:500]})
        return False


def send_warning(text):
    return send_message(WARNING_MARK + str(text))


def send_critical(text):
    return send_message(CRITICAL_MARK + str(text))
