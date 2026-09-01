"""
Headless entry point.

Runs StockAppBackend the same way gui.py does, but with no Tkinter window,
and serves a small web dashboard (status/alerts/logs/portfolio/sources/
keywords) over HTTP so the app can be checked from any device - a phone
browser, another PC, wherever - instead of only from whatever machine has
the desktop GUI installed.

This is meant to run on a server (a VM, not your desktop) with the port
reachable only over a private network like Tailscale (see DEPLOY.md) - the
whole dashboard also sits behind an HTTP Basic Auth login (set
DASHBOARD_USERNAME / DASHBOARD_PASSWORD in data/settings.json) as a second
layer, but that alone is not safe to expose to the raw public internet:
Basic Auth sends the password in a trivially-decodable form on every
request unless the connection is already encrypted, and Tailscale is what
provides that here.

Run with:  python server.py
"""
import collections
import datetime
import hmac
import os
import threading

# Every data file is addressed as "data/..." relative to the working
# directory. Pin it to this file's folder so the service behaves the same
# whether systemd set WorkingDirectory or someone ran it from elsewhere.
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, jsonify, request, send_from_directory

import config
from cloud_providers import PROVIDERS
from local_time import now_local
from main import StockAppBackend
from portfolio_manager import PortfolioManager
from portfolio_history import compute_history
from price_lookup import fetch_prices
from source_manager import SourceManager
from keyword_manager import KeywordManager

MAX_LOG_LINES = 2000
MAX_ALERTS = 500
HOST = "0.0.0.0"
PORT = 8000

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


