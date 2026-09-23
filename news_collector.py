import requests
import collections
import datetime
import hashlib
import ipaddress
import re
import socket
import sys
import threading
import time
import pytz
from urllib.parse import urlparse, urlsplit, urlunsplit, parse_qsl, urlencode, urljoin
from bs4 import BeautifulSoup, SoupStrainer, XMLParsedAsHTMLWarning
import warnings
from concurrent.futures import ThreadPoolExecutor
from source_manager import SourceManager, source_trust, OPINION
from reddit_source import RedditPoller, is_reddit_url, parse_subreddit
from config import LOOKBACK_MINUTES
import config
import feedparser

# Suppress warnings when parsing XML/RSS with html.parser (intended behavior for robustness)
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

# The progress lines below use ✓/✗ glyphs. On a Windows console that is not
# UTF-8 those raise UnicodeEncodeError - and one raised from inside
# scrape_article's own except-block escaped the thread pool and aborted the
# whole scan cycle. Make stdout tolerant here rather than relying on another
# module having done so; errors='replace' guarantees a print never raises.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

# feedparser fetches URLs itself through urllib, with no timeout at all, so
# one unresponsive feed host could park the scan thread indefinitely. Feeds
# are normally fetched through our own session (which has a timeout) and only
# handed to feedparser as bytes; this default covers the fallback where
# feedparser is asked to fetch the URL itself. requests sets explicit
# timeouts and is unaffected.
if socket.getdefaulttimeout() is None:
    socket.setdefaulttimeout(30)

FEED_ACCEPT = 'application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.8'


def _strip_html(text):
    """Feed summaries frequently arrive as HTML ("<p>...</p><img ...>").
    Sent raw, the tags cost prompt tokens and give the keyword scorer
    attributes to chew on; keep the visible text only."""
    if not text or '<' not in text:
        return text or ''
    try:
        return ' '.join(BeautifulSoup(text, 'html.parser').get_text(' ').split())
    except Exception:
        return text


def _looks_like_feed(body):
    """Whether fetched bytes are an RSS/Atom document rather than HTML."""
    head = body[:2048].lstrip().lower()
    return head.startswith(b'<?xml') or b'<rss' in head or b'<feed' in head or b'<rdf:rdf' in head


def _norm_url(url):
    return (url or '').strip().lower().rstrip('/')


# Query parameters that only say where a click came from. Feeds tag their
# links with them ("?mod=mw_rss_topstories", "?source=feed_all_articles"), so
# the same story reached by two routes had two URLs - and was analysed, and
# alerted on, twice.
_TRACKING_PARAMS = {'mod', 'source', 'ncid', '.tsrc', 'tsrc', 'cmpid', 'fbclid', 'gclid',
                    'mc_cid', 'mc_eid', 'guccounter', 'guce_referrer', 'guce_referrer_sig',
                    'soc_src', 'soc_trk', 'yptr'}


def clean_url(url):
    """`url` without tracking parameters or a #fragment - the key an article
    is deduplicated by. Anything unparseable is returned as it was."""
    url = (url or '').strip()
    if '?' not in url and '#' not in url:
        return url
    try:
        parts = urlsplit(url)
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if not (k.lower().startswith('utm_') or k.lower() in _TRACKING_PARAMS)]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))
    except ValueError:
        return url


def title_key(title):
    """A headline reduced to its words, for spotting one story carried by
    several feeds under different URLs (Yahoo US and Yahoo UK, a wire story
    syndicated to three sites). None for a headline too short to be told
    apart from an unrelated one ("Market update")."""
    words = re.findall(r'[a-z0-9]+', (title or '').lower())
    return ' '.join(words) if len(words) >= 5 else None


# Headlines the analysis prompt already classes as irrelevant, dropped before
# they cost a page scrape and a paid model call. Deliberately narrow - only
# formats that are never a single company's new, tradable news: transcripts
# of calls that were reported when they happened, conference-appearance
# notices, funds' quarterly letters, buy/watch lists, market wraps and advice
# columns.
_NOISE_HEADLINE = re.compile(
    r'earnings call transcript|\btranscript\s*$'
    r'|\bq[1-4] \d{4} (?:commentary|letter)\b'
    r'|\bpresents? at\b.{0,80}\bconference\b'
    r'|\b(?:stocks?|shares|etfs?) to (?:buy|watch|sell|avoid|own)\b'
    r'|\bstock market today\b'
    r'|\bstocks making the biggest moves\b'
    r'|\bshould (?:i|we)\b',
    re.I)


