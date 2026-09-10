"""Tests for the Reddit source. No network: the feeds below are synthetic but
shaped like Reddit's real Atom output (September 2026), and the poller is
given a fake fetch and a fake clock.

Run from the project root:  py -m unittest discover -s tests
"""

import datetime
import os
import sys
import tempfile
import unittest
from xml.sax.saxutils import escape

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import llm_prompts  # noqa: E402
import reddit_source as rs  # noqa: E402
from source_manager import SourceManager  # noqa: E402

T0 = datetime.datetime(2026, 9, 10, 15, 0, tzinfo=datetime.timezone.utc)


def _iso(minutes_before_t0):
    return (T0 - datetime.timedelta(minutes=minutes_before_t0)).isoformat()


def _post_html(body, author):
    return (f'<!-- SC_OFF --><div class="md"><p>{body}</p></div><!-- SC_ON --> &#32; submitted by &#32; '
            f'<a href="https://www.reddit.com/user/{author}"> /u/{author} </a> <br/> '
            f'<span><a href="https://example.com">[link]</a></span> &#32; '
            f'<span><a href="https://www.reddit.com/r/stocks/comments/x/">[comments]</a></span>')


def post_entry(pid, title, author="trader1", age_min=5, body="Some thoughts on the company."):
    link = f"https://www.reddit.com/r/stocks/comments/{pid}/{title.lower().replace(' ', '_')[:30]}/"
    return (f'<entry><author><name>/u/{author}</name></author>'
            f'<category term="stocks" label="r/stocks"/>'
            f'<content type="html">{escape(_post_html(body, author))}</content>'
            f'<id>t3_{pid}</id><link href="{link}"/>'
            f'<updated>{_iso(age_min)}</updated><published>{_iso(age_min)}</published>'
            f'<title>{escape(title)}</title></entry>')


def comment_entry(cid, text, author="replier"):
    # Real comment entries carry <updated> but no <published>.
    html = f'<!-- SC_OFF --><div class="md"><p>{text}</p></div><!-- SC_ON -->'
    return (f'<entry><author><name>/u/{author}</name></author>'
            f'<content type="html">{escape(html)}</content>'
            f'<id>t1_{cid}</id><link href="https://www.reddit.com/r/stocks/comments/abc/t/{cid}/"/>'
            f'<updated>{_iso(1)}</updated><title>/u/{author} on a post</title></entry>')


def feed(*entries):
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">'
            '<category term="stocks" label="r/stocks"/><updated>2026-09-10T15:00:00+00:00</updated>'
            '<id>/r/stocks/new/.rss</id><title>newest submissions : stocks</title>'
            + "".join(entries) + '</feed>').encode("utf-8")


class FakeClock:
    def __init__(self):
        self.now = T0.timestamp()

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeFetch:
    """Answers each URL from a list of (url substring, response) rules."""

    def __init__(self):
        self.rules = []
        self.calls = []

    def on(self, fragment, status=200, body=b"", headers=None):
        self.rules.insert(0, (fragment, (status, body, headers or {})))

    def __call__(self, url):
        self.calls.append(url)
        for fragment, response in self.rules:
            if fragment in url:
                return response
        return 404, b"", {}


class SettingsMixin:
    """Pins the Reddit settings for a test and restores them afterwards."""

    SETTINGS = dict(REDDIT_COMMENTS_PER_POST=10, REDDIT_COMMENT_DELAY_MIN=30,
                    REDDIT_POLL_MINUTES=10, REDDIT_MAX_POST_AGE_HOURS=6, REDDIT_CAN_TRADE=False)

    def setUp(self):
        self._saved = {k: getattr(config, k) for k in self.SETTINGS}
        for k, v in self.SETTINGS.items():
            setattr(config, k, v)
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(config, k, v)
        self.tmp.cleanup()


# --- links -------------------------------------------------------------------

