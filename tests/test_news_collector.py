"""Tests for what the collector takes to be an article's text, and for which
feeds and articles a scan keeps. No network: the session is replaced by a
fake one and the feeds below are plain dicts.

Run from the project root:  py -m unittest discover -s tests
"""

import datetime
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from news_collector import (MAX_ARTICLES_PER_SCAN, NewsCollector,  # noqa: E402
                            _interleave_newest, _is_consent_wall)
from source_manager import SourceManager  # noqa: E402


class _Response:
    def __init__(self, url, body):
        self.url = url
        self._body = body

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        yield self._body

    def close(self):
        pass


class _Session:
    """Answers every GET as if it had been redirected to `final_url`."""

    def __init__(self, final_url, body):
        self.final_url = final_url
        self.body = body

    def get(self, url, **kwargs):
        return _Response(self.final_url, self.body)


class ConsentWallTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.collector = NewsCollector(source_mgr=SourceManager(os.path.join(tmp.name, 'sources.json')))

    def test_consent_hosts(self):
        self.assertTrue(_is_consent_wall("https://consent.yahoo.com/v2/collectConsent?sessionId=x"))
        self.assertTrue(_is_consent_wall("https://guce.yahoo.com/consent?done=https%3A%2F%2Ffinance.yahoo.com"))
        self.assertFalse(_is_consent_wall("https://finance.yahoo.com/news/some-story.html"))
        self.assertFalse(_is_consent_wall("https://www.cnbc.com/2026/09/14/consent-decree.html"))
        self.assertFalse(_is_consent_wall(None))

    def test_a_consent_page_is_not_the_article(self):
        # Yahoo from an EU address: every article redirects to the same
        # cookie notice, which used to be analysed as the story.
        self.collector.session = _Session("https://consent.yahoo.com/v2/collectConsent?sessionId=x",
                                          b"<p>Your privacy is important to us. We use cookies.</p>")
        self.assertIsNone(self.collector.scrape_article("https://finance.yahoo.com/news/story.html"))

    def test_an_ordinary_page_is(self):
        self.collector.session = _Session("https://finance.yahoo.com/news/story.html",
                                          b"<p>Acme beat estimates.</p><p>Shares rose.</p>")
        self.assertEqual(self.collector.scrape_article("https://finance.yahoo.com/news/story.html"),
                         "Acme beat estimates. Shares rose.")


class FeedShareTests(unittest.TestCase):
    """Which articles survive the per-scan cap when several feeds compete."""

    @staticmethod
    def _feed(prefix, count, newest_minutes_ago):
        base = datetime.datetime(2026, 9, 10, 12, tzinfo=datetime.timezone.utc)
        return [{'title': f"{prefix}{i}",
                 '_pub_dt': base - datetime.timedelta(minutes=newest_minutes_ago + i)}
                for i in range(count)]

    def titles(self, groups, limit):
        return [a['title'] for a in _interleave_newest(groups, limit)]

    def test_every_feed_gets_a_turn_before_any_gets_seconds(self):
        # The busy feed's stories are all newer than the quiet one's, which
        # under a plain newest-first cut would take the whole quota.
        busy = self._feed("us", 10, 1)
        quiet = self._feed("eu", 3, 30)
        picked = self.titles([busy, quiet], 4)
        self.assertEqual(sorted(picked), ["eu0", "eu1", "us0", "us1"])

    def test_a_short_feed_does_not_waste_the_quota(self):
        picked = self.titles([self._feed("us", 10, 1), self._feed("eu", 1, 30)], 5)
        self.assertEqual(sorted(picked), ["eu0", "us0", "us1", "us2", "us3"])

    def test_newest_first_overall(self):
        picked = _interleave_newest([self._feed("us", 3, 1), self._feed("eu", 3, 2)], 6)
        self.assertEqual([a['title'] for a in picked],
                         ["us0", "eu0", "us1", "eu1", "us2", "eu2"])

    def test_empty_feeds_are_ignored(self):
        self.assertEqual(self.titles([[], self._feed("eu", 2, 1), []], 5), ["eu0", "eu1"])
        self.assertEqual(_interleave_newest([], MAX_ARTICLES_PER_SCAN), [])


class BuiltInFeedTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.collector = NewsCollector(source_mgr=SourceManager(os.path.join(tmp.name, 'sources.json')))
        self._saved = config.SCAN_EUROPE
        self.addCleanup(setattr, config, 'SCAN_EUROPE', self._saved)

    def test_europe_is_polled_when_it_is_on(self):
        config.SCAN_EUROPE = True
        names = [name for _, name in self.collector.MARKET_RSS_FEEDS]
        self.assertIn('CNBC Top News', names)
        self.assertIn('FT Companies', names)

    def test_europe_is_skipped_when_it_is_off(self):
        config.SCAN_EUROPE = False
        names = [name for _, name in self.collector.MARKET_RSS_FEEDS]
        self.assertIn('CNBC Top News', names)
        self.assertNotIn('FT Companies', names)


if __name__ == "__main__":
    unittest.main()
