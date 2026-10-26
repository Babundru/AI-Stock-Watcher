"""Mirror of the paper trades on an Interactive Brokers paper (demo) account.

The paper ledger prices every entry and exit from a quote (fills.py): the ask
to buy, the bid to sell, with the spread estimated when Yahoo has none. That
is a model of a fill, and the whole record rests on it. This module checks it
against the real thing - every position the app opens is also opened, as a
market order, on an IBKR paper account, and closed there when the app closes
it - and writes down both prices side by side (data/broker_orders.json).

The broker follows; it never leads. Nothing here feeds back into a decision:
the paper ledger still sizes, opens and closes every position, and a Gateway
that is down, an order IBKR refuses or a stock it can't find only shows up
as a line in this file and the log. That also means the two can drift - a
position the paper account closed while the Gateway was offline is closed on
the broker once it is back - which is the point: the drift is the measurement.

IBKR has no API keys for individual accounts. Orders go through IB Gateway
(or TWS), a Java program that holds the login; the app connects to it over a
local socket (IBKR_HOST/IBKR_PORT). Only paper accounts are traded: a login
whose account isn't a paper one ("DU...") is refused, so a Gateway logged in
to a live account by mistake can't be sent an order. See DEPLOY.md.

All IBKR calls happen on one worker thread that owns the connection: the
library (ib_async) runs on an asyncio loop, and its connection can only be
used from the thread that loop belongs to. The scan loop, a watch check or
the dashboard's Sell button only queue a job, so a slow or absent Gateway
never holds any of them up.
"""

import asyncio
import json
import math
import os
import queue
import threading
import time

import config
import markets
from local_time import now_local
from strategy import LONG, SHORT

LEDGER_FILE = "data/broker_orders.json"

# An entry that couldn't be sent within this long - the Gateway down, say -
# is dropped rather than sent late: the paper trade opened at the price of
# that moment, and an order placed an hour on would be compared against a
# price it never had a chance at. Exits are never dropped; a position open on
# the broker stays open until it is closed.
OPEN_MAX_DELAY_S = 120

# How often a lost or refused connection is retried.
RECONNECT_EVERY_S = 60

# How long a market order is waited on before the worker moves on. Anything
# still working by then is followed through the order-status events.
FILL_WAIT_S = 20

# Order states after which an order will not fill any further.
DONE_STATES = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}

# Yahoo suffix -> (IBKR primary exchange, currency). Orders are SMART-routed;
# the primary exchange only tells IBKR which listing is meant. A suffix not
# here (the German regional venues apart from Frankfurt, Prague, Budapest,
# Athens, anything outside Europe) isn't sent, and the log says so.
IB_LISTINGS = {
    "L": ("LSE", "GBP"),
    "DE": ("IBIS", "EUR"),
    "F": ("FWB", "EUR"),
    "PA": ("SBF", "EUR"),
    "AS": ("AEB", "EUR"),
    "BR": ("ENEXT.BE", "EUR"),
    "LS": ("BVL", "EUR"),
    "IR": ("ISED", "EUR"),
    "SW": ("EBS", "CHF"), "VX": ("EBS", "CHF"),
    "MI": ("BVME", "EUR"),
    "MC": ("BM", "EUR"),
    "VI": ("VSE", "EUR"),
    "ST": ("SFB", "SEK"),
    "CO": ("CPH", "DKK"),
    "OL": ("OSE", "NOK"),
    "HE": ("HEX", "EUR"),
    "WA": ("WSE", "PLN"),
}


def ib_listing(ticker):
    """(symbol, primary exchange, currency) IBKR knows `ticker` by, or None
    when it is on an exchange this module doesn't map.

    Yahoo writes a share class with a dash ("BRK-B"), IBKR with a space in
    the US ("BRK B") and a dot in London ("BT.A"). A US class share the app
    spelled "BRK.B" (markets._is_class_share) is the same thing."""
    tail = markets.suffix(ticker)
    if not tail:
        if markets.market_key(ticker) != markets.US:
            return None
        return str(ticker).upper().replace("-", " ").replace(".", " "), "", "USD"
    if tail not in IB_LISTINGS:
        return None
    symbol = str(ticker).rsplit(".", 1)[0].upper()
    exchange, currency = IB_LISTINGS[tail]
    if tail == "L":
        symbol = symbol.replace("-", ".")
    return symbol, exchange, currency