class DashboardState:
    """Thread-safe holder for everything the web dashboard polls.

    gui.py keeps the equivalent of this in Tkinter queues drained on the
    main thread. A Flask app handles each request on its own thread, so
    this uses a plain lock instead.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.logs = collections.deque(maxlen=MAX_LOG_LINES)
        self.log_seq = 0
        self.alerts = collections.deque(maxlen=MAX_ALERTS)
        self.activity = "Idle"
        self.stats = {'scanned': 0, 'alerts': 0, 'skipped': 0}

    def add_log(self, message):
        with self._lock:
            self.log_seq += 1
            self.logs.append({
                'seq': self.log_seq,
                'text': message,
                'time': now_local().strftime('%H:%M:%S'),
            })

    def add_alert(self, alert):
        with self._lock:
            entry = dict(alert)
            ts = entry.get('time')
            entry['time'] = ts.isoformat() if isinstance(ts, datetime.datetime) else ts
            self.alerts.appendleft(entry)

    def set_status(self, activity, stats):
        with self._lock:
            self.activity = activity
            self.stats = dict(stats)

    def clear_alerts(self):
        with self._lock:
            self.alerts.clear()

    def snapshot(self, since_log_seq=0):
        with self._lock:
            return {
                'activity': self.activity,
                'stats': dict(self.stats),
                'logs': [l for l in self.logs if l['seq'] > since_log_seq],
                'alerts': list(self.alerts),
            }


state = DashboardState()
# The dashboard and the scan loop share one instance of each manager, so a
# holding or source added here is seen by the running scan immediately -
# separate instances used to write the file but leave the backend's
# in-memory copy stale until a restart.
portfolio_mgr = PortfolioManager()
source_mgr = SourceManager()
keyword_mgr = KeywordManager()
backend = StockAppBackend(
    log_callback=state.add_log,
    alert_callback=state.add_alert,
    status_callback=state.set_status,
    portfolio_mgr=portfolio_mgr,
    source_mgr=source_mgr,
)
# Show the persisted counters straight away rather than zeros until the
# first status callback.
state.set_status("Idle", backend.stats)

app = Flask(__name__)


def _digest_eq(a, b):
    # compare_digest refuses str operands containing non-ASCII characters
    # (TypeError), which would turn a password with an accent into a 500.
    return hmac.compare_digest((a or "").encode("utf-8"), (b or "").encode("utf-8"))


def _login_required():
    if not config.DASHBOARD_PASSWORD:
        return False
    auth = request.authorization
    if auth is None:
        return True
    user_ok = _digest_eq(auth.username, config.DASHBOARD_USERNAME)
    pass_ok = _digest_eq(auth.password, config.DASHBOARD_PASSWORD)
    return not (user_ok and pass_ok)


@app.before_request
def require_login():
    if _login_required():
        return (
            "Login required",
            401,
            {"WWW-Authenticate": 'Basic realm="Stocks AI"'},
        )


def _reload_analyzer_keywords():
    if backend and hasattr(backend.analyzer, "reload_keywords"):
        backend.analyzer.reload_keywords()


@app.route("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.route("/api/engine")
def api_engine():
    return jsonify({
        "engine": backend.engine_description(),
        # The Keywords tab only drives scoring when neither AI engine is on.
        "ai_active": bool(config.USE_CLOUD_AI or config.USE_LOCAL_LLM),
    })


# Settings the dashboard may read and write. Secrets are never echoed back:
# the API key and dashboard password come out as "is one set" flags only,
# and a blank value on save means "leave it as it is".
_SETTINGS_PUBLIC = (
    "NTFY_TOPIC", "NOTIFY_OWNERSHIP",
    "USE_CLOUD_AI", "CLOUD_AI_PROVIDER", "CLOUD_AI_MODEL", "CLOUD_AI_BASE_URL",
    "USE_LOCAL_LLM", "LOCAL_MODEL_NAME", "OLLAMA_NUM_THREADS", "OLLAMA_URL",
    "PAPER_COST_PCT", "DASHBOARD_USERNAME",
)
_SETTINGS_SECRET = ("CLOUD_AI_API_KEY", "DASHBOARD_PASSWORD")


def _settings_payload():
    out = {key: getattr(config, key) for key in _SETTINGS_PUBLIC}
    key = config.CLOUD_AI_API_KEY or ""
    out["cloud_api_key_set"] = bool(key)
    out["cloud_api_key_hint"] = ("…" + key[-4:]) if len(key) >= 8 else ""
    out["dashboard_password_set"] = bool(config.DASHBOARD_PASSWORD)
    out["providers"] = list(PROVIDERS)
    out["engine"] = backend.engine_description()
    out["ai_active"] = bool(config.USE_CLOUD_AI or config.USE_LOCAL_LLM)
    return out


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    """The Settings dialog, for a backend that only exists as a service on
    a VM. POST any subset of the public keys (plus CLOUD_AI_API_KEY /
    DASHBOARD_PASSWORD to replace them, or clear_api_key: true to drop the
    key). Persists to data/settings.json and applies live: the engine is
    rebuilt, the notifier re-reads its topic, no restart.
    """
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        updates = {k: body[k] for k in _SETTINGS_PUBLIC if k in body}
        for secret in _SETTINGS_SECRET:
            value = body.get(secret)
            if isinstance(value, str) and value.strip():
                updates[secret] = value.strip()
        if body.get("clear_api_key"):
            updates["CLOUD_AI_API_KEY"] = ""

        provider = updates.get("CLOUD_AI_PROVIDER")
        if provider is not None and provider not in PROVIDERS:
            return jsonify({"error": f"provider must be one of {list(PROVIDERS)}"}), 400
        try:
            if "OLLAMA_NUM_THREADS" in updates and int(updates["OLLAMA_NUM_THREADS"]) < 1:
                return jsonify({"error": "OLLAMA_NUM_THREADS must be at least 1"}), 400
            if "PAPER_COST_PCT" in updates and not (0 <= float(updates["PAPER_COST_PCT"]) < 1):
                return jsonify({"error": "PAPER_COST_PCT must be a fraction between 0 and 1"}), 400
            config.save_settings(updates)
        except (KeyError, TypeError, ValueError) as e:
            return jsonify({"error": f"invalid setting: {e}"}), 400
        except OSError as e:
            return jsonify({"error": f"could not write settings: {e}"}), 500
        backend.apply_settings()
    return jsonify(_settings_payload())


@app.route("/api/reload", methods=["POST"])
def api_reload():
    """Re-read every JSON file under data/ - for edits made by hand (over
    SSH, say) while the service is running. The desktop app's "Reload
    config" button, for the web."""
    config.reload_from_disk(verbose=False)
    portfolio_mgr.reload()
    source_mgr.reload()
    keyword_mgr.reload()
    backend.apply_settings()
    _reload_analyzer_keywords()
    return jsonify(_settings_payload())


@app.route("/api/alerts", methods=["DELETE"])
def api_alerts_clear():
    state.clear_alerts()
    return jsonify({"cleared": True})


@app.route("/api/state")
def api_state():
    since = request.args.get("since", 0, type=int)
    data = state.snapshot(since_log_seq=since)
    data['running'] = backend.running
    return jsonify(data)


@app.route("/api/sensitivity", methods=["GET", "POST"])
def api_sensitivity():
    """Weakest impact rating that raises an alert. POST {"level": "HIGH"}.

    Applies live - main.py and notifier.py both read config.MIN_IMPACT at
    the moment they filter, so a running watcher picks this up on its next
    article without a restart - and persists to data/settings.json.
    """
    if request.method == "POST":
        level = ((request.get_json(silent=True) or {}).get("level") or "").upper()
        if level not in config.IMPACT_LEVELS:
            return jsonify({"error": f"level must be one of {config.IMPACT_LEVELS}"}), 400
        config.save_setting("MIN_IMPACT", level)
    return jsonify({"level": config.MIN_IMPACT, "levels": config.IMPACT_LEVELS})


