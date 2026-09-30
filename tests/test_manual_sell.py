"""Tests for the manual Sell button (main.close_watch_manually): it only
sells when the stock's market is open and its price is current, and
otherwise closes nothing and says why.

Run from the project root:  py -m unittest discover -s tests
"""

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import main  # noqa: E402
from paper_trader import PaperTrader  # noqa: E402
from watch_manager import WatchManager  # noqa: E402


class ManualSellTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.multiple(config, ENTRY_MAX_PRICE_AGE_MIN=10, NOTIFY_SHORTS=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        b = main.StockAppBackend.__new__(main.StockAppBackend)
        b.status_callback = b.log_callback = b.alert_callback = None
        b.paper = PaperTrader(filename=os.path.join(tmp.name, 'paper_trades.json'), cost_pct=0.001)
        b.watch_mgr = WatchManager(filename=os.path.join(tmp.name, 'watches.json'))
        b.notifier = mock.Mock()
        self.b = b
        self.watch = b.watch_mgr.add_watch("AAA", "Acme", 100.0, "HIGH", "DAYS", "RALLY",
                                           extra={'spread_pct': 0.002})
        b.paper.open_trade(self.watch)
        self.session = True
        self.last = (110.0, time.time() - 60)
        self.quote = {'bid': 109.9, 'ask': 110.1, 'market_cap': 5e10, 'currency': 'USD',
                      'delay_min': 0}

    def sell(self, watch_id=None):
        lookup = main.price_lookup
        with mock.patch.object(main.strategy, 'market_open', return_value=self.session), \
             mock.patch.object(lookup, 'fetch_last_trade', return_value=self.last), \
             mock.patch.object(lookup, 'fetch_quote', return_value=self.quote), \
             mock.patch.object(lookup, 'fetch_prices', return_value={'SPY': 500.0}):
            return self.b.close_watch_manually(watch_id or self.watch['id'])

    def assert_still_open(self):
        self.assertEqual(len(self.b.watch_mgr.get_open_watches()), 1)
        self.assertEqual(self.b.paper.trades[0]['status'], 'OPEN')
        self.b.notifier.notify_sell.assert_not_called()

    def test_sells_at_the_bid_when_the_market_is_open(self):
        self.assertEqual(self.sell(), 109.9)
        self.assertEqual(self.b.watch_mgr.get_open_watches(), [])
        trade = self.b.paper.trades[0]
        self.assertEqual((trade['status'], trade['reason'], trade['exit_price']),
                         ('CLOSED', 'manual', 109.9))

    def test_refused_while_the_market_is_closed(self):
        self.session = False
        with self.assertRaisesRegex(main.SellRefused, "market is closed"):
            self.sell()
        self.assert_still_open()

    def test_refused_without_a_price(self):
        self.last = (None, None)
        with self.assertRaisesRegex(main.SellRefused, "Couldn't get a price"):
            self.sell()
        self.assert_still_open()

    def test_refused_on_a_stale_price(self):
        # "Open" by the clock but nothing has traded - a holiday or a halt.
        self.last = (110.0, time.time() - 3600)
        with self.assertRaisesRegex(main.SellRefused, "hasn't traded for 60 minutes"):
            self.sell()
        self.assert_still_open()

    def test_the_exchange_delay_is_allowed_for(self):
        self.last = (110.0, time.time() - 20 * 60)
        self.quote['delay_min'] = 15
        self.assertEqual(self.sell(), 109.9)

    def test_refused_for_a_position_that_is_not_open(self):
        with self.assertRaisesRegex(main.SellRefused, "no longer open"):
            self.sell("nope")

    def test_an_unmodelled_exchange_is_not_treated_as_closed(self):
        self.session = None
        self.assertEqual(self.sell(), 109.9)


if __name__ == "__main__":
    unittest.main()