def order_quantity(watch):
    """Whole shares for the paper position's size (position_usd), or 0.

    The paper account counts every currency as dollars (markets.py: only
    percentages matter to it), and so does this, apart from London, where
    Yahoo quotes in pence. On a demo account a size that is out by the
    exchange rate measures the fill just as well."""
    size = watch.get("position_usd")
    price = watch.get("entry_price")
    if not size or not price:
        return 0
    if markets.suffix(watch.get("ticker")) == "L":
        price = price / 100.0
    return max(0, math.floor(size / price))


def is_paper_account(account):
    """IBKR numbers paper accounts "DU..." (and "DF..." for advisors); a live
    one starts "U", "F" or "I"."""
    return str(account or "").upper().startswith("D")


def _ref(watch_id, leg):
    return f"asw:{watch_id}:{leg}"


def _parse_ref(ref):
    """(watch_id, leg) from an order reference this module set, else None."""
    parts = str(ref or "").split(":")
    if len(parts) == 3 and parts[0] == "asw" and parts[2] in ("open", "close"):
        return parts[1], parts[2]
    return None


def _default_ib():
    from ib_async import IB
    return IB()


def _stock(symbol, exchange, currency):
    from ib_async import Stock
    return Stock(symbol, "SMART", currency, primaryExchange=exchange)


def _market_order(action, qty, account, ref):
    from ib_async import MarketOrder
    order = MarketOrder(action, qty)
    order.account = account
    order.orderRef = ref
    order.tif = "DAY"
    return order


