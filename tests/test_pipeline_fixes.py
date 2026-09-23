"""Tests for the scan loop's retry bookkeeping and the collector's filters:
outages don't write articles off, Reddit posts survive a failing engine, one
story isn't analysed twice. No network, no data files.

Run from the project root:  py -m unittest discover -s tests
"""

import collections
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import main  # noqa: E402
from llm_prompts import AnalysisUnavailable  # noqa: E402
from news_collector import clean_url, is_noise_headline, title_key  # noqa: E402
from source_manager import SourceManager  # noqa: E402


def _article(n, reddit=False):
    if reddit:
        return {"url": f"https://www.reddit.com/r/stocks/comments/p{n}/x/",
                "title": f"Reddit post number {n} about some company",
                "source": "Reddit/r/stocks"}
    return {"url": f"https://news.example.com/story-{n}",
            "title": f"Company {n} announces something quite new today"}


class _Engine:
    """Fails while `down`, and on any URL in `bad`; otherwise finds the
    article irrelevant (None), which ends its processing early."""

    def __init__(self):
        self.down = False
        self.bad = set()
        self.calls = []

    def analyze_article(self, company, article, open_markets, portfolio_tickers=None):
        self.calls.append(article["url"])
        if self.down or article["url"] in self.bad:
            raise AnalysisUnavailable("boom")
        return None


class PipelineTest(unittest.TestCase):
    def setUp(self):
        b = main.StockAppBackend.__new__(main.StockAppBackend)
        b.running = True
        b._generation = 1
        b.log_callback = None
        b.status_callback = None
        b.alert_callback = None
        b.stats = {'scanned': 0, 'alerts': 0, 'skipped': 0}
        b.max_stored_urls = 500
        b.processed_urls = collections.deque(maxlen=500)
        b.processed_set = set()
        b.urls_lock = threading.Lock()
        b.articles_since_save = 0
        b._save_processed_urls = mock.Mock()
        b._save_stats = mock.Mock()
        b.portfolio_mgr = mock.Mock()
        b.portfolio_mgr.get_portfolio.return_value = {}
        b.collector = mock.Mock()
        b.collector.fetch_reddit_posts.return_value = []
        b.notifier = mock.Mock()
        b.notifier.open_markets.return_value = []
        b.analyzer = self.engine = _Engine()
        b._init_pipeline_state()
        self.b = b

    def scan(self, articles):
        self.b._run_pass([("General Market News", a, True) for a in articles], 1, [])

    def test_an_outage_charges_no_attempt(self):
        # Longer than MAX_ANALYSIS_ATTEMPTS scans of outage: the article
        # first in line used to be written off after three.
        first = _article(1)
        self.engine.down = True
        for _ in range(main.MAX_ANALYSIS_ATTEMPTS + 2):
            self.scan([first, _article(2)])
        self.assertNotIn(first["url"], self.b.processed_set)
        self.assertNotIn(first["url"], self.b._failed_attempts)
        self.engine.down = False
        self.scan([first, _article(2)])
        self.assertIn(first["url"], self.b.processed_set)

    def test_an_outage_stops_the_pass_after_two_failures(self):
        self.engine.down = True
        self.scan([_article(n) for n in range(5)])
        self.assertEqual(len(self.engine.calls), main.FAILURES_BEFORE_ENGINE_DOWN)

    def test_an_unreadable_article_is_given_up_while_others_go_through(self):
        bad = _article(1)
        self.engine.bad = {bad["url"]}
        for n in range(main.MAX_ANALYSIS_ATTEMPTS):
            self.scan([bad, _article(10 + n)])
        self.assertIn(bad["url"], self.b.processed_set)

    def test_a_failed_article_goes_behind_fresh_ones(self):
        bad = _article(1)
        self.engine.bad = {bad["url"]}
        self.scan([bad, _article(2)])
        self.engine.calls.clear()
        self.scan([bad, _article(3)])
        self.assertEqual(self.engine.calls, [_article(3)["url"], bad["url"]])

    def test_an_outage_gives_up_eventually(self):
        bad = _article(1)
        self.engine.down = True
        for _ in range(main.MAX_OUTAGE_STRIKES):
            self.scan([bad])
        self.assertIn(bad["url"], self.b.processed_set)

    def test_a_reddit_post_survives_a_failing_engine(self):
        post = _article(1, reddit=True)
        self.engine.down = True
        self.b.collector.fetch_reddit_posts.return_value = [post]
        self.b._poll_reddit(1)
        self.assertIn(post["url"], self.b._reddit_backlog)
        # The poller hands a post out once; the backlog brings it back.
        self.engine.down = False
        self.b.collector.fetch_reddit_posts.return_value = []
        self.b._poll_reddit(1)
        self.assertIn(post["url"], self.b.processed_set)
        self.assertFalse(self.b._reddit_backlog)

    def test_a_reddit_post_held_back_by_an_earlier_failure_is_kept(self):
        post = _article(3, reddit=True)
        self.engine.down = True
        self.scan([_article(1), _article(2), post])
        self.assertIn(post["url"], self.b._reddit_backlog)

    def test_the_same_headline_under_another_url_is_analysed_once(self):
        a = _article(1)
        b = dict(a, url="https://uk.news.example.com/story-1")
        self.scan([a, b])
        self.assertEqual(self.engine.calls, [a["url"]])
        self.assertIn(b["url"], self.b.processed_set)


