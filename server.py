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

from flask import Flask, jsonify, request, send_from_directory

import config
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

    def snapshot(self, since_log_seq=0):
        with self._lock:
            return {
                'activity': self.activity,
                'stats': dict(self.stats),
                'logs': [l for l in self.logs if l['seq'] > since_log_seq],
                'alerts': list(self.alerts),
            }


state = DashboardState()
backend = StockAppBackend(
    log_callback=state.add_log,
    alert_callback=state.add_alert,
    status_callback=state.set_status,
)
portfolio_mgr = PortfolioManager()
source_mgr = SourceManager()
keyword_mgr = KeywordManager()

app = Flask(__name__)


def _login_required():
    if not config.DASHBOARD_PASSWORD:
        return False
    auth = request.authorization
    if auth is None:
        return True
    user_ok = hmac.compare_digest(auth.username or "", config.DASHBOARD_USERNAME)
    pass_ok = hmac.compare_digest(auth.password or "", config.DASHBOARD_PASSWORD)
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
    import config
    if config.USE_CLOUD_AI:
        text = f"Cloud AI · {config.CLOUD_AI_PROVIDER}/{config.CLOUD_AI_MODEL}"
    elif config.USE_LOCAL_LLM:
        text = f"Local AI · {config.LOCAL_MODEL_NAME}"
    else:
        text = "Keyword scoring · offline"
    return jsonify({"engine": text})


@app.route("/api/state")
def api_state():
    since = request.args.get("since", 0, type=int)
    data = state.snapshot(since_log_seq=since)
    data['running'] = backend.running
    return jsonify(data)


@app.route("/api/test-notification", methods=["POST"])
def api_test_notification():
    sent = backend.notifier.notify_system(
        "Test Notification",
        f"This is a test alert from your Stocks AI dashboard, sent at {now_local().strftime('%H:%M:%S')}.",
    )
    if not sent:
        return jsonify({"sent": False, "error": "No NTFY_TOPIC configured, or ntfy.sh rejected the request."}), 400
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


@app.route("/api/portfolio/<ticker>", methods=["DELETE"])
def api_portfolio_delete(ticker):
    portfolio_mgr.remove_stock(ticker)
    return jsonify(portfolio_mgr.get_portfolio())


@app.route("/api/portfolio/summary")
def api_portfolio_summary():
    """Live current value/profit - a cheap batched quote call, safe to poll
    often (unlike /api/portfolio/history, which fetches per-ticker daily
    history and is much heavier)."""
    portfolio = portfolio_mgr.get_portfolio()
    tickers = [t for t, d in portfolio.items() if d.get("shares", 0) > 0]
    prices = fetch_prices(tickers) if tickers else {}

    holdings = []
    total_value = 0.0
    total_cost = 0.0
    for ticker, data in portfolio.items():
        shares = data.get("shares", 0.0)
        if shares <= 0:
            continue
        price = prices.get(ticker)
        cost = data.get("buy_price", 0.0) * shares
        value = (price * shares) if price else None
        holdings.append({
            "ticker": ticker, "shares": shares, "price": price,
            "cost_basis": cost, "value": value,
        })
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


@app.route("/api/watches")
def api_watches():
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


@app.route("/api/keywords/<path:keyword>", methods=["DELETE"])
def api_keywords_delete(keyword):
    keyword_mgr.remove_keyword(keyword, request.args.get("type"))
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
