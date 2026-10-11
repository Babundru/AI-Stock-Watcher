"""Tests for the exits as a real broker would run them: stops checked against
every 1-minute bar and filled at their level, no exit on a stale price,
positions with no price at all closed after a while, the broker's fees, and
trading hours learned for exchanges markets.py doesn't model. No network.

Run from the project root:  py -m unittest discover -s tests
"""

import datetime
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import costs  # noqa: E402
import fills  # noqa: E402
import markets  # noqa: E402
import paper_account  # noqa: E402
import strategy  # noqa: E402
from paper_trader import PaperTrader  # noqa: E402

UTC = datetime.timezone.utc
# Wednesday 30 September 2026, 10:30 New York time - the US session.
NOW = datetime.datetime(2026, 9, 30, 14, 30, tzinfo=UTC)
NOFEES = dict(PAPER_FEE_PER_ORDER=0.0, PAPER_FX_FEE_PCT=0.0, PAPER_MAX_FEE_PCT=0.0)


def position(direction="LONG", entry=100.0, stop=0.05, target=0.10, let_run=True, **extra):
    p = {'id': 'w1', 'ticker': 'AAA', 'company': 'Acme', 'direction': direction,
         'entry_price': entry, 'stop_pct': stop, 'stop_gain': -stop, 'target_pct': target,
         'target_price': strategy.price_at_gain(direction, entry, target),
         'peak_gain': 0.0, 'let_run': let_run, 'trailing': False,
         'opened_at': (NOW - datetime.timedelta(hours=2)).isoformat(),
         'expires_at': (NOW + datetime.timedelta(days=2)).isoformat()}
    p.update(extra)
    return p


def bar(minutes_ago, o, h, l, c):
    """A 1-minute bar that started `minutes_ago` before NOW."""
    return ((NOW - datetime.timedelta(minutes=minutes_ago)).timestamp(), o, h, l, c)


class BarExitTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.multiple(config, BREAKEVEN_AT=0.5, TRAIL_AFTER_TARGET_MULT=0.5)
        p.start()
        self.addCleanup(p.stop)

    def test_a_dip_between_checks_triggers_the_stop_at_its_level(self):
        # Low 94 went through the 95 stop; the minute closed back at 99 -
        # a last-price check would never have seen it.
        reason, price, _ = strategy.update_exit_bar(position(), (99, 99.5, 94, 99))
        self.assertEqual((reason, price), ('stop_loss', 95.0))

    def test_a_gap_through_the_stop_fills_at_the_open(self):
        reason, price, _ = strategy.update_exit_bar(position(), (90, 91, 89, 90.5))
        self.assertEqual((reason, price), ('stop_loss', 90.0))

    def test_a_short_is_stopped_by_the_high(self):
        reason, price, _ = strategy.update_exit_bar(position("SHORT"), (101, 106, 100, 101))
        self.assertEqual((reason, price), ('stop_loss', 105.0))

    def test_a_target_order_fills_at_the_target(self):
        reason, price, _ = strategy.update_exit_bar(position(let_run=False), (105, 112, 104, 106))
        self.assertEqual((reason, price), ('target_hit', 110.0))

    def test_a_bar_through_both_is_read_pessimistically(self):
        reason, price, _ = strategy.update_exit_bar(position(let_run=False), (100, 111, 94, 100))
        self.assertEqual((reason, price), ('stop_loss', 95.0))

    def test_a_stop_raised_by_the_high_is_checked_against_the_close(self):
        # High 112 reaches the 10% target: the stop trails 2.5% behind, at
        # +9.5%, and the close at 105 is below it.
        p = position()
        reason, price, reached = strategy.update_exit_bar(p, (104, 112, 104, 105))
        self.assertTrue(reached)
        self.assertEqual(reason, 'trailing_stop')
        self.assertAlmostEqual(price, 109.5)

    def test_a_quiet_bar_only_ratchets(self):
        p = position()
        self.assertEqual(strategy.update_exit_bar(p, (100, 103, 99, 102))[0], None)
        self.assertAlmostEqual(p['peak_gain'], 0.03)
        self.assertAlmostEqual(p['stop_gain'], -0.02)


