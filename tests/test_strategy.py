"""Tests for the entry rules (strategy.plan_trade), source trust and the
skipped-trade book. No network: price context is given as plain dicts.

Run from the project root:  py -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import strategy  # noqa: E402
from shadow_trades import ShadowBook  # noqa: E402
from source_manager import (OPINION, REPORTING, SourceManager, article_trust,  # noqa: E402
                            default_trust, source_trust)


class RulesMixin:
    """Pins the strategy settings (data/settings.json may override them) and
    restores them afterwards."""

    SETTINGS = dict(STOP_LOSS_PCT=0.08, STOP_ATR_MULT=1.5, STOP_MIN_PCT=0.02, MIN_REWARD_RISK=1.2,
                    EXHAUSTED_ATR_MULT=3.0, AGAINST_NEWS_ATR_MULT=0.5, CONFIRM_ATR_MULT=0.5,
                    MAX_SHADOW_POSITIONS=20, LET_WINNERS_RUN=True, BREAKEVEN_AT=0.5)

    def setUp(self):
        self._saved = {k: getattr(config, k) for k in self.SETTINGS}
        for k, v in self.SETTINGS.items():
            setattr(config, k, v)

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(config, k, v)


def ctx(close=None, publish=None, atr=0.02, in_session=True, m_close=None, m_publish=None):
    c = {'atr_pct': atr, 'published_in_session': in_session}
    for key, value in (('change_since_close_pct', close), ('change_since_publish_pct', publish),
                       ('market_since_close_pct', m_close), ('market_since_publish_pct', m_publish)):
        if value is not None:
            c[key] = value
    return c


class NewsMoveTests(RulesMixin, unittest.TestCase):
    def test_session_news_is_measured_from_publication_net_of_the_market(self):
        move = strategy.news_move('LONG', ctx(close=0.05, publish=0.03, m_close=0.02, m_publish=0.01))
        self.assertAlmostEqual(move['pct'], 0.02)
        self.assertEqual(move['from'], 'publish')
        self.assertEqual(move['market_pct'], 0.01)

    def test_news_from_outside_market_hours_is_measured_from_the_close(self):
        move = strategy.news_move('LONG', ctx(close=0.05, publish=0.01, m_close=0.01, in_session=False))
        self.assertAlmostEqual(move['pct'], 0.04)
        self.assertEqual(move['from'], 'close')

    def test_unknown_publication_time_falls_back_to_the_close(self):
        move = strategy.news_move('LONG', ctx(close=0.03, publish=0.01, in_session=None))
        self.assertEqual(move['from'], 'close')

    def test_short_is_signed_for_the_trade(self):
        self.assertAlmostEqual(strategy.news_move('SHORT', ctx(publish=-0.03))['pct'], 0.03)

    def test_no_market_price_subtracts_nothing(self):
        self.assertAlmostEqual(strategy.news_move('LONG', ctx(publish=0.03))['pct'], 0.03)

    def test_no_price_data(self):
        self.assertIsNone(strategy.news_move('LONG', {}))
        self.assertIsNone(strategy.news_move('LONG', None))


class PlanTradeTests(RulesMixin, unittest.TestCase):
    """A 6% story on a stock with a 2% daily range: stop 3%, exhausted past
    6%, against past -1%, confirmed from +1%."""

    def plan(self, moved, atr=0.02, expected=0.06, direction='LONG', **kw):
        return strategy.plan_trade(direction, 'HIGH', expected, ctx(publish=moved, atr=atr), **kw)

    def test_a_stock_already_rising_on_the_news_still_trades(self):
        # v2 called this priced in: 4% is more than half of 6%.
        plan = self.plan(0.04)
        self.assertTrue(plan['ok'], plan['reason'])
        # The target never drops below the reward the entry rule asks for.
        self.assertAlmostEqual(plan['stop_pct'], 0.03)
        self.assertAlmostEqual(plan['target_pct'], 1.2 * 0.03)

    def test_untouched_stock_aims_for_the_whole_expected_move(self):
        self.assertAlmostEqual(self.plan(0.0)['target_pct'], 0.06)

    def test_a_spent_move_is_skipped(self):
        self.assertEqual(self.plan(0.061)['rule'], 'exhausted')
        self.assertTrue(self.plan(0.059)['ok'])

    def test_a_big_story_on_a_calm_stock_is_not_spent_after_three_ranges(self):
        # 1% daily range: 3.5% is 3.5 ranges, but only a third of a 10% story.
        self.assertTrue(self.plan(0.035, atr=0.01, expected=0.10)['ok'])
        self.assertEqual(self.plan(0.101, atr=0.01, expected=0.10)['rule'], 'exhausted')

    def test_a_small_story_on_a_jumpy_stock_needs_three_ranges(self):
        # 4% range, 8% story: past the expected move, but only 2.25 ranges.
        self.assertTrue(self.plan(0.09, atr=0.04, expected=0.08)['ok'])
        self.assertEqual(self.plan(0.121, atr=0.04, expected=0.08)['rule'], 'exhausted')

    def test_a_move_against_the_news_is_skipped(self):
        self.assertEqual(self.plan(-0.011)['rule'], 'against')
        self.assertTrue(self.plan(-0.009)['ok'])

    def test_opinion_sources_need_the_price_to_confirm(self):
        self.assertEqual(self.plan(0.005, needs_confirmation=True)['rule'], 'unconfirmed')
        self.assertTrue(self.plan(0.011, needs_confirmation=True)['ok'])
        no_data = strategy.plan_trade('LONG', 'HIGH', 0.10, {}, needs_confirmation=True)
        self.assertEqual(no_data['rule'], 'unconfirmed')

    def test_reporting_trades_without_price_data(self):
        # No ATR means the maximum 8% stop, so the story has to be big.
        self.assertTrue(strategy.plan_trade('LONG', 'HIGH', 0.10, {})['ok'])

    def test_capped_news_is_skipped(self):
        self.assertEqual(self.plan(0.0, capped=True)['rule'], 'capped')

    def test_story_too_small_for_the_stock(self):
        # 3% daily range: a 4.5% stop, so the story needs 5.4%.
        self.assertEqual(self.plan(0.0, atr=0.03, expected=0.05)['rule'], 'reward_risk')

    def test_short(self):
        self.assertTrue(self.plan(-0.03, expected=0.10, direction='SHORT')['ok'])
        self.assertEqual(self.plan(0.02, direction='SHORT')['rule'], 'against')

    def test_market_move_is_netted_out(self):
        # Up 7% since publication, but the market is up 2%: 5% is the story's.
        plan = strategy.plan_trade('LONG', 'HIGH', 0.06, ctx(publish=0.07, m_publish=0.02))
        self.assertTrue(plan['ok'], plan['reason'])
        self.assertAlmostEqual(plan['already_moved_pct'], 0.05)

    def test_a_refused_signal_is_still_sized(self):
        plan = self.plan(0.07)
        self.assertFalse(plan['ok'])
        self.assertIn('stop_pct', plan)
        self.assertIn('target_pct', plan)


class TrustTests(unittest.TestCase):
    def test_defaults_by_type(self):
        self.assertEqual(default_trust('twitter'), OPINION)
        self.assertEqual(default_trust('reddit'), OPINION)
        self.assertEqual(default_trust('rss'), REPORTING)
        self.assertEqual(default_trust('webpage'), REPORTING)
        # A source saved before the setting existed.
        self.assertEqual(source_trust({'type': 'twitter'}), OPINION)

    def test_article_trust(self):
        self.assertEqual(article_trust({'source': 'Custom/CNBC', 'trust': OPINION}), OPINION)
        self.assertEqual(article_trust({'source': 'Reddit/r/stocks'}), OPINION)
        self.assertEqual(article_trust({'source': 'Twitter/someone'}), OPINION)
        self.assertEqual(article_trust({'source': 'Custom/CNBC Top News'}), REPORTING)

    def test_source_manager_stores_and_changes_trust(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SourceManager(os.path.join(tmp, 'sources.json'))
            tweets = mgr.add_source("Someone", "https://x.com/someone")
            wire = mgr.add_source("Wire", "https://example.com/feed.xml", "rss")
            by_id = {s['id']: s for s in mgr.get_sources()}
            self.assertEqual(by_id[tweets]['trust'], OPINION)
            self.assertEqual(by_id[wire]['trust'], REPORTING)

            self.assertTrue(mgr.set_trust(tweets, REPORTING))
            self.assertEqual(source_trust(SourceManager(os.path.join(tmp, 'sources.json'))
                                          .get_sources()[-2]), REPORTING)
            with self.assertRaises(ValueError):
                mgr.set_trust(wire, 'gospel')
            with self.assertRaises(ValueError):
                mgr.add_source("Bad", "https://example.com/", trust='gospel')
            self.assertFalse(mgr.set_trust('no-such-id', OPINION))


class ShadowBookTests(RulesMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'shadow.json')
        self.book = ShadowBook(self.path, cost_pct=0.002)
        # Exhausted: 7% on a 6% story with a 2% range. Stop 3%, target 3.6%.
        self.refused = strategy.plan_trade('LONG', 'HIGH', 0.06, ctx(publish=0.07))

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()

    def track(self, ticker='NVDA', entry=100.0):
        return self.book.track(ticker, 'Nvidia', 'LONG', entry, self.refused,
                               horizon='DAYS', benchmark_price=500.0)

    def test_follows_a_refused_signal_once_per_ticker(self):
        record = self.track()
        self.assertEqual(record['skip_rule'], 'exhausted')
        self.assertEqual(record['stop_loss_price'], 97.0)
        self.assertIsNone(self.track())
        self.assertIsNone(self.book.track('AMD', 'AMD', 'LONG', None, self.refused))

    def test_open_count_is_capped(self):
        config.MAX_SHADOW_POSITIONS = 2
        self.assertTrue(self.track('A'))
        self.assertTrue(self.track('B'))
        self.assertIsNone(self.track('C'))

    def test_exits_are_the_real_ones(self):
        record = self.track()
        self.assertEqual(strategy.update_exit(record, 96.9, 0.002), ('stop_loss', False))

    def test_close_and_summary(self):
        record = self.track()
        self.book.mark_price(record, 104.0)
        self.book.close(record, 'trailing_stop', 103.0, 505.0)
        self.book.save()

        self.assertAlmostEqual(record['net_pct'], 0.028)
        self.assertAlmostEqual(record['alpha_pct'], 0.018)
        self.assertAlmostEqual(record['mfe_pct'], 0.04)
        row = self.book.summary()['by_rule']['exhausted']
        self.assertEqual(row['trades'], 1)
        self.assertEqual(row['win_rate'], 1.0)
        self.assertAlmostEqual(row['expectancy'], 0.028)

        again = ShadowBook(self.path)
        self.assertEqual(len(again.closed()), 1)
        self.assertEqual(again.open_records(), [])


class SessionTests(unittest.TestCase):
    def test_regular_session(self):
        try:
            from price_lookup import _in_regular_session
            import pandas  # noqa: F401
        except ImportError as e:
            self.skipTest(f"pandas not installed here: {e}")
        self.assertTrue(_in_regular_session("2026-09-10T14:00:00+00:00"))   # Thu 10:00 ET
        self.assertTrue(_in_regular_session("2026-09-10T14:00:00"))         # naive = UTC
        self.assertFalse(_in_regular_session("2026-09-10T21:00:00+00:00"))  # Thu 17:00 ET
        self.assertFalse(_in_regular_session("2026-09-10T13:00:00+00:00"))  # Thu 09:00 ET
        self.assertFalse(_in_regular_session("2026-09-12T15:00:00+00:00"))  # Saturday
        self.assertIsNone(_in_regular_session("not a time"))


if __name__ == "__main__":
    unittest.main()
