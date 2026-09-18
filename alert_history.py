"""Persisted alert history - the last MAX_HISTORY alerts, saved to disk so
the Alerts section (dashboard and desktop GUI alike) survives an app
restart instead of coming back empty. Kept out of git (see .gitignore) -
it's runtime state, not configuration.
"""
import datetime
import json
import os

ALERTS_FILE = 'data/alerts_history.json'
MAX_HISTORY = 50


def _json_default(value):
    """gui.py's alerts still carry a raw datetime in 'time' (server.py's
    DashboardState converts it to a string before this is ever called) -
    handle it here too so this module doesn't care which caller it is."""
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return str(value)


def load():
    """Returns the persisted alerts, newest first (empty list if none saved
    yet or the file is missing/corrupt)."""
    if not os.path.exists(ALERTS_FILE):
        return []
    try:
        with open(ALERTS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save(alerts):
    """Persists the newest MAX_HISTORY alerts. `alerts` must already be
    ordered newest-first and be plain JSON-serializable dicts (datetimes
    converted to strings before this is called)."""
    os.makedirs(os.path.dirname(ALERTS_FILE), exist_ok=True)
    try:
        with open(ALERTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(list(alerts)[:MAX_HISTORY], f, indent=2, default=_json_default)
    except OSError:
        pass


def clear():
    try:
        os.remove(ALERTS_FILE)
    except FileNotFoundError:
        pass