class SessionCountTest(unittest.TestCase):
    def test_sessions_without_price(self):
        last = datetime.datetime(2026, 9, 25, 19, 0, tzinfo=UTC)    # Friday 15:00 ET
        # Monday's and Tuesday's sessions closed with no price since.
        self.assertEqual(strategy.sessions_without_price("AAA", last, NOW), 2)
        self.assertEqual(strategy.sessions_without_price(
            "AAA", datetime.datetime(2026, 9, 29, 19, 0, tzinfo=UTC), NOW), 0)

    def test_session_started(self):
        self.assertTrue(strategy.session_started("AAA", NOW - datetime.timedelta(minutes=30), NOW))
        self.assertFalse(strategy.session_started("AAA", NOW - datetime.timedelta(hours=2), NOW))


class WatchPassTest(unittest.TestCase):
    """main._run_bars and main._time_exit - the watch check's two halves."""

    def setUp(self):
        import main
        self.main = main
        p = mock.patch.multiple(config, EXITS_REGULAR_HOURS_ONLY=True, NO_PRICE_CLOSE_SESSIONS=2,
                                BREAKEVEN_AT=0.5, TRAIL_AFTER_TARGET_MULT=0.5)
        p.start()
        self.addCleanup(p.stop)

    def run_bars(self, p, rows):
        return self.main.StockAppBackend._run_bars(p, rows, NOW, 0.0)

    def test_bars_since_the_last_check_are_all_read(self):
        p = position(last_bar_ts=bar(10, 0, 0, 0, 0)[0])
        seen = self.run_bars(p, [bar(9, 100, 101, 99, 100), bar(6, 100, 100, 94, 99),
                                 bar(2, 99, 99, 98, 99)])
        # The first bar's high trailed the 5% stop up to 96; the second's
        # low went through it.
        self.assertEqual((seen['reason'], seen['price']), ('stop_loss', 96.0))
        self.assertEqual(p['last_bar_ts'], bar(6, 0, 0, 0, 0)[0])

    def test_the_forming_bar_and_old_bars_are_skipped(self):
        p = position(last_bar_ts=bar(5, 0, 0, 0, 0)[0])
        seen = self.run_bars(p, [bar(8, 90, 90, 90, 90), bar(0, 90, 90, 90, 90)])
        self.assertIsNone(seen['reason'])
        self.assertIsNone(seen['close'])

    def test_bars_outside_the_session_do_nothing(self):
        # 08:00 ET, pre-market: a crash there fires nothing.
        early = datetime.datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
        p = position(opened_at=(early - datetime.timedelta(hours=1)).isoformat())
        row = (early.timestamp(), 80, 80, 80, 80)
        seen = self.main.StockAppBackend._run_bars(p, [row], early + datetime.timedelta(minutes=5),
                                                   0.0)
        self.assertIsNone(seen['reason'])
        self.assertFalse(seen['in_session'])
        self.assertAlmostEqual(p['stop_gain'], -0.05)

    def test_time_exit_closes_at_todays_price(self):
        p = position(expires_at=(NOW - datetime.timedelta(minutes=1)).isoformat(),
                     last_price=103.0, last_price_at=(NOW - datetime.timedelta(minutes=2)).isoformat())
        self.assertEqual(self.main.StockAppBackend._time_exit(p, NOW, True),
                         ('horizon_expired', 103.0, False))

    def test_time_exit_waits_for_a_price_from_today(self):
        p = position(expires_at=(NOW - datetime.timedelta(minutes=1)).isoformat(),
                     last_price=103.0,
                     last_price_at=datetime.datetime(2026, 9, 29, 19, 0, tzinfo=UTC).isoformat())
        self.assertEqual(self.main.StockAppBackend._time_exit(p, NOW, True), (None, None, None))

    def test_no_price_for_two_sessions_closes_at_the_last_one(self):
        p = position(last_price=6.5,
                     last_price_at=datetime.datetime(2026, 9, 25, 19, 0, tzinfo=UTC).isoformat())
        self.assertEqual(self.main.StockAppBackend._time_exit(p, NOW, True),
                         ('no_price', 6.5, True))
        self.assertTrue(p['exit_price_stale'])

    def test_nothing_closes_while_the_market_is_shut(self):
        p = position(last_price=6.5,
                     last_price_at=datetime.datetime(2026, 9, 25, 19, 0, tzinfo=UTC).isoformat())
        self.assertEqual(self.main.StockAppBackend._time_exit(p, NOW, False), (None, None, None))


