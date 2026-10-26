"""Tests for the strategy v9 entry and exit changes (CHANGE.md C7): the wait
for the session to settle, the wider first-day stop, intraday stories.
No network.

Run from the project root:  py -m unittest discover -s tests
"""

import datetime
import os
import sys
import tempfile
import unittest
from unittest import mock

import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import strategy  # noqa: E402
from paper_trader import PaperTrader  # noqa: E402

ET = pytz.timezone("US/Eastern")


def et(day, hour, minute=0):
    """A time in New York on October `day`, 2026 (the 26th is a Monday)."""
    return ET.localize(datetime.datetime(2026, 10, day, hour, minute))


class EntryReadyTests(unittest.TestCase):

    def setUp(self):
        p = mock.patch.multiple(config, ENTRY_DELAY_AFTER_OPEN_MIN=20, ENTRY_WAIT_FOR_SESSION=True)
        p.start()
        self.addCleanup(p.stop)

    def ready(self, now, published):
        return strategy.entry_ready_at("AAPL", now, published)

    def test_news_in_the_first_minutes_waits_for_the_delay(self):
        self.assertEqual(self.ready(et(26, 9, 35), et(26, 9, 31)), et(26, 9, 50))

    def test_news_later_in_the_session_opens_now(self):
        self.assertIsNone(self.ready(et(26, 10, 0), et(26, 9, 58)))

    def test_pre_market_news_waits_for_the_open(self):
        self.assertEqual(self.ready(et(26, 7, 30), et(26, 7, 25)), et(26, 9, 50))

    def test_weekend_news_waits_for_monday(self):
        self.assertEqual(self.ready(et(24, 12, 0), et(23, 18, 0)), et(26, 9, 50))

    def test_news_from_before_the_last_close_is_not_waited_for(self):
        # Published Wednesday midday; Thursday's open is not its news day.
        self.assertIs(self.ready(et(29, 8, 0), et(28, 12, 0)), False)
        # In the session, it is simply judged now.
        self.assertIsNone(self.ready(et(29, 9, 35), et(28, 12, 0)))

    def test_no_delay_opens_at_once(self):
        config.ENTRY_DELAY_AFTER_OPEN_MIN = 0
        self.assertIsNone(self.ready(et(26, 9, 31), et(26, 9, 30)))

    def test_news_from_the_last_minutes_of_a_session_waits_for_the_next(self):
        self.assertEqual(self.ready(et(26, 16, 5), et(26, 15, 50)), et(27, 9, 50))

    def test_waiting_for_the_session_can_be_turned_off(self):
        config.ENTRY_WAIT_FOR_SESSION = False
        self.assertIs(self.ready(et(26, 7, 30), et(26, 7, 25)), False)

    def test_unknown_publish_time_still_waits(self):
        self.assertEqual(self.ready(et(26, 7, 30), None), et(26, 9, 50))


class FirstDayStopTests(unittest.TestCase):

    def setUp(self):
        p = mock.patch.multiple(config, STOP_LOSS_PCT=0.08, STOP_ATR_MULT=1.5, STOP_MIN_PCT=0.02,
                                MIN_REWARD_RISK=1.2, MIN_MOVE_ATR_MULT=1.2, FIRST_DAY_STOP_MULT=1.5,
                                BREAKEVEN_AT=0.5, TRAIL_AFTER_TARGET_MULT=0.5, SKIP_INTRADAY=True,
                                REPORTED_MOVE_MAX_PCT=0.05, MERGER_ARB=True)
        p.start()
        self.addCleanup(p.stop)

    def test_the_plan_widens_the_first_stop_but_not_the_target(self):
        plan = strategy.plan_trade('LONG', 'HIGH', 0.06, {'atr_pct': 0.02})
        self.assertTrue(plan['ok'])
        self.assertAlmostEqual(plan['stop_pct'], 0.045)
        self.assertAlmostEqual(plan['settled_stop_pct'], 0.03)
        self.assertAlmostEqual(plan['target_pct'], 0.06)

    def test_merger_arb_has_one_fixed_stop(self):
        plan = strategy.plan_trade('LONG', 'HIGH', 0.06, {'atr_pct': 0.02}, capped=True)
        self.assertIsNone(plan['settled_stop_pct'])

    def watch(self):
        return {'direction': 'LONG', 'entry_price': 100.0, 'target_pct': 0.06,
                'stop_pct': 0.045, 'settled_stop_pct': 0.03, 'stop_gain': -0.045,
                'first_day_until': et(26, 16, 0).isoformat(), 'let_run': True}

    def test_the_wide_stop_holds_on_news_day(self):
        w = self.watch()
        reason, _, _ = strategy.update_exit_bar(w, (97.0, 97.0, 96.0, 96.5), when=et(26, 11, 0))
        self.assertIsNone(reason)
        self.assertAlmostEqual(w['stop_gain'], -0.045)

    def test_the_stop_settles_after_the_first_session(self):
        w = self.watch()
        reason, level, _ = strategy.update_exit_bar(w, (97.0, 97.0, 96.0, 96.5), when=et(27, 9, 31))
        self.assertEqual(reason, 'stop_loss')
        self.assertAlmostEqual(level, 97.0)

    def test_a_position_without_a_first_day_stop_is_unchanged(self):
        w = {'stop_pct': 0.03}
        self.assertEqual(strategy.stop_distance(w, et(26, 11, 0)), 0.03)

    def test_the_first_session_ends_at_that_days_close(self):
        self.assertEqual(strategy.first_session_end(et(26, 10, 0), "AAPL"), et(26, 16, 0))
        # Opened on a Friday after the close: Monday's session is its first.
        self.assertEqual(strategy.first_session_end(et(30, 16, 30), "AAPL"),
                         ET.localize(datetime.datetime(2026, 11, 2, 16, 0)))

    def test_intraday_stories_are_skipped(self):
        plan = strategy.plan_trade('LONG', 'HIGH', 0.06, {'atr_pct': 0.02}, horizon='INTRADAY')
        self.assertEqual(plan['rule'], 'intraday')
        config.SKIP_INTRADAY = False
        self.assertTrue(strategy.plan_trade('LONG', 'HIGH', 0.06, {'atr_pct': 0.02},
                                            horizon='INTRADAY')['ok'])


