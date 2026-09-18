"""Tests for what the collector takes to be an article's text. No network:
the session is replaced by a fake one.

Run from the project root:  py -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news_collector import NewsCollector, _is_consent_wall  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