class CheckWatchesTest(unittest.TestCase):
    """The whole pass, with the price feed mocked: a dip between two checks
    closes the position at the stop, plus half the spread."""

    def setUp(self):
        import main
        from watch_manager import WatchManager
        p = mock.patch.multiple(config, EXITS_REGULAR_HOURS_ONLY=True, NOTIFY_SHORTS=True,
                                **NOFEES)
        p.start()
        self.addCleanup(p.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        b = main.StockAppBackend.__new__(main.StockAppBackend)
        b.status_callback = b.log_callback = b.alert_callback = None
        b.paper = PaperTrader(filename=os.path.join(tmp.name, 'paper_trades.json'), cost_pct=0.0)
        b.watch_mgr = WatchManager(filename=os.path.join(tmp.name, 'watches.json'))
        b.shadows = None
        b.notifier = mock.Mock()
        self.b, self.main = b, main
        with mock.patch.object(main, 'now_local', return_value=NOW - datetime.timedelta(hours=1)), \
             mock.patch('watch_manager.now_local', return_value=NOW - datetime.timedelta(hours=1)):
            self.watch = b.watch_mgr.add_watch("AAA", "Acme", 100.0, "HIGH", "DAYS", "RALLY",
                                               stop_pct=0.05, target_pct=0.10,
                                               extra={'spread_pct': 0.002})
        b.paper.open_trade(self.watch)

    def check(self, rows):
        lookup = self.main.price_lookup
        with mock.patch.object(self.main, 'now_local', return_value=NOW), \
             mock.patch.object(lookup, 'fetch_bars', return_value={'AAA': rows, 'SPY': rows}), \
             mock.patch.object(lookup, 'fetch_quote', return_value={}), \
             mock.patch.object(self.main, '_release_memory'):
            self.b._check_watches()

    def test_a_dip_between_checks_closes_at_the_stop(self):
        self.check([bar(7, 100, 100, 99, 99.5), bar(4, 99, 99, 94, 98), bar(2, 98, 98, 97, 98)])
        trade = self.b.paper.trades[0]
        self.assertEqual(trade['status'], 'CLOSED')
        self.assertEqual(trade['reason'], 'stop_loss')
        self.assertAlmostEqual(trade['exit_price'], 95.0 * (1 - 0.001))
        self.assertAlmostEqual(trade['mae_pct'], -0.06)

    def test_no_bars_means_no_exit(self):
        self.check([])
        self.assertEqual(self.b.paper.trades[0]['status'], 'OPEN')


class FeeTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.multiple(config, PAPER_COST_PCT=0.001, PAPER_FEE_PER_ORDER=1.0,
                                PAPER_FX_FEE_PCT=0.0025, PAPER_MAX_FEE_PCT=0.02,
                                PAPER_BUDGET=1000.0, PAPER_RISK_PCT=0.01,
                                PAPER_MAX_POSITION_PCT=0.25, PAPER_MIN_POSITION=20.0)
        p.start()
        self.addCleanup(p.stop)

    def test_round_trip_cost(self):
        self.assertAlmostEqual(costs.round_trip_pct({'ticker': 'AAA'}, 200.0), 0.001 + 0.01)
        self.assertAlmostEqual(costs.round_trip_pct({'ticker': 'VOD.L'}, 200.0),
                               0.001 + 0.01 + 0.005)
        # No size known (an old trade): no fixed fee to spread.
        self.assertAlmostEqual(costs.round_trip_pct({'ticker': 'AAA'}), 0.001)

    def test_a_position_too_small_for_its_fees_is_refused(self):
        s = paper_account.settings()
        self.assertEqual(paper_account.position_size(1000, 1000, 0.04, 80, "HIGH", s)['size'], 250.0)
        got = paper_account.position_size(1000, 60, 0.04, 80, "HIGH", s)
        self.assertIsNone(got['size'])
        self.assertIn("fees", got['reason'])

    def test_a_closed_trade_pays_its_fees(self):
        with tempfile.TemporaryDirectory() as tmp:
            paper = PaperTrader(filename=os.path.join(tmp, 'paper_trades.json'), cost_pct=0.001)
            watch = {'id': 'w1', 'ticker': 'SAP.DE', 'company': 'SAP', 'direction': 'LONG',
                     'entry_price': 100.0, 'position_usd': 200.0, 'opened_at': NOW.isoformat()}
            paper.open_trade(watch)
            t = paper.close_trade(dict(watch, closed_at=NOW.isoformat()), 110.0)
            self.assertAlmostEqual(t['cost_pct'], 0.001 + 0.01 + 0.005)
            self.assertAlmostEqual(t['net_pct'], 0.10 - 0.016)


    def test_the_account_costs_closed_trades_under_todays_rules(self):
        t = {'watch_id': 'a', 'ticker': 'AAA', 'direction': 'LONG', 'entry_price': 100.0,
             'opened_at': NOW.isoformat(), 'stop_pct': 0.04, 'confidence': 80,
             'impact': 'HIGH', 'status': 'CLOSED', 'gross_pct': 0.10, 'net_pct': 0.098,
             'closed_at': (NOW + datetime.timedelta(hours=1)).isoformat()}
        acct = paper_account.replay([t], 0.001, now=NOW + datetime.timedelta(hours=2),
                                    curve=False)
        # $250 position: 0.1% + $2 of fees (0.8%), not the 0.2% it closed with.
        self.assertAlmostEqual(acct['nets']['a'], 0.10 - 0.001 - 0.008)
        self.assertAlmostEqual(acct['realised_pnl'], round(250 * 0.091, 2))


class LevelFillTest(unittest.TestCase):
    def test_a_stop_fills_around_its_level_not_the_current_quote(self):
        quote = {'bid': 89.9, 'ask': 90.1, 'market_cap': 5e10, 'currency': 'USD'}
        now_fill = fills.exit_fill("LONG", 90.0, quote, True)
        stop_fill = fills.exit_fill("LONG", 95.0, quote, True, at_level=True)
        self.assertEqual(now_fill['price'], 89.9)
        self.assertAlmostEqual(stop_fill['price'], 95.0 * (1 - stop_fill['spread_pct'] / 2), places=4)
        self.assertEqual(stop_fill['spread_source'], 'quote')


class LearnedMarketTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = mock.patch.object(markets, 'LEARNED_FILE', os.path.join(tmp.name, 'sessions.json'))
        p.start()
        self.addCleanup(p.stop)
        saved = dict(markets._learned)
        self.addCleanup(lambda: (markets._learned.clear(), markets._learned.update(saved)))
        markets._learned.clear()

    def test_tokyo_is_not_a_us_class_share(self):
        self.assertIsNone(markets.market_key("6594.T"))
        self.assertEqual(markets.market_key("BRK.B"), markets.US)

    def test_a_learned_session_gates_the_stock_and_survives_a_restart(self):
        self.assertTrue(markets.needs_learning("TLX.AX"))
        self.assertIsNone(markets.session_open("TLX.AX"))
        markets.learn("TLX.AX", "ASX", "Australia/Sydney", datetime.time(10), datetime.time(16, 12))
        self.assertFalse(markets.needs_learning("BHP.AX"))
        # 20:17 in Sydney - when TLX.AX was bought on a stale price.
        evening = datetime.datetime(2026, 9, 22, 10, 17, tzinfo=UTC)
        self.assertFalse(markets.session_open("TLX.AX", evening))
        self.assertTrue(markets.session_open("TLX.AX", datetime.datetime(2026, 9, 22, 1, 0,
                                                                         tzinfo=UTC)))
        markets._learned.clear()
        markets.load_learned()
        self.assertEqual(markets.market("BHP.AX").close_at, datetime.time(16, 12))


if __name__ == '__main__':
    unittest.main()