class QueuedEntryTests(unittest.TestCase):
    """A signal for a shut market waits in main._pending_entries and opens,
    with its entry alert, once its time has come."""

    def setUp(self):
        import main
        from watch_manager import WatchManager
        p = mock.patch.multiple(config, PAPER_BUDGET=1000.0, PAPER_RISK_PCT=0.01,
                                PAPER_MAX_POSITION_PCT=0.25, ENTRY_MAX_PRICE_AGE_MIN=10,
                                ENTRY_MAX_SPREAD_PCT=0.015, MAX_OPEN_POSITIONS=20,
                                PAPER_FEE_PER_ORDER=0.0, PAPER_FX_FEE_PCT=0.0, PAPER_MAX_FEE_PCT=0.0,
                                FIRST_DAY_STOP_MULT=1.0, ENTRY_DELAY_AFTER_OPEN_MIN=20,
                                ENTRY_WAIT_FOR_SESSION=True, NOTIFY_SHORTS=True)
        p.start()
        self.addCleanup(p.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        b = main.StockAppBackend.__new__(main.StockAppBackend)
        b.status_callback = b.log_callback = None
        b.paper = PaperTrader(filename=os.path.join(tmp.name, 'paper_trades.json'), cost_pct=0.001)
        b.watch_mgr = WatchManager(filename=os.path.join(tmp.name, 'watches.json'))
        b.shadows = mock.Mock()
        b.notifier = mock.Mock()
        b.portfolio_mgr = mock.Mock(has_stock=mock.Mock(return_value=False))
        b._pending_entries = []
        self.b, self.main = b, main
        import time
        self.ctx = {'price': 100.0, 'atr_pct': 0.02, 'change_since_close_pct': 0.0,
                    'market_price': 500.0, 'market_since_close_pct': 0.0,
                    'price_time': time.time() - 30}
        self.quote = {'bid': 99.9, 'ask': 100.1, 'delay_min': 0}

    def test_waits_then_opens_and_alerts(self):
        # No publish time: waits whatever the clock says (EntryReadyTests
        # covers the timing).
        args = ("AAA", "Acme", "LONG", "HIGH", "RALLY", 0.08, 80, {'horizon': 'DAYS'},
                {}, "u", "t")
        with mock.patch.object(self.main.strategy, 'market_open', return_value=False), \
             mock.patch.object(self.main.price_lookup, 'fetch_context') as fetch:
            d = self.b._decide_trade(*args)
        self.assertFalse(d['opened'])
        self.assertIn("entry waits until", d['reason'])
        self.assertTrue(d['pending_until'])
        fetch.assert_not_called()
        self.assertEqual(len(self.b._pending_entries), 1)

        # A second story on the same stock doesn't queue a second entry.
        with mock.patch.object(self.main.strategy, 'market_open', return_value=False):
            self.assertIn("already waiting", self.b._decide_trade(*args)['reason'])

        self.b._pending_entries[0]['ready_at'] = (datetime.datetime.now(datetime.timezone.utc)
                                                  - datetime.timedelta(seconds=1))
        with mock.patch.object(self.main.strategy, 'market_open', return_value=True), \
             mock.patch.object(self.main.price_lookup, 'fetch_context', return_value=self.ctx), \
             mock.patch.object(self.main.price_lookup, 'fetch_quote', return_value=self.quote):
            self.b._run_pending_entries()
        self.assertEqual(self.b._pending_entries, [])
        self.assertEqual(len(self.b.paper.trades), 1)
        decision = self.b.notifier.notify.call_args[1]['decision']
        self.assertTrue(decision['opened'])

    def test_a_queued_entry_on_a_holiday_does_not_open(self):
        self.b._pending_entries.append({
            'ticker': "AAA", 'ready_at': datetime.datetime.now(datetime.timezone.utc),
            'args': ("AAA", "Acme", "LONG", "HIGH", "RALLY", 0.08, 80, {'horizon': 'DAYS'},
                     {}, "u", "t")})
        with mock.patch.object(self.main.strategy, 'market_open', return_value=False), \
             mock.patch.object(self.main.price_lookup, 'fetch_context') as fetch:
            self.b._run_pending_entries()
        fetch.assert_not_called()
        self.assertEqual(self.b._pending_entries, [])
        self.b.notifier.notify.assert_not_called()


if __name__ == '__main__':
    unittest.main()
