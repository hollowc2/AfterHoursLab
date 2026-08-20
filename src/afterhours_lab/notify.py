"""Optional Telegram alert on archive-earnings failure.

Mirrors Butterflyguy's `notify.send()` convention (same env vars, same shape) rather
than inventing a new alerting channel for a single-operator deploy. No-ops (returns
None) when TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID aren't set, so a deploy that hasn't
configured Telegram still runs — it just falls back to last_run_status.json as its
only failure signal.
"""

from __future__ import annotations

import json
import os
import urllib.request


def send(message: str) -> bool | None:
    """Send a Telegram message.

    Returns None when TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID aren't set — not configured,
    not an error. Returns True/False once delivery was actually attempted, for
    success/failure respectively. Callers should only treat False as worth logging;
    None just means this deploy hasn't set up Telegram.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        return None

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": message}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False
