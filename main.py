from config import CHECK_INTERVAL, WATCH_CHECK_INTERVAL, TARGET_COMPANIES, USE_LOCAL_LLM, USE_CLOUD_AI
from local_time import now_local
from news_collector import NewsCollector
from analyzer import MarketAnalyzer
from cloud_analyzer import CloudAnalyzer
from keyword_analyzer import KeywordAnalyzer
from notifier import Notifier
from ollama_manager import OllamaManager
from portfolio_manager import PortfolioManager
from watch_manager import WatchManager
import price_lookup
import time
import datetime
import threading
import collections
import gc
import ctypes
import json
import os


def _release_memory():
    """Free a finished scan cycle's garbage and hand it back to the OS.

    gc.collect() alone only returns memory to Python's own allocator; on
    glibc the freed arenas can stay mapped to the process, so RSS keeps
    showing the high-water mark of the busiest cycle even while the app sits
    idle. malloc_trim(0) is what actually releases them - it matters on a
    1GB VM and is a no-op everywhere else, hence the best-effort wrapper
    (there is no libc.so.6 on Windows/macOS, where the desktop GUI runs).
    """
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


class StockAppBackend:
    def __init__(self, log_callback=print, alert_callback=None, status_callback=None):
        self.log_callback = log_callback
        # Called with a dict for every alert raised, so the UI can keep a
        # history instead of letting alerts scroll away in the log.
        self.alert_callback = alert_callback
        # Called with a short "what am I doing right now" string. Matters
        # because a single LLM analysis can take a minute with no other sign
        # of life.
        self.status_callback = status_callback
        self.stats = {'scanned': 0, 'alerts': 0, 'skipped': 0}
        self.running = False
        self.collector = NewsCollector()
        # Three interchangeable engines, in priority order: the Anthropic API
        # (when a key is configured), a local Ollama model, or - needing
        # neither a key nor a model install - the offline keyword scorer.
        if USE_CLOUD_AI:
            self.ollama = None
            self.analyzer = CloudAnalyzer(ai_log_callback=self.log)
        elif USE_LOCAL_LLM:
            self.ollama = OllamaManager(log_callback=self.log)
            self.analyzer = MarketAnalyzer(ai_log_callback=self.log)
        else:
            self.ollama = None
            self.analyzer = KeywordAnalyzer()
        self.notifier = Notifier()
        self.portfolio_mgr = PortfolioManager()
        self.watch_mgr = WatchManager()
        self._last_watch_check = 0
        self.processed_urls_file = 'data/processed_urls.json'
        self.max_stored_urls = 120  # Keep only last 120 processed URLs
        self.processed_urls = self._load_processed_urls()
        # Mirror of the deque for O(1) lookups; the deque owns eviction order.
        self.processed_set = set(self.processed_urls)
        self.articles_since_save = 0
        # stop() runs on the GUI thread and serializes these while the worker
        # thread may be appending, which can raise "deque mutated during
        # iteration" and lose the file.
        self.urls_lock = threading.Lock()
        # Let the collector skip re-downloading articles we've already analysed
        self.collector.is_seen = self.processed_set.__contains__

    def _load_processed_urls(self):
        """Load previously processed URLs from disk (last 120 only)."""
        if os.path.exists(self.processed_urls_file):
            try:
                with open(self.processed_urls_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    
                    # If data is a list, load directly
                    if isinstance(data, list):
                        # Keep only last 120 URLs
                        urls = collections.deque(data[-self.max_stored_urls:], maxlen=self.max_stored_urls)
                        self.log(f"Loaded {len(urls)} previously processed URLs")
                        return urls
                    
                    # If data is dict (old format), convert to list
                    elif isinstance(data, dict):
                        # Sort by timestamp and take most recent
                        sorted_urls = sorted(data.items(), key=lambda x: x[1])
                        urls = [url for url, _ in sorted_urls[-self.max_stored_urls:]]
                        urls_deque = collections.deque(urls, maxlen=self.max_stored_urls)
                        self.log(f"Loaded {len(urls_deque)} previously processed URLs (converted from old format)")
                        return urls_deque
                    
            except Exception as e:
                self.log(f"Error loading processed URLs: {e}")
                return collections.deque(maxlen=self.max_stored_urls)
        else:
            return collections.deque(maxlen=self.max_stored_urls)
    
    def _save_processed_urls(self):
        """Save processed URLs to disk (last 120 only)."""
        try:
            # Convert deque to list for JSON serialization
            with self.urls_lock:
                urls_list = list(self.processed_urls)

            with open(self.processed_urls_file, 'w', encoding='utf-8') as f:
                json.dump(urls_list, f, indent=2)
                
            self.log(f"Saved {len(urls_list)} processed URLs to disk")
        except Exception as e:
            self.log(f"Error saving processed URLs: {e}")
    
    def log(self, message):
        if self.log_callback:
            self.log_callback(message)

    def status(self, message):
        """Report current activity to the UI (best effort)."""
        if self.status_callback:
            try:
                self.status_callback(message, dict(self.stats))
            except Exception:
                pass

    def start(self):
        self.running = True
        # Bring the model server up before the first article arrives.
        # Attaches to an already-running Ollama rather than starting a second.
        if self.ollama:
            self.ollama.start()
        self.log(f"Monitoring started. Check interval: {CHECK_INTERVAL//60} minutes")
        self.thread = threading.Thread(target=self._run_loop)
        self.thread.daemon = True
        self.thread.start()

    def stop(self):
        self.running = False
        # Save processed URLs before stopping
        self._save_processed_urls()
        # Only shuts down a server we spawned; an externally-started Ollama
        # is left alone.
        if self.ollama:
            self.ollama.stop()
        self.log("Monitoring stopped. Processed URLs saved.")

    def _run_loop(self):
        self.log("Stocks Watcher Started...")
        
        # Send startup notification
        self.notifier.notify_system("Stocks Watcher Started", "The Stocks Watcher is now running and monitoring for news.")

        from config import GLOBAL_SCAN
        
        if GLOBAL_SCAN:
             self.log("🌍 GLOBAL MARKET SCAN: Enabled. Checking all major business sources.")
        else:
             self.log(f"Tracking companies: {', '.join(TARGET_COMPANIES)}")
        
        while self.running:
            try:
                self.log(f"\nScanning for news at {now_local().strftime('%H:%M:%S')}...")
                
                market_open = self.notifier.is_market_open()
                status_msg = "OPEN" if market_open else "CLOSED"
                self.log(f"Market Status: {status_msg}")

                # --- CUSTOM SOURCES (Priority) ---
                # First, check custom user-defined sources
                self.log("🔗 Checking custom sources...")
                self.status("Fetching custom sources")
                custom_articles = self.collector.fetch_from_custom_sources()
                self.log(f"   Found {len(custom_articles)} articles from custom sources")
                
                # No pacing delay needed here: fetching and scraping already
                # happened inside the collector, so this loop is pure local
                # keyword analysis. The old sleeps paced Gemini/API calls that
                # no longer exist, and made a cycle outlast CHECK_INTERVAL.
                for article in custom_articles:
                    if not self.running: break
                    self._process_article("Custom Source News", article, market_open, is_discovery=True)


                # --- GLOBAL SCAN vs WATCHLIST ---
                if GLOBAL_SCAN:
                    self.log("Running Global Market Scan...")
                    # 1. Fetch General News
                    self.status("Fetching market news")
                    articles = self.collector.fetch_general_market_news()
                    for article in articles:
                        if not self.running: break
                        # Hint "General Market" so the analyzer identifies the entity itself
                        self._process_article("General Market News", article, market_open, is_discovery=True)
                
                # We can also still check specific targets if they might not show up in top headlines?
                # For rate limit safety, if Global Scan is on, we might skip the targeted specific loop 
                # OR we just rely on Global Scan finding them. 
                # Let's keep specific checks ONLY if Global Scan is OFF or if list is small.
                if not GLOBAL_SCAN:
                    for company in TARGET_COMPANIES:
                        if not self.running: break
                        articles = self.collector.fetch_news(company)
                        for article in articles:
                            if not self.running: break
                            self._process_article(company, article, market_open)

                # --- WATCH CHECK (sell signals) ---
                # Coarser cadence than the news scan - price doesn't need to
                # be polled every minute, and it's a batched API call per
                # open watch.
                if time.time() - self._last_watch_check >= WATCH_CHECK_INTERVAL:
                    self._check_watches()
                    self._last_watch_check = time.time()

                # --- RECLAIM MEMORY ---
                # A cycle churns through a lot of short-lived HTML and parse
                # trees. Collecting here, at the one moment per minute when
                # none of it is still referenced, keeps the process from
                # growing steadily on a small always-on VM. Once per
                # CHECK_INTERVAL the cost is irrelevant next to the scan
                # itself, and this is deliberately not left to the automatic
                # collector, whose thresholds trigger on allocation counts
                # rather than at a point where a whole cycle's garbage has
                # just gone unreachable at once.
                _release_memory()

                # --- SMART SCHEDULER ---
                # Calculate sleep time until next 15-minute mark (xx:00, xx:15, xx:30, xx:45)
                # To sync with device time.
                # Sleep for the configured check interval
                next_run_time = now_local() + datetime.timedelta(seconds=CHECK_INTERVAL)
                self.log(f"Sleeping until {next_run_time.strftime('%H:%M:%S')} ({CHECK_INTERVAL}s)...")
                self.status(f"Waiting until {next_run_time.strftime('%H:%M:%S')}")
                
                # Sleep loop for responsiveness
                for _ in range(CHECK_INTERVAL):
                    if not self.running: 
                        break
                    time.sleep(1)
                
            except Exception as e:
                self.log(f"Error in main loop: {e}")
                time.sleep(60)

    def _process_article(self, company_hint, article, market_is_open, is_discovery=False):
        url = article.get('url')
        title = article.get('title', 'No Title')
        
        if not url:
            self.log(f"⊘ Skipping article (no URL): {title[:60]}...")
            return
        
        # Check if already processed
        if url in self.processed_set:
            self.log(f"⊘ Already processed: {title[:60]}...")
            return  # Already processed

        self.log(f"\n📰 Processing article: {title}")
        self.log(f"   URL: {url[:80]}...")

        # Add to processed deque (automatically maintains 120 URL limit).
        # Once the deque is full, appending evicts the oldest entry - drop that
        # from the mirror set too so the two stay in sync.
        with self.urls_lock:
            evicted = self.processed_urls[0] if len(self.processed_urls) == self.max_stored_urls else None
            self.processed_urls.append(url)
            if evicted is not None:
                self.processed_set.discard(evicted)
            self.processed_set.add(url)

        # Periodic save to disk (every 10 articles to reduce I/O)
        self.articles_since_save += 1
        if self.articles_since_save >= 10:
            self._save_processed_urls()
            self.articles_since_save = 0

        
        # Get portfolio tickers for context
        portfolio_tickers = list(self.portfolio_mgr.get_portfolio().keys())
        
        # Analyze
        engine = "cloud AI" if USE_CLOUD_AI else ("local LLM" if USE_LOCAL_LLM else "keyword matcher")
        self.log(f"   🔍 Analyzing with {engine}...")
        self.stats['scanned'] += 1
        self.status(f"Analyzing: {title[:48]}")
        analysis = self.analyzer.analyze_article(company_hint, article, market_is_open, portfolio_tickers)
        if not analysis:
            self.log(f"   ⊘ No analysis results (article may not match criteria)")
            self.stats['skipped'] += 1
            self.status("Idle")
            return
        
        # Extract info
        target = analysis.get('target_company', company_hint)
        ticker = analysis.get('ticker')
        sentiment = (analysis.get('sentiment') or 'NEUTRAL').upper()
        impact = (analysis.get('impact') or 'LOW').upper()
        prediction = (analysis.get('prediction') or 'FLAT').upper()
        
        # --- PORTFOLIO LOGIC ---
        # Rule: Positive news -> Notify Always (buying opportunities)
        # Rule: Negative news -> Notify Always (risks and shorting opportunities)
        # Rule: Only notify HIGH or CRITICAL impact to reduce noise
        
        should_notify = False
        skip_reasons = []
        
        # Normalize ticker if possible, else use name
        stock_id = ticker if ticker else target
        
        self.log(f"[{target}] Analyzing: Sentiment={sentiment}, Impact={impact}, Prediction={prediction}")
        
        if sentiment == 'POSITIVE':
            should_notify = True  # Always notify for opportunities
            self.log(f"  ✓ Positive sentiment detected - potential opportunity")
        elif sentiment == 'NEGATIVE':
            should_notify = True  # Always notify for risks (changed from portfolio-only)
            if not self.portfolio_mgr.has_stock(stock_id):
                self.log(f"  📉 MARKET ALERT: Negative news for {stock_id} (Not in Portfolio - potential short opportunity)")
            else:
                self.log(f"  ⚠️ WARNING: Negative news for portfolio stock {stock_id}")
        else:
            skip_reasons.append(f"sentiment is {sentiment} (neutral)")
        
        if impact not in ['CRITICAL', 'HIGH']:
            if should_notify:
                self.log(f"  ✗ Impact too low ({impact}) - notification cancelled")
            skip_reasons.append(f"impact is {impact} (need HIGH/CRITICAL)")
            should_notify = False  # Filter low impact noise
            
        # Extra Safety: Ignore "FLAT" predictions even if some other signal was high
        prediction = (analysis.get('prediction') or '').upper()
        if 'FLAT' in prediction:
            if should_notify:
                self.log(f"  ✗ Prediction is FLAT - notification cancelled")
            skip_reasons.append("prediction is FLAT")
            should_notify = False

        if should_notify:
            self.log(f"🚀 ALERT: {target} ({sentiment}) - {analysis.get('explanation')}")
            # Pass ownership info to notifier
            is_owned = self.portfolio_mgr.has_stock(stock_id)
            self.notifier.notify(target, article, analysis, is_owned=is_owned)

            # Open a watch so we can later tell the user when to sell: only
            # for POSITIVE (buy-signal) alerts, and only if we can price it.
            if sentiment == 'POSITIVE' and ticker:
                entry_price = price_lookup.fetch_prices([ticker]).get(ticker)
                if entry_price:
                    self.watch_mgr.add_watch(
                        ticker, target, entry_price, impact,
                        analysis.get('horizon'), prediction,
                        article_url=url, article_headline=title,
                    )
                else:
                    self.log(f"  (couldn't price {ticker} - no sell watch opened)")

            self.stats['alerts'] += 1
            if self.alert_callback:
                try:
                    self.alert_callback({
                        'time': now_local(),
                        'company': target,
                        'ticker': ticker,
                        'sentiment': sentiment,
                        'impact': impact,
                        'prediction': prediction,
                        'explanation': analysis.get('explanation') or '',
                        'headline': title,
                        'url': url,
                        'is_owned': is_owned,
                    })
                except Exception as e:
                    self.log(f"  (alert view update failed: {e})")
        else:
            reason_str = ", ".join(skip_reasons) if skip_reasons else "unknown reason"
            self.log(f"  ⊘ No notification sent: {reason_str}")
            self.stats['skipped'] += 1

        self.status("Idle")

    def _check_watches(self):
        """Check every open watch's current price against its target/expiry
        and close+notify any that have resolved (see watch_manager.py)."""
        open_watches = self.watch_mgr.get_open_watches()
        if not open_watches:
            return

        self.log(f"👀 Checking {len(open_watches)} open watch(es) for sell signals...")
        tickers = list({w['ticker'] for w in open_watches})
        prices = price_lookup.fetch_prices(tickers)
        now = now_local()

        for watch in open_watches:
            price = prices.get(watch['ticker'])
            if not price:
                continue

            expires_at = datetime.datetime.fromisoformat(watch['expires_at'])
            reason = None
            if price >= watch['target_price']:
                reason = 'target_hit'
            elif now >= expires_at:
                reason = 'horizon_expired'

            if not reason:
                continue

            closed = self.watch_mgr.close_watch(watch['id'], reason, price)
            if not closed:
                continue

            self.log(f"💰 SELL SIGNAL: {watch['company']} ({watch['ticker']}) - {reason}")
            self.notifier.notify_sell(
                watch['ticker'], watch['company'], reason,
                watch['entry_price'], price, watch['target_price'],
                article_url=watch.get('article_url'),
            )

            if self.alert_callback:
                try:
                    self.alert_callback({
                        'time': now,
                        'kind': 'sell_signal',
                        'company': watch['company'],
                        'ticker': watch['ticker'],
                        'reason': reason,
                        'entry_price': watch['entry_price'],
                        'current_price': price,
                        'target_price': watch['target_price'],
                        'headline': watch.get('article_headline'),
                        'url': watch.get('article_url'),
                    })
                except Exception as e:
                    self.log(f"  (alert view update failed: {e})")