def is_noise_headline(title, url=None):
    """Whether an article is one of the formats above - by its headline, or
    by a URL filed under transcripts (Investing.com's "X at Y Summit"
    items, which read like news but are call transcripts)."""
    return (bool(_NOISE_HEADLINE.search(title or ''))
            or '/transcripts/' in (url or '').lower())


# Longest article text kept. The prompt reads the first 5000 characters
# (llm_prompts._article_text) and the keyword engine 2500, so anything past
# this was held in memory - several articles at once - for nothing.
MAX_CONTENT_CHARS = 6000

# Scraped texts remembered for articles not yet analysed, so a scan that
# couldn't analyse them (the engine down) doesn't make every later one
# download and parse the same pages again. 64 x MAX_CONTENT_CHARS is well
# under half a megabyte.
SCRAPE_CACHE_SIZE = 64
# A page that couldn't be scraped is tried again after this long, not on
# every scan.
SCRAPE_RETRY_SECONDS = 10 * 60

# A Nitter instance that failed is left alone this long.
NITTER_BACKOFF_SECONDS = 30 * 60


class ConsentWall(Exception):
    """A request ended on a cookie-consent page instead of the content."""


# Hosts publishers redirect to for their cookie-consent page - Yahoo does,
# from EU addresses: guce.yahoo.com -> consent.yahoo.com. The page's text is
# the same cookie notice for every article, and fed to the model as the
# article it made every Yahoo story look irrelevant.
_CONSENT_HOST_PREFIXES = ('consent.', 'guce.')


def _is_consent_wall(url):
    host = (urlparse(url or '').hostname or '').lower()
    return host.startswith(_CONSENT_HOST_PREFIXES)

# Browser-like headers to get past basic anti-bot checks.
BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    # No 'br': requests can only decode Brotli with the brotli package, which
    # isn't installed (and isn't worth its RAM on the VM). Advertising it got
    # undecodable bytes back from servers that prefer it - City AM does.
    'Accept-Encoding': 'gzip, deflate',
    'DNT': '1',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
    'Cache-Control': 'max-age=0',
    'Referer': 'https://www.google.com/'
}

# Stop reading a response after this much HTML. Without a cap, a single very
# large (or deliberately endless) URL would be pulled entirely into memory.
#
# Kept at 3MB deliberately. Lowering it looks like an easy memory win, but it
# is the only knob here that can cost article text rather than just bytes: a
# page carrying a large inline script/JSON blob ahead of its body would get
# truncated before the paragraphs we actually want, quietly giving the
# analyzer less to work with. It also buys much less than it used to now that
# ARTICLE_STRAINER (below) keeps the parse tree small regardless of page size
# - the difference is a couple of MB per concurrent scrape, not the tens of MB
# the tree used to cost. Not worth the trade.
MAX_DOWNLOAD_BYTES = 3 * 1024 * 1024

# scrape_article only ever reads <p> tags, so tell BeautifulSoup to build tree
# nodes for those alone. On a news page the discarded nav/script/style/div
# scaffolding is the overwhelming majority of the document, and skipping it is
# where most of the parse-time memory saving comes from.
ARTICLE_STRAINER = SoupStrainer('p')

# Scraping (network fetch + parse of a full article page) is by far the
# slowest part of a scan cycle. Doing it concurrently instead of one URL at a
# time turns "N articles * ~1-3s each" into roughly the slowest single fetch.
MAX_SCRAPE_WORKERS = 6

# Articles taken from the built-in feeds per scan, across all of them. Each
# one costs a page fetch, a parse and an LLM call, so this is what bounds a
# cycle's cost; the ones left behind are not marked processed and are still
# offered next cycle, while they remain inside the lookback window.
MAX_ARTICLES_PER_SCAN = 24


def _interleave_newest(groups, limit):
    """Up to `limit` articles taken a round at a time - each group's newest
    unused article, then each group's next - so every feed is represented
    before any feed gets a second helping. Within a round the newest goes
    first; the result is sorted newest-first overall.

    Ties and short feeds look after themselves: a group that runs out simply
    stops contributing, and the remaining quota goes to the others.
    """
    queues = [sorted(g, key=lambda a: a['_pub_dt'], reverse=True) for g in groups if g]
    picked = []
    while queues and len(picked) < limit:
        round_ = []
        for queue in queues:
            if queue:
                round_.append(queue.pop(0))
        round_.sort(key=lambda a: a['_pub_dt'], reverse=True)
        picked.extend(round_[:limit - len(picked)])
        queues = [q for q in queues if q]
    picked.sort(key=lambda a: a['_pub_dt'], reverse=True)
    return picked


