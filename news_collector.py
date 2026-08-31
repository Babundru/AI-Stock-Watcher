import requests
import datetime
import ipaddress
import socket
import pytz
from urllib.parse import urlparse
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
import warnings
from concurrent.futures import ThreadPoolExecutor
from source_manager import SourceManager
from config import LOOKBACK_MINUTES
import feedparser

# Suppress warnings when parsing XML/RSS with html.parser (intended behavior for robustness)
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

# Browser-like headers to get past basic anti-bot checks.
BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
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
MAX_DOWNLOAD_BYTES = 3 * 1024 * 1024

# Scraping (network fetch + parse of a full article page) is by far the
# slowest part of a scan cycle. Doing it concurrently instead of one URL at a
# time turns "N articles * ~1-3s each" into roughly the slowest single fetch.
MAX_SCRAPE_WORKERS = 6


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
    # Major financial news RSS feeds (free, no API key needed)
    MARKET_RSS_FEEDS = [
        ('https://www.cnbc.com/id/100003114/device/rss/rss.html', 'CNBC Top News'),
        ('https://feeds.content.dowjones.io/public/rss/mw_topstories', 'MarketWatch'),
        ('https://finance.yahoo.com/news/rssindex', 'Yahoo Finance'),
        ('https://www.investing.com/rss/news.rss', 'Investing.com')
    ]

    def __init__(self):
        self.source_mgr = SourceManager()
        # Optional callback set by the backend: url -> bool.
        # Lets us skip downloading articles that were already analysed,
        # instead of scraping them every cycle and discarding them later.
        self.is_seen = None
        # One session for the whole app: without it every article scrape pays
        # for a fresh TCP + TLS handshake, and a cycle makes dozens of them.
        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)

    def _seen(self, url):
        return bool(url and self.is_seen and self.is_seen(url))

    def _get(self, url, timeout=15, allow_redirects=True):
        """GET a URL, reading at most MAX_DOWNLOAD_BYTES of the body."""
        response = self.session.get(
            url, timeout=timeout, allow_redirects=allow_redirects, stream=True
        )
        try:
            response.raise_for_status()
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
        """
        try:
            body = self._get(url, timeout=15)

            soup = BeautifulSoup(body, 'html.parser')
            paragraphs = soup.find_all('p')
            text = ' '.join([p.get_text() for p in paragraphs])
            
            if text.strip():
                print(f"✓ Scraped successfully ({len(text)} chars): {url[:80]}...")
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
        except Exception as e:
            print(f"✗ Failed to scrape ({type(e).__name__}): {url[:80]}...")
            return None

    def _scrape_many(self, articles):
        """Scrape full content for a batch of articles concurrently, in place."""
        targets = [a for a in articles if a.get('url') and not a.get('content')]
        if not targets:
            return
        with ThreadPoolExecutor(max_workers=MAX_SCRAPE_WORKERS) as pool:
            for article, content in zip(targets, pool.map(lambda a: self.scrape_article(a['url']), targets)):
                article['content'] = content

    def fetch_general_market_news(self):
        """
        Fetches top business headlines from major news RSS feeds (no APIs needed).

        Feeds are polled in parallel, and only the freshest 20 articles across
        all of them are scraped. Scraping every candidate and only afterwards
        slicing to the top 20 (the previous behaviour) meant paying for up to
        80 full-page fetches that were immediately discarded.
        """
        all_articles = []
        with ThreadPoolExecutor(max_workers=len(self.MARKET_RSS_FEEDS)) as pool:
            futures = {
                pool.submit(self._fetch_from_rss, feed_url, source_name, 25, None, False): source_name
                for feed_url, source_name in self.MARKET_RSS_FEEDS
            }
            for future in futures:
                source_name = futures[future]
                try:
                    print(f"Fetching from {source_name}...")
                    all_articles.extend(future.result())
                except Exception as e:
                    print(f"Error fetching {source_name}: {e}")

        all_articles.sort(key=lambda a: a['_pub_dt'], reverse=True)
        top_articles = all_articles[:20]
        self._scrape_many(top_articles)
        for article in top_articles:
            article.pop('_pub_dt', None)
        return top_articles

    def fetch_news(self, company):
        """
        Fetches recent news mentioning a specific company.
        Used when GLOBAL_SCAN is off and TARGET_COMPANIES is populated.

        Same fetch-then-cap-then-scrape ordering as fetch_general_market_news,
        for the same reason: scraping is the expensive step, so it should only
        ever run on articles that will actually be kept.
        """
        all_articles = []
        with ThreadPoolExecutor(max_workers=len(self.MARKET_RSS_FEEDS)) as pool:
            futures = {
                pool.submit(self._fetch_from_rss, feed_url, source_name, 25, company, False): source_name
                for feed_url, source_name in self.MARKET_RSS_FEEDS
            }
            for future in futures:
                source_name = futures[future]
                try:
                    print(f"Searching {source_name} for '{company}'...")
                    all_articles.extend(future.result())
                except Exception as e:
                    print(f"Error fetching {source_name}: {e}")

        all_articles.sort(key=lambda a: a['_pub_dt'], reverse=True)
        top_articles = all_articles[:10]
        self._scrape_many(top_articles)
        for article in top_articles:
            article.pop('_pub_dt', None)
        return top_articles

    def fetch_from_custom_sources(self):
        """
        Fetch news from user-defined custom sources.
        Returns a list of articles from all enabled sources.

        Sources are fetched concurrently - each is an independent network
        round-trip (and usually a scrape on top of that), so running them one
        at a time made the whole cycle as slow as the sum of every source
        instead of the slowest one.
        """
        sources = self.source_mgr.get_sources(enabled_only=True)

        if not sources:
            print("No custom sources configured.")
            return []

        print(f"Fetching from {len(sources)} custom sources...")

        def fetch_one(source):
            source_name = source.get('name', 'Unknown')
            source_url = source.get('url')
            source_type = source.get('type', 'webpage')
            try:
                print(f"Scraping {source_name}...")
                if source_type == 'twitter' or self._is_nitter_url(source_url):
                    return self._fetch_from_nitter(source_url, source_name)
                elif source_type == 'rss' or self._is_rss_feed(source_url):
                    return self._fetch_from_rss(source_url, source_name)
                else:
                    return self._fetch_from_webpage(source_url, source_name)
            except Exception as e:
                print(f"Error fetching from {source_name}: {e}")
                return []

        all_articles = []
        with ThreadPoolExecutor(max_workers=min(len(sources), MAX_SCRAPE_WORKERS)) as pool:
            for articles in pool.map(fetch_one, sources):
                all_articles.extend(articles)

        print(f"Collected {len(all_articles)} articles from custom sources.")
        return all_articles
    
    def _is_nitter_url(self, url):
        """Check if URL is a Nitter instance."""
        return 'nitter' in url.lower()
    
    def _fetch_from_nitter(self, nitter_url, source_name):
        """Fetch tweets from Nitter HTML page with automatic instance fallback."""
        # List of Nitter instances to try
        nitter_instances = [
            'nitter.poast.org',
            'nitter.privacydev.net',
            'nitter.net',
            'nitter.lunar.icu',
            'nitter.1d4.us'
        ]
        
        # Extract username from the URL
        username = None
        for instance in nitter_instances:
            if instance in nitter_url:
                username = nitter_url.split(instance + '/')[-1].split('/')[0].split('?')[0]
                break
        
        if not username:
            print(f"Could not extract username from {nitter_url}")
            return []
        
        # Try each instance until one works
        for instance in nitter_instances:
            try:
                test_url = f"https://{instance}/{username}"
                print(f"Trying Nitter instance: {instance}...")
                
                soup = BeautifulSoup(self._get(test_url, timeout=15), 'html.parser')
                articles = []
                
                # Find tweet containers (Nitter uses .timeline-item for tweets)
                tweets = soup.find_all('div', class_='timeline-item')
                
                if not tweets:
                    print(f"No tweets found on {instance}, trying next instance...")
                    continue
                
                for tweet in tweets[:10]:  # Limit to 10 most recent tweets
                    try:
                        # Extract tweet text
                        tweet_content = tweet.find('div', class_='tweet-content')
                        if not tweet_content:
                            continue
                        
                        text = tweet_content.get_text(strip=True)
                        if not text or len(text) < 10:
                            continue
                        
                        # Extract tweet link
                        tweet_link = tweet.find('a', class_='tweet-link')
                        if tweet_link:
                            tweet_url = tweet_link.get('href', '')
                            if tweet_url.startswith('/'):
                                # Make absolute URL
                                from urllib.parse import urljoin
                                tweet_url = urljoin(test_url, tweet_url)
                        else:
                            # Generate a unique URL based on tweet text hash
                            import hashlib
                            tweet_hash = hashlib.md5(text.encode()).hexdigest()[:8]
                            tweet_url = f"{test_url}/status/{tweet_hash}"
                        
                        # Extract timestamp if available
                        tweet_date = tweet.find('span', class_='tweet-date')
                        timestamp = tweet_date.get('title', '') if tweet_date else ''
                        
                        article = {
                            'title': text[:100] + '...' if len(text) > 100 else text,
                            'description': text,
                            'url': tweet_url,
                            'publishedAt': timestamp if timestamp else datetime.datetime.now(datetime.timezone.utc).isoformat(),
                            'source': f"Twitter/{source_name}",
                            'content': text
                        }
                        
                        articles.append(article)
                        
                    except Exception as e:
                        print(f"Error parsing tweet: {e}")
                        continue
                
                if articles:
                    print(f"✓ Successfully extracted {len(articles)} tweets from {instance}")
                    return articles
                else:
                    print(f"No valid tweets extracted from {instance}, trying next...")
                    
            except requests.exceptions.HTTPError as e:
                if e.response.status_code in [403, 503, 429]:
                    print(f"✗ {instance} blocked/unavailable (HTTP {e.response.status_code}), trying next instance...")
                    continue
                else:
                    print(f"HTTP error from {instance}: {e}")
                    continue
            except Exception as e:
                print(f"Error with {instance}: {e}, trying next instance...")
                continue
        
        print(f"✗ All Nitter instances failed for {username}")
        return []
    
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
            feed = feedparser.parse(feed_url)
            articles = []

            for entry in feed.entries[:limit]:
                # Check if recent (within lookback period)
                pub_date = entry.get('published_parsed') or entry.get('updated_parsed')
                pub_datetime = None
                if pub_date:
                    pub_datetime = datetime.datetime(*pub_date[:6], tzinfo=pytz.UTC)
                    now = datetime.datetime.now(datetime.timezone.utc)
                    if (now - pub_datetime).total_seconds() > LOOKBACK_MINUTES * 60:
                        continue

                if match:
                    haystack = f"{entry.get('title', '')} {entry.get('summary', '')}".lower()
                    if match.lower() not in haystack:
                        continue

                if self._seen(entry.get('link', '')):
                    continue

                article = {
                    'title': entry.get('title', ''),
                    'description': entry.get('summary', ''),
                    'url': entry.get('link', ''),
                    'publishedAt': entry.get('published', ''),
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

        except Exception as e:
            print(f"Error parsing RSS feed {feed_url}: {e}")
            return []
    
    def _fetch_from_webpage(self, url, source_name):
        """Fetch articles from a regular webpage."""
        try:
            soup = BeautifulSoup(self._get(url, timeout=15), 'html.parser')
            articles = []

            # Try to find RSS feed link first
            rss_link = soup.find('link', type='application/rss+xml')
            if rss_link and rss_link.get('href'):
                rss_url = rss_link['href']
                if not rss_url.startswith('http'):
                    from urllib.parse import urljoin
                    rss_url = urljoin(url, rss_url)
                # This URL came out of the page we just fetched, not from the
                # user, so don't let it aim the scraper at internal hosts.
                if not is_public_url(rss_url):
                    print(f"✗ Ignoring non-public feed URL advertised by page: {rss_url[:60]}...")
                    return []
                print(f"Found RSS feed: {rss_url}")
                return self._fetch_from_rss(rss_url, source_name)
            
            # Extract article links from page
            article_elements = self._extract_article_links(soup, url)
            
            for elem in article_elements[:5]:  # Limit to 5 articles per source
                title = elem.get('title', '')
                link = elem.get('url', '')

                if not title or not link:
                    continue

                if self._seen(link):
                    continue

                article = {
                    'title': title,
                    'description': '',
                    'url': link,
                    'publishedAt': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    'source': f"Custom/{source_name}",
                    'content': None
                }
                
                # Scrape full content
                print(f"Scraping content for (Custom/{source_name}): {title}...")
                article['content'] = self.scrape_article(link)
                
                if article['content']:
                    articles.append(article)
            
            return articles
            
        except Exception as e:
            print(f"Error fetching webpage {url}: {e}")
            return []
    
    def _extract_article_links(self, soup, base_url):
        """Extract article links from webpage using common patterns."""
        from urllib.parse import urljoin
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