class LinkTests(unittest.TestCase):
    def test_parse_subreddit(self):
        cases = {
            "r/wallstreetbets": "wallstreetbets",
            "/r/stocks/": "stocks",
            "reddit.com/r/stocks": "stocks",
            "https://www.reddit.com/r/stocks/new/": "stocks",
            "https://old.reddit.com/r/Investing/?sort=top": "Investing",
            "https://www.reddit.com/r/stocks/comments/1abc/some_post/": "stocks",
            "https://www.reddit.com/user/someone/": None,
            "https://www.reddit.com/": None,
            "https://example.com/r/stocks": None,
            "stocks": None,
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(rs.parse_subreddit(url), expected)
        self.assertEqual(rs.parse_subreddit("stocks", bare_ok=True), "stocks")

    def test_lookalike_hosts_are_not_reddit(self):
        self.assertFalse(rs.is_reddit_url("https://www.reddit.com.evil.example/r/stocks"))
        self.assertFalse(rs.is_reddit_url("https://notreddit.com/r/stocks"))
        self.assertTrue(rs.is_reddit_url("https://np.reddit.com/r/stocks"))


# --- parsing -----------------------------------------------------------------

class ParseTests(unittest.TestCase):
    def test_listing(self):
        posts = rs.parse_listing(feed(post_entry("1abc", "NVDA beats earnings", body="Revenue up 40%.")), "stocks")
        self.assertEqual(len(posts), 1)
        post = posts[0]
        self.assertEqual(post["id"], "1abc")
        self.assertEqual(post["author"], "trader1")
        self.assertEqual(post["body"], "Revenue up 40%.")  # no footer, no markup
        self.assertTrue(post["link"].startswith("https://www.reddit.com/r/stocks/comments/1abc/"))
        self.assertTrue(post["link"].endswith("/"))
        self.assertEqual(post["posted_ts"], _iso(5))

    def test_thread_skips_bots_and_deleted(self):
        body = feed(
            post_entry("1abc", "A post", body="Original text"),
            comment_entry("c1", "Welcome to r/stocks!", author="AutoModerator"),
            comment_entry("c2", "[deleted]"),
            comment_entry("c3", "Great catch, their guidance was raised too", author="alice"),
            comment_entry("c4", "Old news, this was in the 10-Q", author="bob"),
            comment_entry("c5", "third", author="carol"),
        )
        post_text, comments = rs.parse_thread(body, limit=2)
        self.assertEqual(post_text, "Original text")
        self.assertEqual(comments, [("alice", "Great catch, their guidance was raised too"),
                                    ("bob", "Old news, this was in the 10-Q")])

    def test_removed_post_is_reported(self):
        post_text, _ = rs.parse_thread(feed(post_entry("1abc", "A post", body="[removed]")), limit=5)
        self.assertEqual(post_text, "[removed]")


class ArticleTests(unittest.TestCase):
    def post(self, body="Body text"):
        return {"id": "1abc", "subreddit": "stocks", "title": "NVDA beats", "link": "https://www.reddit.com/r/stocks/comments/1abc/x/",
                "author": "a", "posted_ts": _iso(40), "body": body}

    def test_shape(self):
        article = rs.build_article(self.post(), [("alice", "Agreed"), ("bob", "Priced in")])
        self.assertEqual(article["source"], "Reddit/r/stocks")
        self.assertTrue(rs.is_reddit_article(article))
        self.assertEqual(article["published_ts"], _iso(40))
        self.assertIn("Top comments (2):", article["content"])
        self.assertIn("- u/bob: Priced in", article["content"])

    def test_fits_prompt_budget(self):
        comments = [(f"user{i}", "x" * rs.COMMENT_CHAR_LIMIT) for i in range(25)]
        article = rs.build_article(self.post("y" * rs.POST_BODY_LIMIT), comments)
        self.assertLessEqual(len(article["content"]), rs.CONTENT_LIMIT)
        self.assertIn("Top comments", article["content"])

    def test_link_post_falls_back_to_title(self):
        article = rs.build_article(self.post(body=""), [])
        self.assertEqual(article["description"], "NVDA beats")


# --- the poller --------------------------------------------------------------

class PollerTests(SettingsMixin, unittest.TestCase):
    SOURCES = [{"url": "https://www.reddit.com/r/stocks/", "type": "reddit"}]

    def make(self):
        self.clock = FakeClock()
        self.fetch = FakeFetch()
        path = os.path.join(self.tmp.name, "reddit_state.json")
        return rs.RedditPoller(state_file=path, fetch=self.fetch, clock=self.clock, log=lambda m: None)

    def test_post_waits_for_comments_then_is_analysed_once(self):
        # A listing that falls due goes first (it only ever takes half the
        # budget); pushed out of the way here so the order is predictable.
        config.REDDIT_POLL_MINUTES = 60
        poller = self.make()
        self.fetch.on("/r/stocks/new/", body=feed(post_entry("1abc", "NVDA beats earnings", age_min=5)))
        self.fetch.on("/comments/1abc/", body=feed(post_entry("1abc", "NVDA beats earnings"),
                                                   comment_entry("c1", "Guidance raised too", "alice")))

        self.assertEqual(poller.poll(self.SOURCES), [])           # listing: post queued
        self.assertEqual(poller.pending_counts(), {"stocks": 1})
        self.assertEqual(len(self.fetch.calls), 1)

        self.assertEqual(poller.poll(self.SOURCES), [])           # inside the rate gap
        self.assertEqual(len(self.fetch.calls), 1)

        self.clock.advance(rs.MIN_REQUEST_GAP)                    # gap over, post not due yet
        self.assertEqual(poller.poll(self.SOURCES), [])
        self.assertEqual(len(self.fetch.calls), 1)

        self.clock.advance(25 * 60)                               # 30 min after posting
        articles = poller.poll(self.SOURCES)
        self.assertEqual(len(self.fetch.calls), 2)
        self.assertIn("sort=top", self.fetch.calls[1])
        self.assertEqual(len(articles), 1)
        self.assertIn("u/alice: Guidance raised too", articles[0]["content"])
        self.assertEqual(poller.pending_counts(), {})

        self.clock.advance(35 * 60)                               # next listing: same post again
        self.assertEqual(poller.poll(self.SOURCES), [])
        self.assertIn("/new/", self.fetch.calls[2])
        self.assertEqual(poller.pending_counts(), {})

    def test_at_most_one_request_per_poll(self):
        poller = self.make()
        sources = self.SOURCES + [{"url": "https://www.reddit.com/r/wallstreetbets/", "type": "reddit"}]
        for _ in range(5):
            before = len(self.fetch.calls)
            poller.poll(sources)
            self.assertLessEqual(len(self.fetch.calls) - before, 1)
            self.clock.advance(rs.MIN_REQUEST_GAP)
        self.assertEqual(len(self.fetch.calls), 2)  # one listing each; nothing else due

    def test_noise_and_stale_posts_are_skipped(self):
        poller = self.make()
        self.fetch.on("/r/stocks/new/", body=feed(
            post_entry("p1", "Daily Discussion Thread for September 10, 2026"),
            post_entry("p2", "Rules reminder", author="AutoModerator"),
            post_entry("p3", "Old story", age_min=7 * 60),
            post_entry("p4", "Already analysed"),
            post_entry("p5", "Fresh catalyst"),
        ))
        poller.poll(self.SOURCES, is_seen=lambda url: "/p4/" in url)
        self.assertEqual([p["id"] for p in poller.pending], ["p5"])

    def test_rate_limit_backs_off_to_reddits_reset(self):
        poller = self.make()
        self.fetch.on("/r/stocks/new/", status=429, headers={"x-ratelimit-reset": "90", "x-ratelimit-remaining": "0"})
        poller.poll(self.SOURCES)
        self.assertEqual(poller._next_request_at, self.clock() + 92)
        self.clock.advance(60)
        poller.poll(self.SOURCES)
        self.assertEqual(len(self.fetch.calls), 1)  # still waiting out the reset
        self.clock.advance(40)
        poller.poll(self.SOURCES)
        self.assertEqual(len(self.fetch.calls), 2)  # listing retried, not skipped

    def test_removed_post_is_dropped(self):
        poller = self.make()
        self.fetch.on("/r/stocks/new/", body=feed(post_entry("1abc", "Buy this now", age_min=31)))
        self.fetch.on("/comments/1abc/", body=feed(post_entry("1abc", "Buy this now", body="[removed]")))
        poller.poll(self.SOURCES)
        self.clock.advance(rs.MIN_REQUEST_GAP)
        self.assertEqual(poller.poll(self.SOURCES), [])
        self.assertEqual(poller.pending, [])

    def test_failed_comments_fall_back_to_the_post_alone(self):
        poller = self.make()
        self.fetch.on("/r/stocks/new/", body=feed(post_entry("1abc", "Contract win", age_min=31)))
        self.fetch.on("/comments/1abc/", status=503)
        poller.poll(self.SOURCES)
        results = []
        for _ in range(10):  # listings due in between take their turn too
            self.clock.advance(rs.RETRY_DELAY)
            results += poller.poll(self.SOURCES)
        self.assertEqual(len(results), 1)
        self.assertNotIn("Top comments", results[0]["content"])
        self.assertEqual(sum("/comments/1abc/" in url for url in self.fetch.calls), rs.MAX_ATTEMPTS)
        self.assertEqual(poller.pending, [])

    def test_zero_comments_analyses_immediately(self):
        config.REDDIT_COMMENTS_PER_POST = 0
        poller = self.make()
        self.fetch.on("/r/stocks/new/", body=feed(post_entry("1abc", "Contract win"), post_entry("2def", "FDA nod")))
        articles = poller.poll(self.SOURCES)
        self.assertEqual([a["title"] for a in articles], ["Contract win", "FDA nod"])
        self.assertEqual(poller.pending, [])

    def test_state_survives_a_restart(self):
        poller = self.make()
        self.fetch.on("/r/stocks/new/", body=feed(post_entry("1abc", "Contract win")))
        poller.poll(self.SOURCES)
        again = rs.RedditPoller(state_file=poller.state_file, fetch=self.fetch, clock=self.clock, log=lambda m: None)
        self.assertEqual([p["id"] for p in again.pending], ["1abc"])
        self.assertIn("1abc", again._seen_set)

    def test_removed_source_drops_its_waiting_posts(self):
        poller = self.make()
        self.fetch.on("/r/stocks/new/", body=feed(post_entry("1abc", "Contract win")))
        poller.poll(self.SOURCES)
        poller.poll([])
        self.assertEqual(poller.pending, [])

    def test_listings_use_at_most_half_the_budget(self):
        subs = [f"sub{i}" for i in range(20)]
        self.assertEqual(rs.RedditPoller._listing_interval(len(subs)), 40 * 60)


# --- wiring into the rest of the app -----------------------------------------

class SourceManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mgr = SourceManager(sources_file=os.path.join(self.tmp.name, "sources.json"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_subreddit_is_normalised(self):
        self.mgr.add_source("", "https://old.reddit.com/r/wallstreetbets/new/?sort=new", "webpage")
        source = self.mgr.get_sources()[-1]
        self.assertEqual(source["type"], "reddit")
        self.assertEqual(source["url"], "https://www.reddit.com/r/wallstreetbets/")
        self.assertEqual(source["name"], "r/wallstreetbets")

    def test_bare_name_with_reddit_type(self):
        self.mgr.add_source("Stocks", "stocks", "reddit")
        self.assertEqual(self.mgr.get_sources()[-1]["url"], "https://www.reddit.com/r/stocks/")

    def test_duplicates_and_non_subreddits_are_refused(self):
        self.mgr.add_source("WSB", "r/wallstreetbets")
        with self.assertRaises(ValueError):
            self.mgr.add_source("again", "https://www.reddit.com/r/WallStreetBets/")
        with self.assertRaises(ValueError):
            self.mgr.add_source("a user", "https://www.reddit.com/user/someone/")


class PromptTests(unittest.TestCase):
    def article(self, source):
        return {"title": "NVDA beats", "content": "Revenue up 40 percent. " * 10, "source": source,
                "published_ts": _iso(40)}

    def test_reddit_posts_are_framed_as_opinion(self):
        prompt = llm_prompts.build_market_prompt("Custom Source News", self.article("Reddit/r/stocks"), True)
        self.assertIn("a post on r/stocks", prompt)
        self.assertIn("NOT a news report", prompt)
        trade = llm_prompts.build_trade_prompt(self.article("Reddit/r/stocks"), {"ticker": "NVDA"}, {}, "LONG")
        self.assertIn("NOT a news report", trade)

    def test_news_is_unchanged(self):
        prompt = llm_prompts.build_market_prompt("Custom Source News", self.article("Custom/CNBC"), True)
        self.assertNotIn("Reddit", prompt)


class TradeGateTests(SettingsMixin, unittest.TestCase):
    def test_reddit_alerts_do_not_trade_by_default(self):
        try:
            from main import StockAppBackend
        except ImportError as e:  # main pulls in yfinance/pandas
            self.skipTest(f"main not importable here: {e}")
        reddit = {"source": "Reddit/r/stocks"}
        news = {"source": "Custom/CNBC"}
        self.assertFalse(StockAppBackend._may_trade_on(reddit))
        self.assertTrue(StockAppBackend._may_trade_on(news))
        config.REDDIT_CAN_TRADE = True
        self.assertTrue(StockAppBackend._may_trade_on(reddit))


if __name__ == "__main__":
    unittest.main()
