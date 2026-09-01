import json
import os
import uuid
import datetime

from local_time import now_local

# How big a move counts as the alerted-on prediction having "played out".
# Only CRITICAL/HIGH impact ever reaches the notify filter in main.py, so
# those are the only buckets that need a target.
TARGET_PCT = {
    "CRITICAL": 0.10,
    "HIGH": 0.05,
}
DEFAULT_TARGET_PCT = 0.05

# How long the alerted-on news is expected to keep moving the price, per the
# LLM/keyword-analyzer's "horizon" field. A watch that never hits its target
# within this window is closed as a time-stop instead.
HORIZON_DAYS = {
    "INTRADAY": 1,
    "DAYS": 5,
    "WEEKS": 21,
}
DEFAULT_HORIZON = "DAYS"

MAX_STORED_WATCHES = 200

# A watch is either a long (bought the stock / a long CFD, exit by selling)
# or a short (sold a CFD short, exit by buying it back). The direction only
# changes which way the target price sits from the entry, and therefore
# which comparison counts as the alerted-on move having played out.
LONG = "LONG"
SHORT = "SHORT"
DIRECTIONS = (LONG, SHORT)


class WatchManager:
    """Tracks stocks that had a HIGH/CRITICAL alert fire, so the app can
    later tell the user when to close the position: either the predicted
    move happened (target hit) or the expected window passed without it
    (horizon expired).

    POSITIVE alerts open a LONG watch (buy now, sell on the signal);
    NEGATIVE alerts open a SHORT watch (sell a CFD short now, buy it back
    on the signal).

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
            # Watches written before shorting existed have no direction;
            # they were all longs.
            for w in data:
                if isinstance(w, dict):
                    w.setdefault('direction', LONG)
            return data
        except json.JSONDecodeError:
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

    def add_watch(self, ticker, company, entry_price, impact, horizon, prediction,
                  article_url=None, article_headline=None, direction=LONG):
        """Open a new watch for `ticker`. Returns the created record, or None
        if there's no usable entry price or an open watch already exists for
        this ticker (avoids stacking duplicate exit notifications, and stops
        a later opposite-sentiment article opening a contradictory position).
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
        if horizon not in HORIZON_DAYS:
            horizon = DEFAULT_HORIZON

        target_pct = TARGET_PCT.get(impact, DEFAULT_TARGET_PCT)
        # A short profits on the way down, so its target sits below entry.
        sign = -1 if direction == SHORT else 1
        target_price = round(entry_price * (1 + sign * target_pct), 4)

        opened_at = now_local()
        expires_at = opened_at + datetime.timedelta(days=HORIZON_DAYS[horizon])

        watch = {
            "id": uuid.uuid4().hex[:12],
            "ticker": ticker,
            "company": company or ticker,
            "direction": direction,
            "entry_price": entry_price,
            "target_price": target_price,
            "target_pct": target_pct,
            "impact": impact,
            "horizon": horizon,
            "prediction": prediction,
            "article_url": article_url,
            "article_headline": article_headline,
            "opened_at": opened_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "status": "OPEN",
            "reason": None,
            "exit_price": None,
            "closed_at": None,
        }
        self.watches.append(watch)
        self._trim()
        self.save()
        return watch

    def get_open_watches(self):
        return [w for w in self.watches if w['status'] == 'OPEN']

    @staticmethod
    def target_reached(watch, price):
        """Has the alerted-on move played out at `price`? A long needs the
        price at or above its target, a short at or below."""
        if watch.get('direction') == SHORT:
            return price <= watch['target_price']
        return price >= watch['target_price']

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
