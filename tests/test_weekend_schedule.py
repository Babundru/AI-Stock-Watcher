"""Tests for the weekend check interval: which scans count as weekend ones,
that each scan's window reaches back past the previous scan so a longer wait
skips nothing, and that the long weekend wait still gives Reddit its request
a minute. No network, and nothing really sleeps: the wait runs on a fake clock.

Run from the project root:  py -m unittest discover -s tests
"""

import datetime
import email.utils
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import feedparser  # noqa: E402

import config  # noqa: E402
import main  # noqa: E402
from news_collector import NewsCollector  # noqa: E402
from source_manager import SourceManager  # noqa: E402

UTC = datetime.timezone.utc


def _utc(*args):
    return datetime.datetime(*args, tzinfo=UTC)


class IsWeekendTest(unittest.TestCase):
    def test_follows_the_new_york_calendar(self):
        # 2026-09-12 is a Saturday; New York is UTC-4 in September.
        cases = [
            (_utc(2026, 9, 12, 3, 59), False),  # Fri 23:59 in New York
            (_utc(2026, 9, 12, 4, 0), True),    # Sat 00:00
            (_utc(2026, 9, 14, 3, 59), True),   # Sun 23:59
            (_utc(2026, 9, 14, 4, 0), False),   # Mon 00:00
        ]
        for when, expected in cases:
            with self.subTest(when=when):
                self.assertEqual(main.is_weekend(when), expected)


def _feed(now, *ages_min):
    """An RSS feed with one story per age (minutes before `now`)."""
    items = "".join(
        f"<item><title>{age}</title><link>https://example.com/{age}</link>"
        f"<pubDate>{email.utils.format_datetime(now - datetime.timedelta(minutes=age))}</pubDate></item>"
        for age in ages_min)
    return feedparser.parse(f'<?xml version="1.0"?><rss version="2.0"><channel>'
                            f'<title>Wire</title>{items}</channel></rss>')


class WindowTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.collector = NewsCollector(source_mgr=SourceManager(os.path.join(tmp.name, "sources.json")))
        self.now = datetime.datetime.now(UTC)

    def kept(self, *ages_min):
        articles = self.collector._feed_to_articles(_feed(self.now, *ages_min), "Wire", scrape=False)
        return [int(a["title"]) for a in articles]

    def test_first_scan_looks_back_lookback_minutes(self):
        self.assertEqual(config.LOOKBACK_MINUTES, 30)
        self.assertEqual(self.kept(10, 28, 32, 50), [10, 28])

    def test_window_reaches_back_past_the_previous_scan(self):
        # The previous scan started 25 minutes ago - a weekend wait. A story
        # published 50 minutes ago that reached its feed just after that
        # scan is still fresh; one that is older than the previous scan's
        # own window was that scan's to catch.
        previous_scan = self.now - datetime.timedelta(minutes=25)
        self.collector.window_start = previous_scan - datetime.timedelta(minutes=config.LOOKBACK_MINUTES)
        self.assertEqual(self.kept(10, 50, 54, 56, 90), [10, 50, 54])

    def test_window_is_never_narrower_than_lookback_minutes(self):
        self.collector.window_start = self.now - datetime.timedelta(minutes=5)
        self.assertEqual(self.kept(10, 28, 32), [10, 28])


class WaitTest(unittest.TestCase):
    """StockAppBackend._wait on a fake clock that time.sleep advances."""

    def setUp(self):
        self.clock = 1_000_000.0

        def sleep(seconds):
            self.clock += seconds

        for name, fake in (("sleep", sleep), ("time", lambda: self.clock)):
            patcher = mock.patch.object(main.time, name, side_effect=fake)
            patcher.start()
            self.addCleanup(patcher.stop)

        # Just the parts of the backend _wait touches - no data files, no engine.
        self.backend = main.StockAppBackend.__new__(main.StockAppBackend)
        self.backend.running = True
        self.backend._generation = 1
        self.backend.log_callback = None
        self.backend.status_callback = None
        self.backend.collector = mock.Mock()
        self.backend.collector.fetch_reddit_posts.return_value = []
        self.backend.notifier = mock.Mock()
        self.backend.notifier.is_market_open.return_value = False
        self.backend._process_article = mock.Mock()

    def test_weekday_wait_polls_nothing_extra(self):
        start = self.clock
        self.backend._wait(config.CHECK_INTERVAL, 1)
        self.assertEqual(self.clock - start, 60)
        self.backend.collector.fetch_reddit_posts.assert_not_called()

    def test_weekend_wait_polls_reddit_every_minute(self):
        start = self.clock
        self.backend._wait(config.WEEKEND_CHECK_INTERVAL, 1)
        self.assertEqual(self.clock - start, 25 * 60)
        # After minutes 1..24; the 25th is the full scan's.
        self.assertEqual(self.backend.collector.fetch_reddit_posts.call_count, 24)

    def test_reddit_posts_are_analysed_as_they_become_ready(self):
        post = {"url": "https://www.reddit.com/r/stocks/comments/abc/x/", "title": "x"}
        self.backend.collector.fetch_reddit_posts.side_effect = [[], [post]] + [[]] * 30
        self.backend._wait(config.WEEKEND_CHECK_INTERVAL, 1)
        self.backend._process_article.assert_called_once_with(
            "Custom Source News", post, False, is_discovery=True)

    def test_countdown_runs_while_waiting_only(self):
        seen = []
        self.backend.collector.fetch_reddit_posts.side_effect = (
            lambda: seen.append(self.backend.next_scan_countdown()) or [])
        self.backend._wait(config.WEEKEND_CHECK_INTERVAL, 1)
        self.assertEqual(seen[0], (24 * 60, 25 * 60))
        self.assertEqual(seen[-1], (60, 25 * 60))
        self.assertIsNone(self.backend.next_scan_countdown())

    def test_stop_ends_the_wait(self):
        def stop_on_third_poll():
            if self.backend.collector.fetch_reddit_posts.call_count == 3:
                self.backend.running = False
            return []
        self.backend.collector.fetch_reddit_posts.side_effect = stop_on_third_poll
        start = self.clock
        self.backend._wait(config.WEEKEND_CHECK_INTERVAL, 1)
        self.assertEqual(self.clock - start, 3 * 60)


if __name__ == "__main__":
    unittest.main()