class Broker:
    """Queue of open/close jobs for one IBKR paper account, and the worker
    that carries them out. start() it, then call open_position/close_position
    from anywhere."""

    def __init__(self, filename=LEDGER_FILE, log=print, ib_factory=_default_ib,
                 contract_factory=_stock, order_factory=_market_order):
        self.filename = filename
        self.log = log
        self._ib_factory = ib_factory
        self._contract_factory = contract_factory
        self._order_factory = order_factory
        self._jobs = queue.Queue()
        # Jobs taken off the queue but not carried out yet, oldest first -
        # waiting for the Gateway.
        self._pending = []
        self._lock = threading.Lock()
        self.records = self._load()
        self.ib = None
        self.account = None
        self.last_error = None
        self._last_attempt = 0
        self._running = False
        self._thread = None
        # A close asked for before a restart but never sent is sent now.
        for watch_id, rec in self.records.items():
            if (rec.get("close") or {}).get("status") == "pending":
                self._pending.append({"kind": "close", "watch_id": watch_id,
                                      "queued_at": time.time()})

    # --- ledger -------------------------------------------------------------

    def _load(self):
        if not os.path.exists(self.filename):
            return {}
        try:
            with open(self.filename, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (ValueError, OSError) as e:
            print(f"Error loading {self.filename}: {e}")
            return {}

    def save(self):
        with self._lock:
            os.makedirs(os.path.dirname(self.filename) or ".", exist_ok=True)
            tmp = self.filename + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.records, f, indent=2)
            os.replace(tmp, self.filename)

    def status(self):
        """Connection state and counts, for a log line or a UI."""
        legs = [leg for rec in self.records.values()
                for leg in (rec.get("open"), rec.get("close")) if leg]
        return {
            "connected": bool(self.ib and self.ib.isConnected()),
            "account": self.account,
            "last_error": self.last_error,
            "waiting": len(self._pending) + self._jobs.qsize(),
            "orders": len(legs),
            "filled": sum(1 for leg in legs if leg.get("status") == "Filled"),
        }

    # --- called from the app's threads --------------------------------------

    def open_position(self, watch):
        """Queue the broker side of a position the paper account just opened."""
        self._jobs.put({"kind": "open", "watch": dict(watch), "queued_at": time.time()})

    def close_position(self, watch, reason, paper_price):
        """Queue the broker side of a position the paper account just closed
        at `paper_price`."""
        self._jobs.put({"kind": "close", "watch_id": watch["id"], "reason": reason,
                        "paper_price": paper_price, "queued_at": time.time()})

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="ibkr", daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    # --- worker --------------------------------------------------------------

    def _run(self):
        # ib_async needs an event loop on the thread that uses it.
        asyncio.set_event_loop(asyncio.new_event_loop())
        while self._running:
            try:
                self.step()
            except Exception as e:  # never let one bad job end the worker
                self.last_error = str(e)
                try:
                    self.log(f"  ⚠ IBKR: {e}")
                except Exception:
                    pass
            if self.ib and self.ib.isConnected():
                self.ib.sleep(1)  # also lets order-status updates arrive
            else:
                time.sleep(1)
        if self.ib and self.ib.isConnected():
            self.ib.disconnect()

    def step(self):
        """One turn of the worker: (re)connect if due, then carry out every
        job the connection allows. Separate from _run so a test can drive it."""
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                break
            if job["kind"] == "close":
                self._mark_close_pending(job)
            self._pending.append(job)
        if not self._pending:
            return
        if not self._ensure_connected():
            self._drop_stale_opens()
            return
        while self._pending:
            job = self._pending.pop(0)
            if job["kind"] == "open":
                self._open(job)
            else:
                self._close(job)

    def _ensure_connected(self):
        if self.ib and self.ib.isConnected():
            return True
        if time.time() - self._last_attempt < RECONNECT_EVERY_S:
            return False
        self._last_attempt = time.time()
        try:
            ib = self._ib_factory()
            ib.connect(config.IBKR_HOST, config.IBKR_PORT,
                       clientId=config.IBKR_CLIENT_ID, timeout=10)
        except ImportError:
            self.last_error = "ib_async is not installed (pip install ib_async)"
            self.log(f"  ⚠ IBKR: {self.last_error}")
            return False
        except Exception as e:
            self.last_error = (f"can't reach IB Gateway at {config.IBKR_HOST}:{config.IBKR_PORT} "
                               f"({e or type(e).__name__})")
            self.log(f"  ⚠ IBKR: {self.last_error} - retrying in {RECONNECT_EVERY_S}s")
            return False

        accounts = list(ib.managedAccounts() or [])
        wanted = config.IBKR_ACCOUNT.strip()
        account = wanted or (accounts[0] if len(accounts) == 1 else None)
        problem = None
        if wanted and wanted not in accounts:
            problem = f"account {wanted} isn't one this login sees ({', '.join(accounts)})"
        elif not account:
            problem = f"this login sees several accounts ({', '.join(accounts)}) - set IBKR_ACCOUNT"
        elif not is_paper_account(account):
            problem = (f"{account} is a live account - only paper accounts (DU...) are traded. "
                       f"Log the Gateway in with your paper username")
        if problem:
            ib.disconnect()
            self.last_error = problem
            self.log(f"  ⛔ IBKR: {problem}")
            return False

        self.ib = ib
        self.account = account
        self.last_error = None
        ib.orderStatusEvent += self._on_status
        # Orders that moved while the app wasn't listening: the Gateway hands
        # back this session's orders on connect.
        for trade in ib.trades():
            self._on_status(trade)
        self.log(f"  🏦 IBKR connected - paper account {account}")
        return True

    def _drop_stale_opens(self):
        now = time.time()
        keep = []
        for job in self._pending:
            if job["kind"] == "open" and now - job["queued_at"] > OPEN_MAX_DELAY_S:
                watch = job["watch"]
                self._record(watch)["open"] = {"status": "Skipped",
                                               "error": f"Gateway offline: {self.last_error}",
                                               "time": now_local().isoformat()}
                self.save()
                self.log(f"  🏦 IBKR: {watch['ticker']} not opened on the broker - "
                         f"the Gateway was offline for over {OPEN_MAX_DELAY_S}s")
            else:
                keep.append(job)
        self._pending = keep

    def _record(self, watch):
        return self.records.setdefault(watch["id"], {
            "watch_id": watch["id"],
            "ticker": watch["ticker"],
            "direction": watch.get("direction", LONG),
        })

    def _mark_close_pending(self, job):
        rec = self.records.get(job["watch_id"])
        if rec is None or rec.get("close"):
            return
        rec["close"] = {"status": "pending", "reason": job.get("reason"),
                        "paper_price": job.get("paper_price"),
                        "time": now_local().isoformat()}
        self.save()

    def _open(self, job):
        watch = job["watch"]
        rec = self._record(watch)
        leg = rec["open"] = {"paper_price": watch.get("entry_price"),
                             "time": now_local().isoformat()}
        if time.time() - job["queued_at"] > OPEN_MAX_DELAY_S:
            leg.update(status="Skipped", error="queued too long before it could be sent")
            self.save()
            return
        listing = ib_listing(watch["ticker"])
        qty = order_quantity(watch)
        if not listing:
            leg.update(status="Skipped", error="exchange not mapped to IBKR")
        elif qty < 1:
            leg.update(status="Skipped", error="position smaller than one share")
        if leg.get("status"):
            self.save()
            self.log(f"  🏦 IBKR: {watch['ticker']} not opened on the broker - {leg['error']}")
            return
        action = "SELL" if watch.get("direction") == SHORT else "BUY"
        self._place(rec, "open", listing, action, qty)

    def _close(self, job):
        rec = self.records.get(job["watch_id"])
        if rec is None or (rec.get("close") or {}).get("status") not in (None, "pending"):
            return
        leg = rec.setdefault("close", {"reason": job.get("reason"),
                                       "paper_price": job.get("paper_price"),
                                       "time": now_local().isoformat()})
        opening = rec.get("open") or {}
        # An entry still working (sent outside its market's hours, say) is
        # cancelled rather than closed against; whatever of it filled first
        # is what there is to close.
        if opening.get("status") not in DONE_STATES and opening.get("status") != "Skipped":
            trade = self._find_trade(_ref(rec["watch_id"], "open"))
            if trade is not None and not trade.isDone():
                self.ib.cancelOrder(trade.order)
                self.ib.sleep(2)
                self._on_status(trade)
                opening = rec.get("open") or {}
        held = int(opening.get("filled") or 0)
        if held < 1:
            leg.update(status="Skipped", error="nothing was filled on the broker to close")
            self.save()
            return
        action = "BUY" if rec.get("direction") == SHORT else "SELL"
        self._place(rec, "close", ib_listing(rec["ticker"]), action, held)

    def _place(self, rec, leg_name, listing, action, qty):
        leg = rec[leg_name]
        leg.update(action=action, qty=qty, order_ref=_ref(rec["watch_id"], leg_name))
        symbol, exchange, currency = listing
        try:
            contract = self._contract_factory(symbol, exchange, currency)
            found = self.ib.qualifyContracts(contract)
            if not found and exchange == "LSE" and not symbol.endswith("."):
                # IBKR writes some short London symbols with a trailing dot
                # ("RR." for Rolls-Royce).
                contract = self._contract_factory(symbol + ".", exchange, currency)
                found = self.ib.qualifyContracts(contract)
            if not found:
                leg.update(status="Skipped",
                           error=f"IBKR doesn't know {symbol} on {exchange or 'US'}")
                self.save()
                self.log(f"  🏦 IBKR: {rec['ticker']} - {leg['error']}")
                return
            order = self._order_factory(action, qty, self.account, leg["order_ref"])
            trade = self.ib.placeOrder(found[0], order)
            leg["status"] = "Submitted"
            self.save()
            deadline = time.time() + FILL_WAIT_S
            while not trade.isDone() and time.time() < deadline:
                self.ib.sleep(0.5)
            self._on_status(trade)
        except Exception as e:
            leg.update(status="Error", error=str(e) or type(e).__name__)
            self.save()
            self.log(f"  ⚠ IBKR {action} {qty} {rec['ticker']} failed: {leg['error']}")
            return
        leg = rec[leg_name]
        if leg.get("status") == "Filled":
            self.log(f"  🏦 IBKR {action} {qty} {rec['ticker']} filled at "
                     f"{leg['fill_price']:.4g} (paper: {leg.get('paper_price') or 0:.4g})")
        else:
            self.log(f"  🏦 IBKR {action} {qty} {rec['ticker']}: {leg.get('status')}"
                     + (f" - {leg['error']}" if leg.get("error") else ""))

    def _find_trade(self, ref):
        for trade in self.ib.trades():
            if trade.order.orderRef == ref:
                return trade
        return None

    def _on_status(self, trade):
        """Copy an order's state into the ledger leg it belongs to."""
        parsed = _parse_ref(getattr(trade.order, "orderRef", ""))
        if not parsed:
            return
        watch_id, leg_name = parsed
        leg = (self.records.get(watch_id) or {}).get(leg_name)
        if leg is None:
            return
        status = trade.orderStatus
        changed = {
            "status": status.status or leg.get("status"),
            "filled": status.filled,
            "fill_price": status.avgFillPrice or None,
        }
        errors = [entry.message for entry in getattr(trade, "log", [])
                  if getattr(entry, "errorCode", 0)]
        if errors:
            changed["error"] = errors[-1]
        if all(leg.get(k) == v for k, v in changed.items()):
            return
        leg.update(changed)
        # How much worse the broker's price was than the paper one, as a
        # fraction of it: paying more to buy, or getting less to sell.
        # Negative when the broker did better.
        paper = leg.get("paper_price")
        if leg.get("fill_price") and paper:
            worse = leg["fill_price"] - paper if leg.get("action") == "BUY" else paper - leg["fill_price"]
            leg["slippage_pct"] = round(worse / paper, 6)
        self.save()
