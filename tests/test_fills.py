"""Tests for fills.py: paper trades fill at the bid/ask, not the last trade,
and only on a fresh price across a spread that isn't too wide.

Run from the project root:  py -m unittest discover -s tests
"""

import datetime
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import fills  # noqa: E402
import paper_account  # noqa: E402
from paper_trader import PaperTrader  # noqa: E402

NOW = datetime.datetime(2026, 9, 28, 15, 0, tzinfo=datetime.timezone.utc)


def minutes_ago(m):
    return (NOW - datetime.timedelta(minutes=m)).timestamp()


class SpreadTest(unittest.TestCase):
    def test_a_believable_quote_is_used(self):
        f = fills.entry_fill("LONG", 19.81, {'bid': 19.78, 'ask': 19.84}, True)
        self.assertEqual(f['price'], 19.84)
        self.assertEqual(f['spread_source'], 'quote')
        self.assertAlmostEqual(f['spread_pct'], 0.06 / 19.81, places=4)

    def test_a_short_sells_at_the_bid_and_buys_back_at_the_ask(self):
        quote = {'bid': 99.9, 'ask': 100.1}
        self.assertEqual(fills.entry_fill("SHORT", 100.0, quote, True)['price'], 99.9)
        self.assertEqual(fills.exit_fill("SHORT", 100.0, quote, True)['price'], 100.1)
        self.assertEqual(fills.exit_fill("LONG", 100.0, quote, True)['price'], 99.9)

    def test_junk_us_quotes_fall_back_to_an_estimate(self):
        # Seen from Yahoo: AAPL "339.74 / 350.00" with the last trade below
        # the bid, and LFCR "4.68 / 8.24" - a 55% spread.
        aapl = fills.entry_fill("LONG", 338.26, {'bid': 339.74, 'ask': 350.0,
                                                 'market_cap': 4.9e12, 'currency': 'USD'}, True)
        self.assertEqual(aapl['spread_source'], 'estimate')
        self.assertAlmostEqual(aapl['spread_pct'], 0.001)
        self.assertAlmostEqual(aapl['price'], 338.26 * 1.0005, places=4)
        lfcr = fills.entry_fill("LONG", 6.565, {'bid': 4.68, 'ask': 8.24,
                                                'market_cap': 2.5e8, 'currency': 'USD'}, True)
        self.assertEqual(lfcr['spread_source'], 'estimate')
        self.assertAlmostEqual(lfcr['spread_pct'], 0.015)

    def test_a_quote_far_wider_than_the_company_size_suggests_is_ignored(self):
        # 3% on a mega-cap is junk even with the last trade inside it.
        f = fills.entry_fill("LONG", 100.0, {'bid': 98.5, 'ask': 101.5,
                                             'market_cap': 1e12, 'currency': 'USD'}, True)
        self.assertEqual(f['spread_source'], 'estimate')
        # The same quote on a $30M company is believable.
        f = fills.entry_fill("LONG", 100.0, {'bid': 98.5, 'ask': 101.5,
                                             'market_cap': 3e7, 'currency': 'USD'}, True)
        self.assertEqual(f['spread_source'], 'quote')

    def test_estimates_follow_company_size_in_dollars(self):
        self.assertEqual(fills.estimate_spread(3e10, 'USD'), 0.001)
        self.assertEqual(fills.estimate_spread(2e7, 'USD'), 0.03)
        # A London market cap is in pounds even when the price is in pence.
        self.assertEqual(fills.estimate_spread(4.2e8, 'GBp'), 0.006)
        # 7.5 trillion won is about $5bn, not $7.5 trillion.
        self.assertEqual(fills.estimate_spread(7.5e12, 'KRW'), 0.0025)
        self.assertEqual(fills.estimate_spread(None, None), fills.UNKNOWN_SPREAD)

    def test_estimates_widen_out_of_hours(self):
        self.assertAlmostEqual(fills.estimate_spread(2.5e8, 'USD', in_session=False), 0.03)
        # Unknown session (an exchange markets.py doesn't model): no change.
        self.assertAlmostEqual(fills.estimate_spread(2.5e8, 'USD', in_session=None), 0.015)

    def test_an_exit_without_a_quote_reuses_the_entry_spread(self):
        f = fills.exit_fill("LONG", 100.0, {}, True, fallback_spread=0.02)
        self.assertAlmostEqual(f['price'], 99.0)
        self.assertEqual(f['spread_pct'], 0.02)

    def test_exit_haircut_is_half_the_entry_spread(self):
        self.assertEqual(fills.exit_haircut({'spread_pct': 0.01}), 0.005)
        self.assertEqual(fills.exit_haircut({}), 0)


class EntryProblemTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.multiple(config, ENTRY_MAX_PRICE_AGE_MIN=10, ENTRY_MAX_SPREAD_PCT=0.015)
        p.start()
        self.addCleanup(p.stop)
        self.fill = {'price': 100.05, 'last': 100.0, 'spread_pct': 0.001,
                     'spread_source': 'quote'}

    def problem(self, **kw):
        args = dict(price_time=minutes_ago(2), delay_min=0, published_at=None, now=NOW)
        args.update(kw)
        return fills.entry_problem(self.fill, **args)

    def test_a_fresh_tight_price_is_fine(self):
        self.assertIsNone(self.problem())

    def test_an_old_price_is_stale(self):
        self.assertEqual(self.problem(price_time=minutes_ago(25))[0], 'stale')

    def test_the_exchange_delay_is_allowed_for(self):
        self.assertIsNone(self.problem(price_time=minutes_ago(22), delay_min=15))
        self.assertEqual(self.problem(price_time=minutes_ago(30), delay_min=15)[0], 'stale')

    def test_no_price_time_is_stale(self):
        self.assertEqual(self.problem(price_time=None)[0], 'stale')

    def test_a_price_from_before_the_story_is_stale(self):
        # The AT.L case: a takeover bid published after the last trade seen.
        published = (NOW - datetime.timedelta(minutes=1)).isoformat()
        rule, reason = self.problem(price_time=minutes_ago(5), published_at=published)
        self.assertEqual(rule, 'stale')
        self.assertIn("hasn't seen the news", reason)
        self.assertIsNone(self.problem(price_time=minutes_ago(1),
                                       published_at=(NOW - datetime.timedelta(minutes=3)).isoformat()))

    def test_a_wide_spread_is_refused(self):
        self.fill['spread_pct'] = 0.03
        self.assertEqual(self.problem()[0], 'spread')


class LedgerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.paper = PaperTrader(filename=os.path.join(tmp.name, 'paper_trades.json'),
                                 cost_pct=0.001)

    def test_spreads_are_recorded_and_reported(self):
        watch = {'id': 'w1', 'ticker': 'AAA', 'company': 'A', 'direction': 'LONG',
                 'entry_price': 101.0, 'entry_last': 100.0, 'spread_pct': 0.02,
                 'spread_source': 'estimate', 'opened_at': NOW.isoformat()}
        self.paper.open_trade(watch)
        exit_ = fills.exit_fill("LONG", 110.0, {}, True, fallback_spread=0.02)
        t = self.paper.close_trade(dict(watch, closed_at=NOW.isoformat()), exit_['price'],
                                   fill=exit_)
        self.assertEqual(t['exit_last'], 110.0)
        self.assertAlmostEqual(t['exit_price'], 108.9)
        self.assertAlmostEqual(t['gross_pct'], (108.9 - 101.0) / 101.0, places=5)
        stats = self.paper.stats()
        self.assertAlmostEqual(stats['avg_spread'], 0.02)
        self.assertEqual(stats['spread_trades'], 1)

    def test_an_open_position_is_worth_what_selling_it_would_get(self):
        watch = {'id': 'w1', 'ticker': 'AAA', 'company': 'A', 'direction': 'LONG',
                 'entry_price': 101.0, 'spread_pct': 0.02, 'opened_at': NOW.isoformat()}
        self.paper.open_trade(watch)
        row = self.paper.live_positions({'AAA': 101.0})[0]
        self.assertAlmostEqual(row['unrealised_pct'], -0.001 - 0.01)

    def test_the_account_opens_a_position_down_by_its_spread(self):
        t = {'watch_id': 'w1', 'ticker': 'AAA', 'direction': 'LONG', 'entry_price': 101.0,
             'opened_at': NOW.isoformat(), 'stop_pct': 0.04, 'confidence': 80,
             'impact': 'HIGH', 'status': 'OPEN', 'spread_pct': 0.02}
        acct = paper_account.replay([t], 0.001, now=NOW, curve=False)
        self.assertAlmostEqual(acct['unrealised_pnl'] / acct['invested'], -0.021, places=4)


