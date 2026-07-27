from config import CHECK_INTERVAL, TARGET_COMPANIES
from news_collector import NewsCollector
from keyword_analyzer import KeywordAnalyzer
from notifier import Notifier
from portfolio_manager import PortfolioManager
import time
import datetime
import threading
import collections
import json
import os

class StockAppBackend:
    def __init__(self, log_callback=print):
        self.log_callback = log_callback
        self.running = False
        self.collector = NewsCollector()
        self.analyzer = KeywordAnalyzer()
        self.notifier = Notifier()
        self.portfolio_mgr = PortfolioManager()
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

    def start(self):
        self.running = True
        self.log(f"Monitoring started. Check interval: {CHECK_INTERVAL//60} minutes")
        self.thread = threading.Thread(target=self._run_loop)
        self.thread.daemon = True
        self.thread.start()

    def stop(self):
        self.running = False
        # Save processed URLs before stopping
        self._save_processed_urls()
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
                self.log(f"\nScanning for news at {datetime.datetime.now().strftime('%H:%M:%S')}...")
                
                market_open = self.notifier.is_market_open()
                status_msg = "OPEN" if market_open else "CLOSED"
                self.log(f"Market Status: {status_msg}")

                # --- CUSTOM SOURCES (Priority) ---
                # First, check custom user-defined sources
                self.log("🔗 Checking custom sources...")
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

                # --- SMART SCHEDULER ---
                # Calculate sleep time until next 15-minute mark (xx:00, xx:15, xx:30, xx:45)
                # To sync with device time.
                # Sleep for the configured check interval
                next_run_time = datetime.datetime.now() + datetime.timedelta(seconds=CHECK_INTERVAL)
                self.log(f"Sleeping until {next_run_time.strftime('%H:%M:%S')} ({CHECK_INTERVAL}s)...")
                
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
        self.log(f"   🔍 Analyzing with keyword matcher...")
        analysis = self.analyzer.analyze_article(company_hint, article, market_is_open, portfolio_tickers)
        if not analysis:
            self.log(f"   ⊘ No analysis results (article may not match criteria)")
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
        else:
            reason_str = ", ".join(skip_reasons) if skip_reasons else "unknown reason"
            self.log(f"  ⊘ No notification sent: {reason_str}")


