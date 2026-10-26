"""Tests for the news sources: SEC 8-K parsing (edgar.py), the dead-feed
warning, and the headline noise filter for the press-release wires.
No network.

Run from the project root:  py -m unittest discover -s tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import feedparser  # noqa: E402

import config  # noqa: E402
import edgar  # noqa: E402
import news_collector  # noqa: E402
from news_collector import NewsCollector, is_noise_headline  # noqa: E402

SUMMARY = ("Filed: 2026-10-26 AccNo: 0000912767-26-000052 Size: 245 KB "
           "Item 1.01: Entry into a Material Definitive Agreement "
           "Item 7.01: Regulation FD Disclosure Item 9.01: Financial Statements and Exhibits")

INDEX = """<table>
<tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
<tr><td>1</td><td>CURRENT REPORT</td>
<td><a href="/ix?doc=/Archives/edgar/data/1/0001/a-8k.htm">a-8k.htm</a></td><td>8-K</td><td>1</td></tr>
<tr><td>2</td><td>AGREEMENT</td><td><a href="/Archives/edgar/data/1/0001/ex10.htm">ex10.htm</a></td>
<td>EX-10.5</td><td>1</td></tr>
<tr><td>3</td><td>PRESS RELEASE</td><td><a href="/Archives/edgar/data/1/0001/ex99-1.htm">ex99-1.htm</a></td>
<td>EX-99.1</td><td>1</td></tr>
</table>"""


class EdgarTest(unittest.TestCase):

    def test_items_and_materiality(self):
        self.assertEqual(edgar.items(SUMMARY), ["1.01", "7.01", "9.01"])
        self.assertTrue(edgar.is_material(edgar.items(SUMMARY)))
        # A vote result plus its exhibit list is routine.
        self.assertFalse(edgar.is_material(["5.07", "9.01"]))
        self.assertFalse(edgar.is_material([]))

    def test_headline(self):
        title = "8-K - UFP INDUSTRIES INC (0000912767) (Filer)"
        self.assertEqual(edgar.company(title), "Ufp Industries Inc")
        self.assertEqual(edgar.headline(title, ["1.01", "9.01"]),
                         "Ufp Industries Inc files 8-K: Material agreement")
        self.assertEqual(edgar.company("8-K - Sound Point Direct Lending BDC (0002061174) (Filer)"),
                         "Sound Point Direct Lending BDC")

    def test_document_prefers_press_release(self):
        self.assertEqual(edgar.document_url(INDEX),
                         "https://www.sec.gov/Archives/edgar/data/1/0001/ex99-1.htm")
        without = INDEX.replace("EX-99.1", "EX-10.6")
        # The 8-K itself, out of the inline-XBRL viewer link.
        self.assertEqual(edgar.document_url(without),
                         "https://www.sec.gov/Archives/edgar/data/1/0001/a-8k.htm")

    def test_document_text(self):
        html = "<html><head><title>x</title></head><body><div>Acme <font>agrees</font></div>" \
               "<script>var a;</script><p>to be acquired.</p></body></html>"
        self.assertEqual(edgar.document_text(html, 100), "Acme agrees to be acquired.")
        self.assertEqual(edgar.document_text(html, 4), "Acme")


class EdgarFeedTest(unittest.TestCase):

    def setUp(self):
        self.collector = NewsCollector()
        self.collector.window_start = None

    def _feed(self, title, summary, link):
        return feedparser.FeedParserDict(entries=[feedparser.FeedParserDict(
            title=title, summary=summary, link=link)])

    def test_routine_filing_dropped_and_material_one_retitled(self):
        link = "https://www.sec.gov/Archives/edgar/data/1/0001/0001-index.htm"
        routine = self._feed("8-K - ACME CORP (0000000001) (Filer)",
                             "Item 5.07: Submission of Matters to a Vote", link)
        self.assertEqual(self.collector._feed_to_articles(routine, edgar.SOURCE_NAME, scrape=False), [])
        material = self._feed("8-K - ACME CORP (0000000001) (Filer)", SUMMARY, link)
        [article] = self.collector._feed_to_articles(material, edgar.SOURCE_NAME, scrape=False)
        self.assertEqual(article['title'], "Acme Corp files 8-K: Material agreement; "
                                           "Regulation FD disclosure")

    def test_edgar_feed_needs_user_agent(self):
        saved = config.SEC_USER_AGENT
        try:
            config.SEC_USER_AGENT = ""
            self.assertNotIn(edgar.FEED_URL, [u for u, _ in self.collector.MARKET_RSS_FEEDS])
            config.SEC_USER_AGENT = "Test test@example.com"
            self.assertIn(edgar.FEED_URL, [u for u, _ in self.collector.MARKET_RSS_FEEDS])
        finally:
            config.SEC_USER_AGENT = saved


class DeadFeedTest(unittest.TestCase):

    def test_warns_once_after_threshold_and_on_recovery(self):
        collector = NewsCollector()
        warnings = []
        collector.on_warning = warnings.append
        url = "https://example.com/feed"
        for _ in range(news_collector.FEED_DEAD_AFTER_SCANS + 5):
            collector._note_feed(url, "Example", False)
        self.assertEqual(len(warnings), 1)
        self.assertIn("Example", warnings[0])
        self.assertEqual(collector.dead_feeds(), [url])
        collector._note_feed(url, "Example", True)
        self.assertEqual(len(warnings), 2)
        self.assertEqual(collector.dead_feeds(), [])

    def test_a_feed_that_answers_resets_the_count(self):
        collector = NewsCollector()
        collector.on_warning = lambda m: self.fail(m)
        url = "https://example.com/feed"
        for _ in range(3):
            for _ in range(news_collector.FEED_DEAD_AFTER_SCANS - 1):
                collector._note_feed(url, "Example", False)
            collector._note_feed(url, "Example", True)

    def test_no_yahoo_or_investing_feeds(self):
        urls = [u for u, _ in NewsCollector.US_RSS_FEEDS + NewsCollector.EU_RSS_FEEDS]
        self.assertFalse(any('rssindex' in u or 'investing.com' in u for u in urls))


class WireNoiseTest(unittest.TestCase):

    def test_law_firm_solicitations_are_noise(self):
        for headline in (
                "What Should Honeywell Aerospace, Inc. Investors Know After 23.16% Stock Drop? "
                "The Law Offices of Frank R. Cruz Investigates",
                "SHAREHOLDER ALERT: Pomerantz Law Firm Investigates Claims On Behalf of Investors of X",
                "ROSEN, A TOP RANKED LAW FIRM, Encourages Acme Investors to Secure Counsel",
                "Acme Class Action: Lead Plaintiff Deadline Approaching",
                "Investors who lost money on Acme should contact Levi & Korsinsky"):
            self.assertTrue(is_noise_headline(headline), headline)

    def test_company_news_is_not_noise(self):
        for headline in ("Acme to be acquired by Globex for $45 per share in cash",
                         "Acme faces class action over data breach, shares fall",
                         "Acme reports record third-quarter revenue"):
            self.assertFalse(is_noise_headline(headline), headline)

    def test_call_transcripts_from_the_old_ledger_are_noise(self):
        # C11: VGZ and TLX.AX were traded on "M&A Call Transcript" items.
        self.assertTrue(is_noise_headline("Vista Gold Corp. (VGZ) M&A Call Transcript"))
        self.assertTrue(is_noise_headline("Telix Pharmaceuticals Limited (TLX.AX) M&A Call Transcript"))


if __name__ == '__main__':
    unittest.main()