@app.route("/api/stop-loss", methods=["GET", "POST"])
def api_stop_loss():
    """Stop-loss / profit-protection level, as a percent (5 means 5%).
    POST {"pct": 5}. 0 disables it - watches close on schedule as before.

    Applies to watches opened from here on (captured per-watch at open
    time); persists to data/settings.json like the other live settings.
    """
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        try:
            pct = float(body.get("pct"))
        except (TypeError, ValueError):
            return jsonify({"error": "pct must be a number"}), 400
        if pct < 0:
            return jsonify({"error": "pct must be >= 0"}), 400
        config.save_setting("STOP_LOSS_PCT", pct / 100.0)
    return jsonify({"pct": config.STOP_LOSS_PCT * 100.0})


@app.route("/api/notifications", methods=["GET", "POST"])
def api_notifications():
    """Master mute for phone alerts. POST {"enabled": bool} to change it.

    Takes effect immediately on the running notifier and is persisted to
    data/settings.json, so it survives a restart. Muting stops only the push
    to ntfy.sh - scanning, analysis, watches and the paper record continue.
    """
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        if "enabled" not in body:
            return jsonify({"error": "missing 'enabled'"}), 400
        backend.notifier.set_enabled(bool(body["enabled"]))
    return jsonify({
        "enabled": backend.notifier.enabled,
        "has_topic": bool(backend.notifier.ntfy_topic),
    })


@app.route("/api/test-notification", methods=["POST"])
def api_test_notification():
    sent = backend.notifier.notify_system(
        "Test Notification",
        f"This is a test alert from your Stocks AI dashboard, sent at {now_local().strftime('%H:%M:%S')}.",
    )
    if not sent:
        # Name the actual cause - "it failed" sends people debugging their
        # phone when the real answer is a switch on this page.
        if not backend.notifier.enabled:
            error = "Phone alerts are muted. Turn the Alerts toggle back on."
        elif not backend.notifier.ntfy_topic:
            error = "No NTFY_TOPIC configured - set one in data/settings.json."
        else:
            error = "ntfy.sh rejected the request. Check the topic and connection."
        return jsonify({"sent": False, "error": error}), 400
    return jsonify({"sent": True})


@app.route("/api/control", methods=["POST"])
def api_control():
    action = (request.get_json(silent=True) or {}).get("action")
    if action == "start" and not backend.running:
        backend.start()
    elif action == "stop" and backend.running:
        backend.stop()
    elif action not in ("start", "stop"):
        return jsonify({"error": "invalid action"}), 400
    return jsonify({"running": backend.running})


@app.route("/api/portfolio", methods=["GET", "POST"])
def api_portfolio():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        if not portfolio_mgr.add_stock(
            body.get("ticker", ""), body.get("buy_price", 0.0),
            body.get("shares", 0.0), body.get("buy_date"),
        ):
            return jsonify({"error": "invalid ticker"}), 400
    return jsonify(portfolio_mgr.get_portfolio())


@app.route("/api/portfolio", methods=["DELETE"])
def api_portfolio_clear():
    portfolio_mgr.reset_portfolio()
    return jsonify(portfolio_mgr.get_portfolio())


@app.route("/api/portfolio/<ticker>", methods=["DELETE"])
def api_portfolio_delete(ticker):
    portfolio_mgr.remove_stock(ticker)
    return jsonify(portfolio_mgr.get_portfolio())


@app.route("/api/portfolio/summary")
def api_portfolio_summary():
    """Live price and profit per holding plus the totals - a cheap batched
    quote call, safe to poll often (unlike /api/portfolio/history, which
    fetches per-ticker daily history and is much heavier).

    Every holding is priced, including ones entered with just a buy price
    and no share count (the desktop app's way), so the table can show a
    per-share P/L for those; the value/cost totals only cover holdings with
    shares, since a value needs a quantity.
    """
    portfolio = portfolio_mgr.get_portfolio()
    prices = fetch_prices(list(portfolio)) if portfolio else {}

    holdings = []
    total_value = 0.0
    total_cost = 0.0
    for ticker, data in portfolio.items():
        shares = data.get("shares", 0.0) or 0.0
        buy_price = data.get("buy_price", 0.0) or 0.0
        price = prices.get(ticker)
        pl_pct = ((price - buy_price) / buy_price * 100) if (price and buy_price > 0) else None
        cost = buy_price * shares if shares > 0 else None
        value = (price * shares) if (price and shares > 0) else None
        holdings.append({
            "ticker": ticker, "shares": shares, "buy_price": buy_price,
            "buy_date": data.get("buy_date"), "price": price, "pl_pct": pl_pct,
            "gain_per_share": (price - buy_price) if (price and buy_price > 0) else None,
            "cost_basis": cost, "value": value,
        })
        if cost is not None:
            total_cost += cost
        if value is not None:
            total_value += value

    total_profit = total_value - total_cost
    profit_pct = (total_profit / total_cost * 100) if total_cost > 0 else 0.0
    return jsonify({
        "holdings": holdings,
        "total_value": round(total_value, 2),
        "total_cost_basis": round(total_cost, 2),
        "total_profit": round(total_profit, 2),
        "profit_pct": round(profit_pct, 2),
    })