class CollectorFilterTest(unittest.TestCase):
    def test_tracking_parameters_are_stripped(self):
        self.assertEqual(clean_url("https://seekingalpha.com/article/1-x?source=feed_all_articles"),
                         "https://seekingalpha.com/article/1-x")
        self.assertEqual(clean_url("https://a.com/s?id=5&utm_source=x&mod=rss#top"),
                         "https://a.com/s?id=5")
        self.assertEqual(clean_url("https://a.com/s"), "https://a.com/s")

    def test_noise_headlines(self):
        self.assertTrue(is_noise_headline("Avio S.p.A. Q2 2026 Earnings Call Transcript"))
        self.assertTrue(is_noise_headline("Flywire presents at Goldman Sachs Communacopia Conference 2026"))
        self.assertTrue(is_noise_headline("3 Dividend Stocks to Buy Right Now"))
        self.assertTrue(is_noise_headline("Bureau Veritas SA (BVVBY) Analyst/Investor Day Transcript"))
        self.assertTrue(is_noise_headline("BlackRock Low Duration Bond Fund Q2 2026 Commentary"))
        self.assertFalse(is_noise_headline("Transcript leak shows CEO discussed a sale of the company"))
        self.assertTrue(is_noise_headline("I'm 60 with $2M - should I retire?"))
        self.assertFalse(is_noise_headline("Pfizer shares jump after FDA approves obesity drug"))
        self.assertFalse(is_noise_headline("Nvidia to buy chip designer for $4 billion"))
        self.assertTrue(is_noise_headline("Okta at Oktane 2026 Investor Summit",
                                          "https://www.investing.com/news/transcripts/okta-at-oktane"))

    def test_short_headlines_are_not_matched_as_one_story(self):
        self.assertIsNone(title_key("Market update"))
        self.assertEqual(title_key("Apple beats, raises: guidance UP"), "apple beats raises guidance up")


class TwitterUrlTest(unittest.TestCase):
    def setUp(self):
        self.mgr = SourceManager.__new__(SourceManager)

    def test_only_twitter_hosts_count(self):
        self.assertFalse(self.mgr._is_twitter_url("https://www.vox.com/business"))
        self.assertFalse(self.mgr._is_twitter_url("https://www.fedex.com/news/"))
        self.assertTrue(self.mgr._is_twitter_url("https://x.com/someone"))
        self.assertTrue(self.mgr._is_twitter_url("https://www.twitter.com/someone"))

    def test_username_from_a_www_link(self):
        self.assertTrue(self.mgr._convert_to_nitter("https://www.x.com/@someone/status/1")
                        .endswith("/someone"))


class BoolSettingTest(unittest.TestCase):
    def test_strings(self):
        self.assertIs(config._BOOL("false"), False)
        self.assertIs(config._BOOL("True"), True)
        self.assertIs(config._BOOL(0), False)
        with self.assertRaises(ValueError):
            config._BOOL("maybe")




class EuropeanCloseTest(unittest.TestCase):
    """The previous close is picked by the stock's own exchange's closing
    time: at 16:30 Frankfurt is still trading, so today's bar is partial."""

    def test_frankfurt_afternoon_uses_yesterdays_close(self):
        try:
            import pandas as pd
        except ImportError as e:
            self.skipTest(f"pandas not installed here: {e}")
        import datetime
        import price_lookup

        days = pd.date_range("2026-09-14", "2026-09-18", freq="D", tz="Europe/Berlin")
        daily = pd.DataFrame({"High": [101, 102, 103, 104, 150], "Low": [99, 100, 101, 102, 90],
                              "Close": [100.0, 101.0, 102.0, 103.0, 120.0]}, index=days)
        minute = pd.DataFrame({"Close": [120.0]},
                              index=pd.DatetimeIndex(["2026-09-18 16:40"], tz="Europe/Berlin"))

        class Ticker:
            def __init__(self, symbol):
                pass

            def history(self, period, interval, **kwargs):
                return daily if interval == "1d" else minute

        published = datetime.datetime(2026, 9, 18, 14, 30, tzinfo=datetime.timezone.utc)  # 16:30 CEST
        with mock.patch.dict(sys.modules, {"yfinance": mock.Mock(Ticker=Ticker)}):
            ctx = price_lookup.fetch_context("BMW.DE", published)
        self.assertEqual(ctx["ref_close"], 103.0)


if __name__ == "__main__":
    unittest.main()