class DecideTradeFillTest(unittest.TestCase):
    """main._decide_trade opens at the fill, and refuses - following it as a
    skipped trade - a stale price or a wide spread."""

    def setUp(self):
        import main
        from watch_manager import WatchManager
        patcher = mock.patch.multiple(config, PAPER_BUDGET=1000.0, PAPER_RISK_PCT=0.01,
                                      PAPER_MAX_POSITION_PCT=0.25, ENTRY_MAX_PRICE_AGE_MIN=10,
                                      ENTRY_MAX_SPREAD_PCT=0.015, ALLOW_SHORTS=True,
                                      MAX_OPEN_POSITIONS=20)
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        b = main.StockAppBackend.__new__(main.StockAppBackend)
        b.status_callback = b.log_callback = None
        b.paper = PaperTrader(filename=os.path.join(tmp.name, 'paper_trades.json'), cost_pct=0.001)
        b.watch_mgr = WatchManager(filename=os.path.join(tmp.name, 'watches.json'))
        b.shadows = mock.Mock()
        self.b = b
        self.main = main
        self.ctx = {'price': 100.0, 'atr_pct': 0.02, 'change_since_close_pct': 0.0,
                    'market_price': 500.0, 'market_since_close_pct': 0.0,
                    'price_time': time.time() - 30}
        self.quote = {'bid': 99.9, 'ask': 100.1, 'market_cap': 5e10, 'currency': 'USD',
                      'delay_min': 0}

    def decide(self, market_open=True):
        with mock.patch.object(self.main.price_lookup, 'fetch_context',
                               return_value=self.ctx) as fetch_context, \
             mock.patch.object(self.main.price_lookup, 'fetch_quote', return_value=self.quote), \
             mock.patch.object(self.main.strategy, 'market_open', return_value=market_open):
            self.fetched_context = fetch_context
            return self.b._decide_trade("AAA", "Acme", "LONG", "HIGH", "RALLY", 0.08, 80,
                                        {'horizon': 'DAYS'}, {'source': 'Custom/X'}, "u", "t")

    def test_a_long_opens_at_the_ask(self):
        d = self.decide()
        self.assertTrue(d['opened'])
        self.assertEqual(d['entry_price'], 100.1)
        trade = self.b.paper.trades[0]
        self.assertEqual(trade['entry_price'], 100.1)
        self.assertEqual(trade['entry_last'], 100.0)
        self.assertEqual(trade['spread_source'], 'quote')

    def test_nothing_opens_while_the_market_is_shut(self):
        d = self.decide(market_open=False)
        self.assertFalse(d['opened'])
        self.assertIn("market is closed", d['reason'])
        # Not priced, and not followed as a skipped trade either.
        self.fetched_context.assert_not_called()
        self.b.shadows.track.assert_not_called()
        self.assertEqual(self.b.paper.trades, [])

    def test_an_unmodelled_exchange_still_trades(self):
        self.assertTrue(self.decide(market_open=None)['opened'])

    def test_a_stale_price_is_followed_not_traded(self):
        self.ctx['price_time'] = time.time() - 3600
        d = self.decide()
        self.assertFalse(d['opened'])
        self.assertIn("min old", d['reason'])
        self.assertEqual(self.b.shadows.track.call_args[0][4]['rule'], 'stale')

    def test_a_wide_spread_is_followed_not_traded(self):
        # A $30M company: a 6% quote is believable for it, and too wide.
        self.quote = {'bid': 97.0, 'ask': 103.0, 'market_cap': 3e7, 'currency': 'USD',
                      'delay_min': 0}
        d = self.decide()
        self.assertFalse(d['opened'])
        self.assertEqual(self.b.shadows.track.call_args[0][4]['rule'], 'spread')
        # Followed from the price it would have paid, spread and all.
        self.assertEqual(self.b.shadows.track.call_args[0][3], 103.0)
        self.assertEqual(self.b.shadows.track.call_args[1]['spread_pct'], 0.06)


if __name__ == "__main__":
    unittest.main()
