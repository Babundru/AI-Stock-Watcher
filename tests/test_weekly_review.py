"""Tests for the weekly reviewer's prompt: each strategy version is shown on
its own, with figures that don't hide behind a few outsized winners. No
network.

Run from the project root:  py -m unittest discover -s tests
"""

import datetime
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategy  # noqa: E402
import weekly_review  # noqa: E402


def trade(version, net, closed_days_ago=1, **extra):
    closed = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=closed_days_ago)
    t = {'strategy': version, 'net_pct': net, 'direction': 'LONG', 'event_type': 'EARNINGS',
         'reason': 'stop_loss', 'closed_at': closed.isoformat()}
    t.update(extra)
    return t


class VersionSectionTests(unittest.TestCase):
    def setUp(self):
        old = [trade('v7', 0.5, 30)] + [trade('v7', -0.01, 30) for _ in range(5)]
        new = [trade('v9', -0.03) for _ in range(4)]
        self.paper = mock.Mock(closed=mock.Mock(return_value=old + new))
        self.shadows = mock.Mock(closed=mock.Mock(return_value=[
            {'strategy': 'v9', 'skip_rule': 'reported', 'net_pct': -0.02}]))

    def test_the_newest_version_comes_first_and_alone(self):
        with mock.patch.object(strategy, 'STRATEGY_VERSION', 'v9'):
            text = weekly_review.version_sections(self.paper, self.shadows)
        first = text.splitlines()[0]
        self.assertTrue(first.startswith('v9 (running now): 4 trades, average -3.00%'))
        self.assertIn('skipped, by rule: reported 1 -2.00%', text)
        self.assertLess(text.index('v9'), text.index('v7'))

    def test_outliers_are_called_out(self):
        text = weekly_review._robust([0.5, -0.01, -0.01, -0.01, -0.01, -0.01])
        self.assertIn('median -1.00%', text)
        self.assertIn('without the 3 best -1.00%', text)

    def test_recent_window(self):
        text = weekly_review.recent_section(self.paper)
        self.assertTrue(text.startswith('4 trades'))

    def test_the_prompt_carries_both_sections(self):
        paper = mock.Mock(closed=self.paper.closed, stats=mock.Mock(return_value=None))
        shadows = mock.Mock(closed=self.shadows.closed, summary=mock.Mock(return_value=None))
        prompt = weekly_review.build_prompt(paper, shadows, {})
        self.assertIn('--- BY STRATEGY VERSION', prompt)
        self.assertIn('--- LAST 14 DAYS', prompt)


if __name__ == '__main__':
    unittest.main()
