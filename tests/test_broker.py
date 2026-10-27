"""Tests for the IBKR paper-account mirror (broker.py), against a fake
Gateway: symbols map to IBKR's, only paper accounts are traded, entries and
exits go out as the right orders, and a Gateway that is down never loses an
exit.

Run from the project root:  py -m unittest discover -s tests
"""

import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import broker  # noqa: E402
import config  # noqa: E402


class FakeEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class FakeTrade:
    def __init__(self, order, fill_price):
        self.order = order
        self.orderStatus = SimpleNamespace(status="Submitted", filled=0.0, avgFillPrice=0.0)
        self.log = []
        self._fill_price = fill_price

    def fill(self):
        self.orderStatus.status = "Filled"
        self.orderStatus.filled = float(self.order.totalQuantity)
        self.orderStatus.avgFillPrice = self._fill_price

    def isDone(self):
        return self.orderStatus.status in broker.DONE_STATES


class FakeIB:
    """Stands in for ib_async.IB. Orders fill at once at `fill_price`
    unless `fills` is False; symbols in `unknown` can't be qualified."""

    def __init__(self, accounts=("DU1234567",), fill_price=101.0, fills=True, unknown=()):
        self.accounts = list(accounts)
        self.fill_price = fill_price
        self.fills = fills
        self.unknown = set(unknown)
        self.connected = False
        self.placed = []
        self.cancelled = []
        self._trades = []
        self.orderStatusEvent = FakeEvent()

    def connect(self, host, port, clientId, timeout):
        self.connected = True

    def disconnect(self):
        self.connected = False

    def isConnected(self):
        return self.connected

    def managedAccounts(self):
        return self.accounts

    def qualifyContracts(self, contract):
        return [] if contract.symbol in self.unknown else [contract]

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        trade = FakeTrade(order, self.fill_price)
        if self.fills:
            trade.fill()
        self._trades.append(trade)
        return trade

    def cancelOrder(self, order):
        self.cancelled.append(order)
        for trade in self._trades:
            if trade.order is order:
                trade.orderStatus.status = "Cancelled"

    def trades(self):
        return list(self._trades)

    def sleep(self, seconds):
        pass


def contract(symbol, exchange, currency):
    return SimpleNamespace(symbol=symbol, exchange=exchange, currency=currency)


def order(action, qty, account, ref):
    return SimpleNamespace(action=action, totalQuantity=qty, account=account, orderRef=ref)


def watch(ticker="AAA", direction="LONG", entry=100.0, size=500.0, wid="w1"):
    return {"id": wid, "ticker": ticker, "direction": direction,
            "entry_price": entry, "position_usd": size}


class ListingTest(unittest.TestCase):
    def test_us_symbols_and_share_classes(self):
        self.assertEqual(broker.ib_listing("AAPL"), ("AAPL", "", "USD"))
        self.assertEqual(broker.ib_listing("BRK-B"), ("BRK B", "", "USD"))
        self.assertEqual(broker.ib_listing("BRK.B"), ("BRK B", "", "USD"))

    def test_european_listings(self):
        self.assertEqual(broker.ib_listing("BMW.DE"), ("BMW", "IBIS", "EUR"))
        self.assertEqual(broker.ib_listing("BT-A.L"), ("BT.A", "LSE", "GBP"))
        self.assertEqual(broker.ib_listing("NESN.SW"), ("NESN", "EBS", "CHF"))

    def test_unmapped_exchange(self):
        self.assertIsNone(broker.ib_listing("CEZ.PR"))
        self.assertIsNone(broker.ib_listing("SHOP.TO"))

    def test_quantity_whole_shares_and_pence(self):
        self.assertEqual(broker.order_quantity(watch(entry=30.0, size=100.0)), 3)
        # 250p = £2.50 a share.
        self.assertEqual(broker.order_quantity(watch(ticker="VOD.L", entry=250.0, size=100.0)), 40)
        self.assertEqual(broker.order_quantity(watch(entry=600.0, size=100.0)), 0)
        self.assertEqual(broker.order_quantity({"ticker": "AAA", "entry_price": 10.0}), 0)


class BrokerTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.multiple(config, IBKR_HOST="127.0.0.1", IBKR_PORT=4002,
                                      IBKR_CLIENT_ID=17, IBKR_ACCOUNT="")
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = os.path.join(tmp.name, "broker_orders.json")
        self.ib = FakeIB()
        self.lines = []

    def make(self, ib=None):
        b = broker.Broker(filename=self.file, log=self.lines.append,
                          ib_factory=lambda: ib or self.ib,
                          contract_factory=contract, order_factory=order)
        return b

    def test_long_round_trip(self):
        b = self.make()
        b.open_position(watch())
        b.step()
        _, sent = self.ib.placed[0]
        self.assertEqual((sent.action, sent.totalQuantity, sent.account), ("BUY", 5, "DU1234567"))
        rec = b.records["w1"]
        self.assertEqual(rec["open"]["status"], "Filled")
        self.assertAlmostEqual(rec["open"]["slippage_pct"], 0.01)

        self.ib.fill_price = 110.0
        b.close_position(watch(), "target", 110.5)
        b.step()
        _, sent = self.ib.placed[1]
        self.assertEqual((sent.action, sent.totalQuantity), ("SELL", 5))
        self.assertEqual(rec["close"]["status"], "Filled")
        # Sold at 110.0 against a paper 110.5: worse by the difference.
        self.assertAlmostEqual(rec["close"]["slippage_pct"], 0.5 / 110.5, places=6)

    def test_short_opens_with_sell_and_covers_with_buy(self):
        b = self.make()
        b.open_position(watch(direction="SHORT"))
        b.step()
        b.close_position(watch(direction="SHORT"), "stop_loss", 105.0)
        b.step()
        self.assertEqual([o.action for _, o in self.ib.placed], ["SELL", "BUY"])

    def test_live_account_is_refused(self):
        ib = FakeIB(accounts=["U7654321"])
        b = self.make(ib)
        b.open_position(watch())
        b.step()
        self.assertEqual(ib.placed, [])
        self.assertFalse(ib.connected)
        self.assertIn("live account", b.last_error)

    def test_several_accounts_need_one_chosen(self):
        ib = FakeIB(accounts=["DU1", "DU2"])
        b = self.make(ib)
        b.open_position(watch())
        b.step()
        self.assertEqual(ib.placed, [])
        with mock.patch.object(config, "IBKR_ACCOUNT", "DU2"):
            b._last_attempt = 0
            b.step()
        self.assertEqual(ib.placed[0][1].account, "DU2")

    def test_exit_survives_gateway_outage_and_restart(self):
        b = self.make()
        b.open_position(watch())
        b.step()
        self.ib.disconnect()
        b._last_attempt = time.time()  # the next reconnect isn't due yet
        b.close_position(watch(), "time_exit", 100.0)
        b.step()
        self.assertEqual(len(self.ib.placed), 1)
        self.assertEqual(b.records["w1"]["close"]["status"], "pending")

        # The app restarts; the pending exit is picked up from the file.
        b2 = self.make()
        b2.step()
        self.assertEqual([o.action for _, o in self.ib.placed], ["BUY", "SELL"])
        self.assertEqual(b2.records["w1"]["close"]["status"], "Filled")

    def test_stale_entry_is_dropped_while_offline(self):
        b = self.make()
        b._last_attempt = time.time()
        b.open_position(watch())
        b._jobs.queue[0]["queued_at"] -= broker.OPEN_MAX_DELAY_S + 1
        b.step()
        self.assertEqual(b.records["w1"]["open"]["status"], "Skipped")
        self.assertEqual(self.ib.placed, [])

    def test_unfilled_entry_is_cancelled_not_closed(self):
        ib = FakeIB(fills=False)
        b = self.make(ib)
        b.open_position(watch())
        b.step()
        b.close_position(watch(), "news_reversal", 100.0)
        b.step()
        self.assertEqual(len(ib.cancelled), 1)
        self.assertEqual(len(ib.placed), 1)
        self.assertEqual(b.records["w1"]["close"]["status"], "Skipped")

    def test_unknown_symbol_and_unmapped_exchange_are_skipped(self):
        ib = FakeIB(unknown={"ZZZZ"})
        b = self.make(ib)
        b.open_position(watch(ticker="ZZZZ"))
        b.open_position(watch(ticker="CEZ.PR", wid="w2"))
        b.step()
        self.assertEqual(ib.placed, [])
        self.assertEqual(b.records["w1"]["open"]["status"], "Skipped")
        self.assertEqual(b.records["w2"]["open"]["status"], "Skipped")
        # Nothing held, so the exit sends nothing either.
        b.close_position(watch(ticker="ZZZZ"), "target", 100.0)
        b.step()
        self.assertEqual(ib.placed, [])

    def test_missing_library_is_reported(self):
        def no_library():
            raise ImportError("No module named 'ib_async'")
        b = broker.Broker(filename=self.file, log=self.lines.append, ib_factory=no_library)
        b.open_position(watch())
        b.step()
        self.assertIn("pip install ib_async", b.last_error)


class SyncBrokerTest(unittest.TestCase):
    """Turning the mirror on or off from the dashboard applies at the next
    start of the monitoring."""

    def setUp(self):
        import main
        self.main = main
        self.backend = main.StockAppBackend.__new__(main.StockAppBackend)
        self.backend.broker = None
        self.backend.log = lambda m: None

    def test_created_when_enabled_and_dropped_when_disabled(self):
        fake = mock.Mock()
        with mock.patch.object(self.main, "Broker", return_value=fake), \
                mock.patch.object(self.main, "PAPER_TRADING", True):
            with mock.patch.object(config, "IBKR_ENABLED", True):
                self.backend._sync_broker()
                self.assertIs(self.backend.broker, fake)
                self.assertEqual(self.backend.broker_status(), fake.status())
            with mock.patch.object(config, "IBKR_ENABLED", False):
                self.backend._sync_broker()
                self.assertIsNone(self.backend.broker)
                fake.stop.assert_called_once()
                self.assertIsNone(self.backend.broker_status())


if __name__ == "__main__":
    unittest.main()
