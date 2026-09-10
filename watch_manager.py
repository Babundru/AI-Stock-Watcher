import json
import os
import uuid
import datetime

import strategy
from local_time import now_local

MAX_STORED_WATCHES = 200

# Hard ceiling on how long a watch may stay OPEN, whatever the price is
# doing. Every horizon's time exit falls well inside it (WEEKS is 15 trading
# days), so this only catches a watch whose time exit could not fire - one
# that never got a price, say.
#
# Without it the open set could only grow: _trim() never drops an OPEN
# watch, and every open watch is priced on every check, so an unbounded set
# makes the price call cost grow with uptime - which on a small VM
# eventually stops the whole machine rather than just this app. A watch
# closed by the limit is recorded with its real exit price, so the paper
# ledger still gets a truthful result for it (reason 'max_age').
MAX_OPEN_DAYS = 30

# A watch is either a long (bought the stock / a long CFD, exit by selling)
# or a short (sold a CFD short, exit by buying it back). The direction only
# changes which way the target and stop sit from the entry.
LONG = strategy.LONG
SHORT = strategy.SHORT
DIRECTIONS = (LONG, SHORT)


class WatchManager:
    """The positions opened from alerts, and the state their exits need.

    POSITIVE alerts open a LONG watch (buy now, sell on the signal);
    NEGATIVE alerts open a SHORT watch (sell a CFD short now, buy it back
    on the signal). The exit rules themselves live in strategy.py; this
    class stores each position's stop, peak and time exit between checks.

    Mirrors PortfolioManager's plain-JSON-file pattern (data/watches.json).
    """

    def __init__(self, filename="data/watches.json"):
        self.filename = filename
        self.watches = self._load()

    def _load(self):
        if not os.path.exists(self.filename):
            return []
        try:
            with open(self.filename, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, list):
                return []
            data = [w for w in data if isinstance(w, dict)]
            for w in data:
                _upgrade(w)
            return data
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: could not read {self.filename} ({e}); starting with no watches")
            return []

    def save(self):
        try:
            with open(self.filename, 'w', encoding='utf-8') as f:
                json.dump(self.watches, f, indent=2)
        except Exception as e:
            print(f"Error saving watches: {e}")

    def has_open_watch(self, ticker):
        ticker = (ticker or '').upper().strip()
        return any(w['ticker'] == ticker and w['status'] == 'OPEN' for w in self.watches)

    def open_count(self):
        return sum(1 for w in self.watches if w['status'] == 'OPEN')

    def add_watch(self, ticker, company, entry_price, impact, horizon, prediction,
                  article_url=None, article_headline=None, direction=LONG,
                  target_pct=None, stop_pct=None, extra=None):
        """Open a new watch for `ticker`. Returns the created record, or None
        if there's no usable entry price or an open watch already exists for
        this ticker (avoids stacking duplicate exit notifications).

        `target_pct`/`stop_pct` come from strategy.plan_trade; without them
        the impact-bucket target and the maximum stop are used. `extra` is
        recorded as-is (the confidence and price context the trade was
        opened on), so the ledger can later tell which of them mattered.
        """
        if not ticker or not entry_price:
            return None

        ticker = ticker.upper().strip()
        if self.has_open_watch(ticker):
            return None

        direction = (direction or LONG).upper()
        if direction not in DIRECTIONS:
            direction = LONG

        impact = (impact or '').upper()
        horizon = (horizon or '').upper()
        if horizon not in strategy.HORIZON_TRADING_DAYS:
            horizon = strategy.DEFAULT_HORIZON

        target_pct = target_pct or strategy.TARGET_PCT.get(impact, strategy.DEFAULT_TARGET_PCT)
        stop_pct = stop_pct or strategy.max_stop_pct()

        opened_at = now_local()
        watch = {
            "id": uuid.uuid4().hex[:12],
            "ticker": ticker,
            "company": company or ticker,
            "direction": direction,
            "strategy": strategy.STRATEGY_VERSION,
            "entry_price": entry_price,
            "target_price": round(strategy.price_at_gain(direction, entry_price, target_pct), 4),
            "target_pct": round(target_pct, 6),
            # Distance of the initial stop, and the stop's current level as
            # a gain on the position (negative = below break-even). The
            # level only ever ratchets up - see strategy.update_exit.
            "stop_pct": round(stop_pct, 6),
            "stop_gain": round(-stop_pct, 6),
            "stop_loss_price": round(strategy.price_at_gain(direction, entry_price, -stop_pct), 4),
            "initial_stop_price": round(strategy.price_at_gain(direction, entry_price, -stop_pct), 4),
            "peak_gain": 0.0,
            # Captured at open, like the stop, so changing the setting
            # later doesn't rewrite how an existing position is managed.
            "let_run": bool(strategy.config.LET_WINNERS_RUN),
            "trailing": False,
            "impact": impact,
            "horizon": horizon,
            "prediction": prediction,
            "article_url": article_url,
            "article_headline": article_headline,
            "opened_at": opened_at.isoformat(),
            "expires_at": strategy.time_exit_at(opened_at, horizon).isoformat(),
            "status": "OPEN",
            "reason": None,
            "exit_price": None,
            "closed_at": None,
        }
        for key, value in (extra or {}).items():
            watch.setdefault(key, value)
        self.watches.append(watch)
        self._trim()
        self.save()
        return watch

    def get_open_watches(self):
        return [w for w in self.watches if w['status'] == 'OPEN']

    @staticmethod
    def update_exit(watch, price, cost_pct=0.0):
        """Ratchet the stop from `price` and return (reason, target_just_reached);
        see strategy.update_exit. Changes the record in memory only - call
        save() once after a pass over every open watch."""
        return strategy.update_exit(watch, price, cost_pct)

    @staticmethod
    def over_age_limit(watch, now=None):
        """Whether this watch has been open past MAX_OPEN_DAYS."""
        opened = watch.get('opened_at')
        if not opened:
            return False
        try:
            opened_at = datetime.datetime.fromisoformat(opened)
        except (TypeError, ValueError):
            return False
        now = now or now_local()
        # now_local() is tz-aware, so a normally-written record compares
        # fine. A naive opened_at (a hand-edited file, or one written before
        # local_time existed) would raise TypeError here - and this runs
        # inside the watch check, where an exception costs the whole pass.
        # Assume such a stamp is already in local time.
        if (opened_at.tzinfo is None) != (now.tzinfo is None):
            if opened_at.tzinfo is None:
                opened_at = opened_at.replace(tzinfo=now.tzinfo)
            else:
                now = now.replace(tzinfo=opened_at.tzinfo)
        return (now - opened_at) >= datetime.timedelta(days=MAX_OPEN_DAYS)

    def remove_watch(self, watch_id):
        """Delete a watch outright, regardless of status. Used when the user
        dismisses a pending sell signal from the dashboard."""
        before = len(self.watches)
        self.watches = [w for w in self.watches if w['id'] != watch_id]
        removed = len(self.watches) != before
        if removed:
            self.save()
        return removed

    def close_watch(self, watch_id, reason, exit_price):
        for w in self.watches:
            if w['id'] == watch_id and w['status'] == 'OPEN':
                w['status'] = 'CLOSED'
                w['reason'] = reason
                w['exit_price'] = exit_price
                w['closed_at'] = now_local().isoformat()
                self.save()
                return w
        return None

    def get_all(self, limit=100):
        """Open watches first (newest first), then most-recently-closed ones,
        capped to `limit` total - for the dashboard's "Watching" panel."""
        open_watches = [w for w in self.watches if w['status'] == 'OPEN']
        closed_watches = [w for w in self.watches if w['status'] == 'CLOSED']
        open_watches.reverse()
        closed_watches.reverse()
        return (open_watches + closed_watches)[:limit]

    def _trim(self):
        """Keep the file bounded: never drop an OPEN watch, only trim the
        oldest CLOSED ones once total storage exceeds the cap."""
        if len(self.watches) <= MAX_STORED_WATCHES:
            return
        open_watches = [w for w in self.watches if w['status'] == 'OPEN']
        closed_watches = [w for w in self.watches if w['status'] == 'CLOSED']
        keep_closed = max(0, MAX_STORED_WATCHES - len(open_watches))
        self.watches = open_watches + closed_watches[-keep_closed:]


def _upgrade(w):
    """Bring a record written by an older version up to the current shape.

    Watches from before shorting have no direction (they were all longs).
    Watches from before this strategy version have no ratcheting stop: an
    open one gets the stop it was opened with, or the maximum stop if it had
    none - so a loser the old rules were carrying with no floor is closed
    at the next check once it is past that - and keeps closing at its
    target, as it was opened to.
    """
    w.setdefault('direction', LONG)
    w.setdefault('strategy', strategy.LEGACY_STRATEGY)
    if w.get('status') != 'OPEN' or 'stop_gain' in w:
        return
    entry = w.get('entry_price')
    if not entry:
        return
    stop_pct = w.get('stop_loss_pct') or strategy.max_stop_pct()
    w['stop_pct'] = round(stop_pct, 6)
    w['stop_gain'] = round(-stop_pct, 6)
    w['stop_loss_price'] = round(strategy.price_at_gain(w['direction'], entry, -stop_pct), 4)
    w.setdefault('initial_stop_price', w['stop_loss_price'])
    w.setdefault('peak_gain', 0.0)
    w.setdefault('let_run', False)
    w.setdefault('trailing', False)