def is_public_url(url):
    """Whether a URL points at a public host.

    Applied to URLs discovered *inside* fetched pages, which the operator
    never chose. Sources the user configured themselves are exempt, so
    pointing the app at a self-hosted feed on the LAN still works.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            return False
        infos = socket.getaddrinfo(parsed.hostname, None)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast):
                return False
        return True
    except Exception:
        return False


class NewsCollector:
    # Major financial news RSS feeds (free, no API key needed).
    US_RSS_FEEDS = [
        ('https://www.cnbc.com/id/100003114/device/rss/rss.html', 'CNBC Top News'),
        ('https://feeds.content.dowjones.io/public/rss/mw_topstories', 'MarketWatch'),
        ('https://finance.yahoo.com/news/rssindex', 'Yahoo Finance'),
        ('https://www.investing.com/rss/news.rss', 'Investing.com')
    ]

    # European coverage (config.SCAN_EUROPE). The US wires above do report
    # the largest European names, but only once a story is big enough to
    # cross the Atlantic - which is usually after the move. These carry the
    # same stories hours earlier, and the mid-caps not at all.
    #
    # English-language on purpose: the analysis prompt is in English, and a
    # local model reads a German or French article visibly less well than an
    # English one. A national feed (Handelsblatt, Les Echos, Borsa Italiana)
    # can still be added as a custom source by anyone running a model that
    # handles it - see the README.
    EU_RSS_FEEDS = [
        ('https://www.ft.com/companies?format=rss', 'FT Companies'),
        ('https://uk.finance.yahoo.com/news/rssindex', 'Yahoo Finance UK'),
        ('https://www.cityam.com/feed/', 'City AM'),
        ('https://www.euronews.com/rss?level=theme&name=business', 'Euronews Business'),
    ]

    NITTER_INSTANCES = [
        'nitter.poast.org',
        'nitter.privacydev.net',
        'nitter.net',
        'nitter.lunar.icu',
        'nitter.1d4.us'
    ]

    @property
    def MARKET_RSS_FEEDS(self):
        """The built-in feeds to poll this cycle. Read live from config, so
        turning Europe off in settings takes effect on the next scan."""
        feeds = list(self.US_RSS_FEEDS)
        if getattr(config, 'SCAN_EUROPE', True):
            feeds += self.EU_RSS_FEEDS
        return feeds

    def __init__(self, source_mgr=None):
        # Shared with the UI when one is passed in, so a source added there
        # is fetched on the very next cycle instead of after a restart.
        self.source_mgr = source_mgr or SourceManager()
        # Optional callbacks set by the backend. Let us skip downloading
        # articles that were already analysed, instead of scraping them
        # every cycle and discarding them later: url -> bool, and headline
        # -> bool for the same story under another URL (see title_key).
        self.is_seen = None
        self.is_seen_title = None
        # One session for the whole app: without it every article scrape pays
        # for a fresh TCP + TLS handshake, and a cycle makes dozens of them.
        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)
        # One poller for every Reddit source: Reddit's feed budget is about
        # one request a minute for the whole app, not per source.
        self.reddit = RedditPoller()
        # Set by the scan loop before each scan: the oldest publish time
        # (UTC) still worth fetching, LOOKBACK_MINUTES before the previous
        # scan started. None - the first scan - means LOOKBACK_MINUTES ago.
        self.window_start = None
        # url -> (time.time() scraped, text or None); see SCRAPE_CACHE_SIZE.
        # Filled from the scrape pool's threads, hence the lock.
        self._scraped = collections.OrderedDict()
        self._scraped_lock = threading.Lock()
        # Nitter instance -> time.time() it may be tried again, and the one
        # that answered last (tried first next time).
        self._nitter_down_until = {}
        self._nitter_last_good = None

    def _seen(self, url):
        return bool(url and self.is_seen and self.is_seen(url))

    def _seen_article(self, url, title):
        """Already analysed - under this URL, or as the same headline under
        another one."""
        if self._seen(url):
            return True
        key = title_key(title)
        return bool(key and self.is_seen_title and self.is_seen_title(key))

    def _cutoff(self, now):
        """Articles published before this are too old (see window_start)."""
        cutoff = now - datetime.timedelta(minutes=LOOKBACK_MINUTES)
        return min(cutoff, self.window_start) if self.window_start else cutoff

    def _get(self, url, timeout=15, allow_redirects=True, headers=None):
        """GET a URL, reading at most MAX_DOWNLOAD_BYTES of the body."""
        response = self.session.get(
            url, timeout=timeout, allow_redirects=allow_redirects, stream=True,
            headers=headers,
        )
        try:
            response.raise_for_status()
            if _is_consent_wall(response.url):
                raise ConsentWall(response.url)
            chunks = []
            total = 0
            for chunk in response.iter_content(8192):
                chunks.append(chunk)
                total += len(chunk)
                if total >= MAX_DOWNLOAD_BYTES:
                    print(f"⚠ Response exceeded {MAX_DOWNLOAD_BYTES // (1024*1024)}MB, truncating: {url[:60]}...")
                    break
            return b''.join(chunks)
        finally:
            response.close()

    def scrape_article(self, url):
        """
        Attempts to scrape the full text of an article from its URL.

        Only failures are printed: on the VM stdout is journald, and a line
        per successful scrape was dozens of disk writes a scan.
        """
        soup = None
        try:
            body = self._get(url, timeout=15)

            soup = BeautifulSoup(body, 'html.parser', parse_only=ARTICLE_STRAINER)
            # Drop the raw bytes as soon as the tree exists - with several of
            # these running concurrently, holding both at once is the peak.
            body = None
            text = ' '.join(p.get_text() for p in soup.find_all('p'))

            if text.strip():
                return text.strip()
            else:
                print(f"✗ No content extracted from: {url[:80]}...")
                return None
        except requests.exceptions.Timeout:
            print(f"✗ Timeout scraping: {url[:80]}...")
            return None
        except requests.exceptions.HTTPError as e:
            print(f"✗ HTTP {e.response.status_code} error: {url[:80]}...")
            return None
        except ConsentWall:
            print(f"✗ Cookie-consent page instead of the article: {url[:80]}...")
            return None
        except Exception as e:
            print(f"✗ Failed to scrape ({type(e).__name__}): {url[:80]}...")
            return None
        finally:
            # A BeautifulSoup tree is full of parent<->child reference cycles,
            # so dropping the last name bound to it doesn't free it - it sits
            # there until the cyclic collector happens to run. decompose()
            # breaks the cycles now, which is what keeps a long-running
            # process from ratcheting upward one scan cycle at a time.
            if soup is not None:
                soup.decompose()

    def _scrape_cached(self, url):
        """scrape_article through the scrape cache: a page already scraped
        for an article still waiting to be analysed is not fetched again,
        nor is one that failed within SCRAPE_RETRY_SECONDS."""
        now = time.time()
        with self._scraped_lock:
            hit = self._scraped.get(url)
            if hit and (hit[1] is not None or now - hit[0] < SCRAPE_RETRY_SECONDS):
                self._scraped.move_to_end(url)
                return hit[1]
        text = self.scrape_article(url)
        if text:
            text = text[:MAX_CONTENT_CHARS]
        with self._scraped_lock:
            self._scraped[url] = (now, text)
            self._scraped.move_to_end(url)
            while len(self._scraped) > SCRAPE_CACHE_SIZE:
                self._scraped.popitem(last=False)
        return text

    def _scrape_many(self, articles):
        """Scrape full content for a batch of articles concurrently, in place."""
        targets = [a for a in articles if a.get('url') and not a.get('content')]
        if not targets:
            return
        with ThreadPoolExecutor(max_workers=min(len(targets), MAX_SCRAPE_WORKERS)) as pool:
            for article, content in zip(targets, pool.map(lambda a: self._scrape_cached(a['url']), targets)):
                article['content'] = content

    def _builtin_feed_urls(self):
        return {_norm_url(url) for url, _ in self.MARKET_RSS_FEEDS}

    def _is_redundant_custom(self, source):
        """A custom source that is one of the built-in feeds and adds nothing
        to it: same URL, the built-in feeds' own "reporting" trust, and a
        global scan that fetches the built-in copy anyway. Fetching it as a
        custom source instead dodged the per-scan cap and got 10 entries
        rather than 25. One marked "opinion" does add something - its trust -
        and replaces the built-in copy instead."""
        return (config.GLOBAL_SCAN and source_trust(source) != OPINION
                and _norm_url(source.get('url')) in self._builtin_feed_urls())

    def fetch_general_market_news(self):
        """
        Fetches top business headlines from major news RSS feeds (no APIs needed).

        Feeds are polled in parallel, and only the freshest MAX_ARTICLES_PER_SCAN
        articles across all of them are scraped. Scraping every candidate and only
        afterwards slicing to that number (the previous behaviour) meant paying
        for dozens of full-page fetches that were immediately discarded.

        The survivors are picked a round at a time, newest first from each
        feed in turn, rather than by taking the newest N overall. With one
        region's wires that was the same thing; with two it is not - a busy
        hour on the US wires would fill the whole quota and the European
        stories, which are the point of polling those feeds, would be
        dropped every cycle they arrived in.
        """
        # A built-in feed the user has also added as a custom source with a
        # trust of its own is fetched there instead (see _is_redundant_custom).
        replaced = {_norm_url(s.get('url')) for s in self.source_mgr.get_sources(enabled_only=True)
                    if not self._is_redundant_custom(s)}
        feeds = [(url, name) for url, name in self.MARKET_RSS_FEEDS if _norm_url(url) not in replaced]
        if not feeds:
            return []

        by_feed = self._fetch_feeds_unscraped(feeds)
        # One story carried by two feeds (Yahoo US and Yahoo UK) is analysed
        # once: the first copy is kept, the others dropped before scraping.
        seen_titles = set()
        for name, articles in by_feed.items():
            kept = []
            for article in articles:
                key = title_key(article['title'])
                if key and key in seen_titles:
                    continue
                if key:
                    seen_titles.add(key)
                kept.append(article)
            by_feed[name] = kept

        top_articles = _interleave_newest(by_feed.values(), MAX_ARTICLES_PER_SCAN)
        self._scrape_many(top_articles)
        for article in top_articles:
            article.pop('_pub_dt', None)
        return top_articles

    def _fetch_feeds_unscraped(self, feeds, limit=25):
        """{feed name: unscraped articles} for (url, name) `feeds`, fetched
        in parallel."""
        by_feed = {}
        if not feeds:
            return by_feed
        with ThreadPoolExecutor(max_workers=len(feeds)) as pool:
            futures = {
                pool.submit(self._fetch_from_rss, feed_url, source_name, limit, None, False): source_name
                for feed_url, source_name in feeds
            }
            for future in futures:
                source_name = futures[future]
                try:
                    by_feed.setdefault(source_name, []).extend(future.result())
                except Exception as e:
                    print(f"Error fetching {source_name}: {e}")
        return by_feed

    def fetch_company_news(self, companies, per_company=10):
        """{company: recent articles mentioning it} for every company in
        `companies`. Used when GLOBAL_SCAN is off and TARGET_COMPANIES is
        populated.

        Each feed is downloaded once per scan however many companies there
        are - it used to be once per company - and a company is matched as a
        whole word, so "Meta" no longer picks up every story about metals.
        An article mentioning two companies goes to the first. Same
        fetch-then-cap-then-scrape order as fetch_general_market_news.
        """
        result = {company: [] for company in companies}
        if not companies:
            return result
        by_feed = self._fetch_feeds_unscraped(self.MARKET_RSS_FEEDS)
        pool = sorted((a for articles in by_feed.values() for a in articles),
                      key=lambda a: a['_pub_dt'], reverse=True)
        taken = set()
        for company in companies:
            pattern = re.compile(r'(?<![A-Za-z0-9])' + re.escape(company) + r'(?![A-Za-z0-9])', re.I)
            for article in pool:
                if len(result[company]) >= per_company:
                    break
                if article['url'] in taken:
                    continue
                if pattern.search(f"{article['title']} {article['description']}"):
                    result[company].append(article)
                    taken.add(article['url'])
        chosen = [a for articles in result.values() for a in articles]
        self._scrape_many(chosen)
        for article in chosen:
            article.pop('_pub_dt', None)
        return result

    def fetch_news(self, company):
        """Recent articles mentioning one company (see fetch_company_news)."""
        return self.fetch_company_news([company])[company]

    def fetch_from_custom_sources(self):
        """
        Fetch news from user-defined custom sources.
        Returns a list of articles from all enabled sources.

        Sources are fetched concurrently - each is an independent network
        round-trip (and usually a scrape on top of that), so running them one
        at a time made the whole cycle as slow as the sum of every source
        instead of the slowest one.
        """
        sources = [s for s in self.source_mgr.get_sources(enabled_only=True)
                   if not self._is_redundant_custom(s)]

        if not sources:
            return []

        def fetch_one(source):
            source_name = source.get('name', 'Unknown')
            source_url = source.get('url')
            source_type = source.get('type', 'webpage')
            try:
                if source_type == 'twitter' or self._is_nitter_url(source_url):
                    articles = self._fetch_from_nitter(source_url, source_name)
                elif source_type == 'rss' or self._is_rss_feed(source_url):
                    articles = self._fetch_from_rss(source_url, source_name)
                else:
                    articles = self._fetch_from_webpage(source_url, source_name)
            except Exception as e:
                print(f"Error fetching from {source_name}: {e}")
                return []
            # Carried to the trade decision: a story from an opinion source
            # only trades once the price has confirmed it.
            trust = source_trust(source)
            for article in articles:
                article['trust'] = trust
            return articles

        # Reddit sources go to the poller as a group - they share one request
        # budget - and it runs alongside the others as one more job.
        reddit = [s for s in sources if self._is_reddit_source(s)]
        others = [s for s in sources if s not in reddit]

        all_articles = []
        with ThreadPoolExecutor(max_workers=min(len(sources), MAX_SCRAPE_WORKERS)) as pool:
            reddit_job = pool.submit(self._poll_reddit, reddit) if reddit else None
            for articles in pool.map(fetch_one, others):
                all_articles.extend(articles)
            if reddit_job:
                all_articles.extend(reddit_job.result())

        return all_articles

    def fetch_reddit_posts(self):
        """Poll the enabled Reddit sources alone - what the scan loop does
        between weekend scans (see StockAppBackend._wait)."""
        reddit = [s for s in self.source_mgr.get_sources(enabled_only=True) if self._is_reddit_source(s)]
        return self._poll_reddit(reddit) if reddit else []

    def _poll_reddit(self, sources):
        """One RedditPoller.poll() over the Reddit `sources`. The poller serves
        every subreddit at once; each post takes its own subreddit's trust
        setting ("Reddit/r/<sub>")."""
        try:
            posts = self.reddit.poll(sources, self._seen)
        except Exception as e:
            print(f"Error polling Reddit: {e}")
            return []
        trust = {(parse_subreddit(s.get('url'), bare_ok=True) or '').lower(): source_trust(s)
                 for s in sources}
        for post in posts:
            sub = (post.get('source') or '').split('/r/', 1)[-1].lower()
            post['trust'] = trust.get(sub, OPINION)
        return posts

    @staticmethod
    def _is_reddit_source(source):
        return source.get('type') == 'reddit' or is_reddit_url(source.get('url'))

    def _is_nitter_url(self, url):
        """Check if URL is a Nitter instance."""
        return 'nitter' in url.lower()

    def _nitter_order(self, now):
        """Instances worth trying now: the one that answered last first, then
        the rest, leaving out any that failed within NITTER_BACKOFF_SECONDS.
        Most public instances are dead, and trying all five with a 15s
        timeout on every scan cost over a minute a scan per Twitter source."""
        order = list(self.NITTER_INSTANCES)
        if self._nitter_last_good in order:
            order.remove(self._nitter_last_good)
            order.insert(0, self._nitter_last_good)
        return [i for i in order if self._nitter_down_until.get(i, 0) <= now]

    def _fetch_from_nitter(self, nitter_url, source_name):
        """Fetch tweets from Nitter HTML page with automatic instance fallback."""
        # Extract username from the URL
        username = None
        for instance in self.NITTER_INSTANCES:
            if instance in nitter_url:
                username = nitter_url.split(instance + '/')[-1].split('/')[0].split('?')[0]
                break

        if not username:
            print(f"Could not extract username from {nitter_url}")
            return []

        for instance in self._nitter_order(time.time()):
            test_url = f"https://{instance}/{username}"
            soup = None
            try:
                soup = BeautifulSoup(self._get(test_url, timeout=15), 'html.parser')
                articles = self._parse_tweets(soup, test_url, source_name)
            except Exception as e:
                status = getattr(getattr(e, 'response', None), 'status_code', None)
                print(f"✗ Nitter {instance} failed ({status or type(e).__name__}) - "
                      f"skipping it for {NITTER_BACKOFF_SECONDS // 60} min")
                articles = None
            finally:
                if soup is not None:
                    soup.decompose()
            if articles:
                self._nitter_last_good = instance
                return articles
            self._nitter_down_until[instance] = time.time() + NITTER_BACKOFF_SECONDS

        return []

    @staticmethod
    def _parse_tweets(soup, page_url, source_name):
        """The newest tweets on a Nitter timeline page, as articles."""
        articles = []
        # Nitter uses .timeline-item for tweets
        for tweet in soup.find_all('div', class_='timeline-item')[:10]:  # 10 most recent
            try:
                tweet_content = tweet.find('div', class_='tweet-content')
                if not tweet_content:
                    continue

                text = tweet_content.get_text(strip=True)
                if not text or len(text) < 10:
                    continue

                tweet_link = tweet.find('a', class_='tweet-link')
                if tweet_link:
                    tweet_url = tweet_link.get('href', '')
                    if tweet_url.startswith('/'):
                        tweet_url = urljoin(page_url, tweet_url)
                else:
                    # Generate a unique URL based on tweet text hash
                    tweet_hash = hashlib.md5(text.encode()).hexdigest()[:8]
                    tweet_url = f"{page_url}/status/{tweet_hash}"

                tweet_date = tweet.find('span', class_='tweet-date')
                timestamp = tweet_date.get('title', '') if tweet_date else ''

                articles.append({
                    'title': text[:100] + '...' if len(text) > 100 else text,
                    'description': text,
                    'url': tweet_url,
                    'publishedAt': timestamp if timestamp else datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    'source': f"Twitter/{source_name}",
                    'content': text
                })
            except Exception as e:
                print(f"Error parsing tweet: {e}")
        return articles

    def _is_rss_feed(self, url):
        """Check if URL appears to be an RSS feed."""
        rss_indicators = ['/rss', '/feed', '.xml', '.rss', '/atom']
        return any(indicator in url.lower() for indicator in rss_indicators)

    def _fetch_from_rss(self, feed_url, source_name, limit=10, match=None, scrape=True):
        """
        Fetch articles from RSS/Atom feed.

        If `match` is given, only entries mentioning it in the title or summary
        are kept. Filtering happens before scraping so we don't download pages
        we are going to discard.

        If `scrape` is False, articles are returned with content=None and an
        extra '_pub_dt' key (the parsed publish time, for sorting) instead of
        being scraped here. Callers that fetch from several feeds and only
        keep the newest N articles overall pass scrape=False, sort by
        '_pub_dt', trim to N, and scrape only the survivors.
        """
        try:
            feed = self._load_feed(feed_url)
            return self._feed_to_articles(feed, source_name, limit, match, scrape)
        except Exception as e:
            print(f"Error parsing RSS feed {feed_url}: {e}")
            return []

    def _load_feed(self, feed_url):
        """Download a feed through the shared session (timeout, browser
        headers, keep-alive) and hand the bytes to feedparser. Falls back to
        feedparser's own fetch for a host that rejects those headers - but
        only then: after a timeout, a connection error or a consent wall a
        second fetch (30s timeout, no size cap) fails the same way, and it
        was paid on every scan."""
        try:
            body = self._get(feed_url, timeout=15, headers={'Accept': FEED_ACCEPT})
        except requests.exceptions.HTTPError as e:
            print(f"Feed fetch failed (HTTP {e.response.status_code}) for {feed_url[:60]}, "
                  f"retrying via feedparser...")
            return feedparser.parse(feed_url)
        feed = feedparser.parse(body, response_headers={'content-location': feed_url})
        body = None
        if feed.entries or not feed.get('bozo'):
            return feed
        print(f"Feed at {feed_url[:60]} did not parse ({feed.get('bozo_exception')}), retrying via feedparser...")
        return feedparser.parse(feed_url)

    def _feed_to_articles(self, feed, source_name, limit=10, match=None, scrape=True):
        """Turn parsed feed entries into article dicts (see _fetch_from_rss)."""
        articles = []
        cutoff = self._cutoff(datetime.datetime.now(datetime.timezone.utc))

        for entry in feed.entries[:limit]:
            # Check if recent (within lookback period)
            pub_date = entry.get('published_parsed') or entry.get('updated_parsed')
            pub_datetime = None
            if pub_date:
                try:
                    pub_datetime = datetime.datetime(*pub_date[:6], tzinfo=pytz.UTC)
                except (TypeError, ValueError):
                    pub_datetime = None
            if pub_datetime and pub_datetime < cutoff:
                continue

            link = clean_url(entry.get('link'))
            title = ' '.join((entry.get('title') or '').split())
            if not link or self._seen_article(link, title) or is_noise_headline(title, link):
                continue

            summary = _strip_html(entry.get('summary', ''))

            if match:
                haystack = f"{title} {summary}".lower()
                if match.lower() not in haystack:
                    continue

            article = {
                'title': title,
                'description': summary,
                'url': link,
                'publishedAt': entry.get('published', ''),
                # Parsed, UTC. Survives the '_pub_dt' cleanup below: the
                # prompt reports the article's age, and the trade check
                # measures how far the price has moved since this moment.
                'published_ts': pub_datetime.isoformat() if pub_datetime else None,
                'source': f"Custom/{source_name}",
                'content': None,
                '_pub_dt': pub_datetime or datetime.datetime.min.replace(tzinfo=pytz.UTC),
            }
            articles.append(article)

        if scrape:
            self._scrape_many(articles)
            for article in articles:
                article.pop('_pub_dt', None)

        return articles

    def _fetch_from_webpage(self, url, source_name):
        """Fetch articles from a regular webpage."""
        soup = None
        try:
            body = self._get(url, timeout=15)
            # A source typed in as "webpage" is often really a feed URL that
            # the name-based check couldn't tell apart. Parsed as HTML, a
            # feed has no <h2 a>-style links and yields nothing every cycle.
            if _looks_like_feed(body):
                return self._feed_to_articles(feedparser.parse(body), source_name)

            soup = BeautifulSoup(body, 'html.parser')
            body = None
            articles = []

            # Try to find RSS feed link first
            rss_link = soup.find('link', type='application/rss+xml')
            if rss_link and rss_link.get('href'):
                rss_url = urljoin(url, rss_link['href'])
                # This URL came out of the page we just fetched, not from the
                # user, so don't let it aim the scraper at internal hosts.
                if not is_public_url(rss_url):
                    print(f"✗ Ignoring non-public feed URL advertised by page: {rss_url[:60]}...")
                    return []
                return self._fetch_from_rss(rss_url, source_name)

            # Extract article links from page
            article_elements = self._extract_article_links(soup, url)
            soup.decompose()
            soup = None

            source_host = (urlparse(url).hostname or '').lower()
            for elem in article_elements:
                if len(articles) >= 5:  # Limit to 5 articles per source
                    break
                title = elem.get('title', '')
                link = clean_url(elem.get('url', ''))

                if not title or not link:
                    continue

                if self._seen_article(link, title) or is_noise_headline(title, link):
                    continue

                # Discovered inside the page, so held to the same rule as a
                # discovered feed link - except on the source's own host,
                # which the user chose (a self-hosted page on the LAN).
                if (urlparse(link).hostname or '').lower() != source_host and not is_public_url(link):
                    print(f"✗ Ignoring non-public article link on {source_name}: {link[:60]}...")
                    continue

                articles.append({
                    'title': title,
                    'description': '',
                    'url': link,
                    'publishedAt': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    'source': f"Custom/{source_name}",
                    'content': None
                })

            # Scrape the survivors concurrently rather than one page at a
            # time; a page with no extractable text is dropped, as before.
            self._scrape_many(articles)
            return [a for a in articles if a['content']]

        except Exception as e:
            print(f"Error fetching webpage {url}: {e}")
            return []
        finally:
            # A full-page tree, not the <p>-only one scrape_article builds -
            # break its reference cycles now rather than at the next gc.
            if soup is not None:
                soup.decompose()

    def _extract_article_links(self, soup, base_url):
        """Extract article links from webpage using common patterns."""
        articles = []

        # Common article containers
        selectors = [
            'article a',
            'h2 a',
            'h3 a',
            '.article a',
            '.story a',
            '.news-item a',
            '[class*="headline"] a',
            '[class*="title"] a'
        ]

        seen_urls = set()

        for selector in selectors:
            links = soup.select(selector)

            for link in links:
                href = link.get('href')
                if not href:
                    continue

                # Make absolute URL
                full_url = urljoin(base_url, href)

                # Skip duplicates and non-article URLs
                if full_url in seen_urls:
                    continue
                if any(skip in full_url.lower() for skip in ['#', 'javascript:', 'mailto:', '/tag/', '/category/']):
                    continue

                # Get title
                title = link.get_text(strip=True)
                if not title or len(title) < 10:
                    continue

                articles.append({
                    'title': title,
                    'url': full_url
                })
                seen_urls.add(full_url)

                if len(articles) >= 10:
                    break

            if len(articles) >= 10:
                break

        return articles