@app.route("/api/portfolio/history")
def api_portfolio_history():
    return jsonify(compute_history(portfolio_mgr.get_portfolio()))


@app.route("/api/paper")
def api_paper():
    """The paper-trading record: closed trades, open positions marked to
    market, and the totals. Shown in the Portfolio view alongside real
    holdings, but kept entirely separate from them - these are simulated
    positions the app opened from its own alerts, not stock anyone owns.
    """
    if not backend.paper:
        return jsonify({"enabled": False})
    paper = backend.paper
    tickers = paper.tickers_open()
    prices = fetch_prices(tickers) if tickers else {}
    return jsonify(paper.overview(
        prices,
        start_capital=config.PAPER_START_CAPITAL,
        position_pct=config.PAPER_POSITION_PCT,
    ))


@app.route("/api/watches")
def api_watches():
    return jsonify(backend.watch_mgr.get_all())


@app.route("/api/watches/<watch_id>", methods=["DELETE"])
def api_watches_delete(watch_id):
    if backend.watch_mgr.remove_watch(watch_id) and backend.paper:
        # No exit signal will ever fire for this one, so its open paper
        # position would otherwise sit in the ledger forever.
        backend.paper.discard_trade(watch_id)
    return jsonify(backend.watch_mgr.get_all())


@app.route("/api/sources", methods=["GET", "POST"])
def api_sources():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        try:
            source_mgr.add_source(body.get("name", ""), body.get("url", ""), body.get("type", "webpage"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    return jsonify(source_mgr.get_sources())


@app.route("/api/sources/reset", methods=["POST"])
def api_sources_reset():
    source_mgr.reset_to_defaults()
    return jsonify(source_mgr.get_sources())


@app.route("/api/sources/<source_id>/toggle", methods=["POST"])
def api_sources_toggle(source_id):
    return jsonify({"enabled": source_mgr.toggle_source(source_id)})


@app.route("/api/sources/<source_id>", methods=["DELETE"])
def api_sources_delete(source_id):
    source_mgr.remove_source(source_id)
    return jsonify(source_mgr.get_sources())


@app.route("/api/keywords", methods=["GET", "POST"])
def api_keywords():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        keyword = body.get("keyword", "")
        try:
            weight = int(body.get("weight", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "weight must be a number"}), 400
        if not keyword_mgr.add_keyword(keyword, weight, body.get("type", "positive")):
            return jsonify({"error": "invalid keyword"}), 400
        _reload_analyzer_keywords()
    return jsonify(keyword_mgr.get_all_keywords())


@app.route("/api/keywords/reset", methods=["POST"])
def api_keywords_reset():
    keyword_mgr.reset_to_defaults()
    _reload_analyzer_keywords()
    return jsonify(keyword_mgr.get_all_keywords())


@app.route("/api/keywords/<path:keyword>", methods=["DELETE"])
def api_keywords_delete(keyword):
    kind = request.args.get("type")
    keyword_mgr.remove_keyword(keyword, kind if kind in ("positive", "negative") else None)
    _reload_analyzer_keywords()
    return jsonify(keyword_mgr.get_all_keywords())


if __name__ == "__main__":
    if not config.DASHBOARD_PASSWORD:
        print("WARNING: DASHBOARD_USERNAME/DASHBOARD_PASSWORD are not set "
              "(data/settings.json) - the dashboard has no login. Set them "
              "before running this anywhere but localhost.")
    backend.start()
    try:
        from waitress import serve
        print(f"Serving dashboard on http://{HOST}:{PORT} (waitress)")
        serve(app, host=HOST, port=PORT)
    except ImportError:
        print(f"Serving dashboard on http://{HOST}:{PORT} (Flask dev server - "
              f"'pip install waitress' for a sturdier one on a 24/7 server)")
        app.run(host=HOST, port=PORT, threaded=True)
