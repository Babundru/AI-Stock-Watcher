import config
from config import (CHECK_INTERVAL, WATCH_CHECK_INTERVAL, TARGET_COMPANIES,
                    PAPER_TRADING, PAPER_COST_PCT, PAPER_BENCHMARK)
from local_time import now_local
from news_collector import NewsCollector
from analyzer import MarketAnalyzer
from cloud_analyzer import CloudAnalyzer
from keyword_analyzer import KeywordAnalyzer
from notifier import Notifier
from ollama_manager import OllamaManager
from portfolio_manager import PortfolioManager
from watch_manager import WatchManager
from paper_trader import PaperTrader
import price_lookup
import strategy
from strategy import LONG, SHORT
import time
import datetime
import threading
import collections
import gc
import ctypes
import json
import os


def _process_rss_mb():
    """This process's resident memory in MB, or None where unavailable.

    Deliberately dependency-free (no psutil) and Linux-only in effect: it is
    for the always-on VM, where knowing whether the footprint is flat or
    climbing is the difference between a five-minute diagnosis and days of
    guesswork. A 1GB box that runs out of memory does not necessarily
    OOM-kill anything - it can just stop responding - so the growth has to be
    visible *before* that point, in the logs the dashboard already shows.
    """
    try:
        with open('/proc/self/statm', 'r') as f:
            pages = int(f.read().split()[1])
        return pages * os.sysconf('SC_PAGE_SIZE') / (1024 * 1024)
    except (OSError, ValueError, IndexError, AttributeError):
        return None


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
    def __init__(self, log_callback=print, alert_callback=None, status_callback=None,
                 portfolio_mgr=None, source_mgr=None):
        self.log_callback = log_callback
        # Called with a dict for every alert raised, so the UI can keep a
        # history instead of letting alerts scroll away in the log.
        self.alert_callback = alert_callback
        # Called with a short "what am I doing right now" string. Matters
        # because a single LLM analysis can take a minute with no other sign
        # of life.
        self.status_callback = status_callback
        self.stats_file = 'data/stats.json'
        self.stats = self._load_stats()
        self.running = False
        self.thread = None
        # Bumped on every start(). A scan loop only keeps going while it is
        # the current generation, so a stop() followed quickly by a start()
        # cannot leave two loops alive: the old thread may still be inside a
        # minute-long LLM call when the new one begins, and `running` alone
        # would be True again by the time it looks.
        self._generation = 0
        # The UI's own manager instances are passed in so both sides read and
        # write the same in-memory state. With separate instances, a holding
        # or source added in the dashboard reached the file but not the
        # running scan - no Owned tag, no portfolio context in the prompt, no
        # new source fetched - until a restart.
        self.collector = NewsCollector(source_mgr=source_mgr)
        # Three interchangeable engines, in priority order: a hosted AI API
        # (when a key is configured), a local Ollama model, or - needing
        # neither a key nor a model install - the offline keyword scorer.
        self.ollama = None
        self.analyzer = None
        self._build_analyzer()
        self.notifier = Notifier()
        self.portfolio_mgr = portfolio_mgr or PortfolioManager()
        self.watch_mgr = WatchManager()
        # Passive record of every watch's round trip, for judging whether the
        # alerts are actually worth acting on. Changes nothing about how the
        # app behaves - see paper_trader.py.
        self.paper = PaperTrader(cost_pct=PAPER_COST_PCT,
                                 benchmark=PAPER_BENCHMARK) if PAPER_TRADING else None
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

    @staticmethod
    def engine_name():
        """Which engine the current config selects, read live."""
        if config.USE_CLOUD_AI:
            return "cloud AI"
        if config.USE_LOCAL_LLM:
            return "local LLM"
        return "keyword matcher"

    @staticmethod
    def engine_description():
        """Human-readable engine label for the UIs."""
        if config.USE_CLOUD_AI:
            return f"Cloud AI · {config.CLOUD_AI_PROVIDER}/{config.CLOUD_AI_MODEL}"
        if config.USE_LOCAL_LLM:
            return f"Local AI · {config.LOCAL_MODEL_NAME}"
        return "Keyword scoring · offline"

    def _build_analyzer(self):
        """(Re)create the analysis engine from the current config."""
        want_ollama = config.USE_LOCAL_LLM and not config.USE_CLOUD_AI
        if self.ollama and not want_ollama:
            # Only shuts down a server we spawned; an external one is left.
            self.ollama.stop()
            self.ollama = None
        if want_ollama and not self.ollama:
            self.ollama = OllamaManager(log_callback=self.log)
            if self.running:
                self.ollama.start()

        if config.USE_CLOUD_AI:
            self.analyzer = CloudAnalyzer(ai_log_callback=self.log)
        elif config.USE_LOCAL_LLM:
            self.analyzer = MarketAnalyzer(ai_log_callback=self.log)
        else:
            self.analyzer = KeywordAnalyzer()

    def apply_settings(self):
        """Apply the current config module values to the running backend.

        Called after the dashboard saves settings (config.save_settings) or
        re-reads the file (config.reload_from_disk): the engine is rebuilt,
        the notifier picks up its topic/mute, and the paper ledger its cost.
        Takes effect on the next article - an analysis already in flight
        finishes on the engine it started with. No restart needed, which
        matters when the backend is a headless VM reached from a phone.
        """
        self._build_analyzer()
        self.notifier.apply_settings()
        if self.paper:
            self.paper.cost_pct = config.PAPER_COST_PCT
        self.log(f"Settings applied: engine = {self.engine_description()}")

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
    
    def _load_stats(self):
        """Load persisted scan/alert/skip counters from disk (survives a restart)."""
        defaults = {'scanned': 0, 'alerts': 0, 'skipped': 0}
        if os.path.exists(self.stats_file):
            try:
                with open(self.stats_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    defaults.update({k: data.get(k, v) for k, v in defaults.items()})
                    self.log(f"Loaded stats: {defaults}")
            except Exception as e:
                self.log(f"Error loading stats: {e}")
        return defaults

    def _save_stats(self):
        """Persist scan/alert/skip counters to disk."""
        try:
            with open(self.stats_file, 'w', encoding='utf-8') as f:
                json.dump(self.stats, f, indent=2)
        except Exception as e:
            self.log(f"Error saving stats: {e}")

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
        if self.running:
            return
        # Give a previous loop a moment to wind down so two cycles don't
        # overlap; if it is stuck in a long analysis the generation check
        # retires it at its next opportunity anyway.
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)
        self._generation += 1
        self.running = True
        # Bring the model server up before the first article arrives.
        # Attaches to an already-running Ollama rather than starting a second.
        if self.ollama:
            self.ollama.start()
        self.log(f"Monitoring started. Check interval: {CHECK_INTERVAL//60} minutes")
        self.thread = threading.Thread(target=self._run_loop, args=(self._generation,))
        self.thread.daemon = True
        self.thread.start()

    def stop(self):
        self.running = False
        # Save processed URLs and counters before stopping
        self._save_processed_urls()
        self._save_stats()
        # Only shuts down a server we spawned; an externally-started Ollama
        # is left alone.
        if self.ollama:
            self.ollama.stop()
        self.log("Monitoring stopped. Processed URLs saved.")

    def _alive(self, generation):
        """Whether the loop of this generation should keep going."""
        return self.running and self._generation == generation

    def _sleep(self, seconds, generation):
        """Sleep in 1s steps so stop() stays responsive."""
        for _ in range(int(seconds)):
            if not self._alive(generation):
                break
            time.sleep(1)

    def _run_loop(self, generation):
        self.log("Stocks Watcher Started...")

        # Send startup notification
        self.notifier.notify_system("Stocks Watcher Started", "The Stocks Watcher is now running and monitoring for news.")

        from config import GLOBAL_SCAN

        if GLOBAL_SCAN:
             self.log("🌍 GLOBAL MARKET SCAN: Enabled. Checking all major business sources.")
        else:
             self.log(f"Tracking companies: {', '.join(TARGET_COMPANIES)}")

        while self._alive(generation):
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
                    if not self._alive(generation): break
                    self._process_article("Custom Source News", article, market_open, is_discovery=True)


                # --- GLOBAL SCAN vs WATCHLIST ---
                if GLOBAL_SCAN and self._alive(generation):
                    self.log("Running Global Market Scan...")
                    # 1. Fetch General News
                    self.status("Fetching market news")
                    articles = self.collector.fetch_general_market_news()
                    for article in articles:
                        if not self._alive(generation): break
                        # Hint "General Market" so the analyzer identifies the entity itself
                        self._process_article("General Market News", article, market_open, is_discovery=True)

                # We can also still check specific targets if they might not show up in top headlines?
                # For rate limit safety, if Global Scan is on, we might skip the targeted specific loop
                # OR we just rely on Global Scan finding them.
                # Let's keep specific checks ONLY if Global Scan is OFF or if list is small.
                if not GLOBAL_SCAN:
                    for company in TARGET_COMPANIES:
                        if not self._alive(generation): break
                        articles = self.collector.fetch_news(company)
                        for article in articles:
                            if not self._alive(generation): break
                            self._process_article(company, article, market_open)

                if not self._alive(generation):
                    break

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

                rss = _process_rss_mb()
                if rss is not None:
                    self.log(f"   💾 Memory in use: {rss:.0f} MB")

                # --- SMART SCHEDULER ---
                # Calculate sleep time until next 15-minute mark (xx:00, xx:15, xx:30, xx:45)
                # To sync with device time.
                # Sleep for the configured check interval
                next_run_time = now_local() + datetime.timedelta(seconds=CHECK_INTERVAL)
                self.log(f"Sleeping until {next_run_time.strftime('%H:%M:%S')} ({CHECK_INTERVAL}s)...")
                self.status(f"Waiting until {next_run_time.strftime('%H:%M:%S')}")
                
                self._sleep(CHECK_INTERVAL, generation)

            except Exception as e:
                self.log(f"Error in main loop: {e}")
                # Same responsive sleep as the normal path: a flat 60s
                # sleep here made Stop take up to a minute after an error.
                self._sleep(60, generation)

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
            self._save_stats()
            self.articles_since_save = 0

        
        # Get portfolio tickers for context
        portfolio_tickers = list(self.portfolio_mgr.get_portfolio().keys())
        
        # Analyze
        self.log(f"   🔍 Analyzing with {self.engine_name()}...")
        self.stats['scanned'] += 1
        self.status(f"Analyzing: {title[:48]}")
        analysis = self.analyzer.analyze_article(company_hint, article, market_is_open, portfolio_tickers)
        if not analysis:
            self.log(f"   ⊘ No analysis results (article may not match criteria)")
            self.stats['skipped'] += 1
            self.status("Idle")
            return
        
        # Extract info
        target = analysis.get('target_company') or company_hint
        # "NASDAQ: TSLA" / "$tsla" / "BRK.B" -> "TSLA" / "TSLA" / "BRK-B".
        # Everything downstream (portfolio lookup, watch, price) keys off
        # the clean form.
        ticker = price_lookup.normalize_ticker(analysis.get('ticker'))
        sentiment = (analysis.get('sentiment') or 'NEUTRAL').upper()
        impact = (analysis.get('impact') or 'LOW').upper()
        prediction = (analysis.get('prediction') or 'FLAT').upper()
        confidence = strategy.parse_confidence(analysis.get('confidence'))
        expected_pct = strategy.parse_pct(analysis.get('expected_move_pct'))

        # Normalize ticker if possible, else use name
        stock_id = ticker if ticker else (target or '')
        is_owned = self.portfolio_mgr.has_stock(stock_id)

        self.log(f"[{target}] Analyzing: Sentiment={sentiment}, Impact={impact}, "
                 f"Prediction={prediction}, Confidence={_fmt(confidence)}, "
                 f"Expected move={_fmt_pct(expected_pct)}")

        skip_reasons = self._alert_skip_reasons(analysis, sentiment, impact, prediction, confidence)
        if skip_reasons:
            self.log(f"  ⊘ No notification sent: {', '.join(skip_reasons)}")
            self.stats['skipped'] += 1
            self.status("Idle")
            return

        direction = LONG if sentiment == 'POSITIVE' else SHORT

        # News that contradicts an open position closes it first - whatever
        # happens next. The old code ignored it ("already has an open
        # position"), throwing away the most useful exit signal the app had.
        if ticker:
            self._close_on_reversal(ticker, direction)

        # Negative news on a stock you don't hold is a short setup; on one
        # you do hold it is a risk warning, which is always worth sending.
        is_short_setup = direction == SHORT and not is_owned
        if is_short_setup and not config.ALLOW_SHORTS and not config.NOTIFY_SHORTS:
            self.log(f"  ⊘ Negative news on {stock_id}, which you don't hold - short selling "
                     f"is off in settings")
            self.stats['skipped'] += 1
            self.status("Idle")
            return

        if direction == LONG:
            self.log(f"  ✓ Positive sentiment detected - potential opportunity")
        elif is_owned:
            self.log(f"  ⚠️ WARNING: Negative news for portfolio stock {stock_id}")
        else:
            self.log(f"  📉 MARKET ALERT: Negative news for {stock_id} (Not in Portfolio - potential short opportunity)")

        decision = self._decide_trade(ticker, target, direction, impact, prediction,
                                      expected_pct, confidence, analysis, article, url, title)
        if decision['opened']:
            watch = decision['watch']
            verb = 'BUY' if direction == LONG else 'SHORT'
            self.log(f"  {verb} {ticker} at {watch['entry_price']:.2f} -> target "
                     f"{watch['target_price']:.2f}, stop {watch['stop_loss_price']:.2f}, "
                     f"time exit {watch['expires_at'][:16].replace('T', ' ')}")
        else:
            self.log(f"  ✗ No trade: {decision['reason']}")

        self.log(f"🚀 ALERT: {target} ({sentiment}) - {analysis.get('explanation')}")
        if is_short_setup and not config.NOTIFY_SHORTS:
            self.log("  (short setups are muted in settings - no phone notification)")
        else:
            self.notifier.notify(target, article, analysis, is_owned=is_owned, decision=decision)

        self.stats['alerts'] += 1
        self._save_stats()
        if self.alert_callback:
            try:
                self.alert_callback({
                    'time': now_local(),
                    'company': target,
                    'ticker': ticker,
                    'sentiment': sentiment,
                    'impact': impact,
                    'prediction': prediction,
                    'confidence': confidence,
                    'explanation': analysis.get('explanation') or '',
                    'headline': title,
                    'url': url,
                    'is_owned': is_owned,
                    'trade': {k: v for k, v in decision.items() if k != 'watch'},
                })
            except Exception as e:
                self.log(f"  (alert view update failed: {e})")

        self.status("Idle")

    @staticmethod
    def _alert_skip_reasons(analysis, sentiment, impact, prediction, confidence):
        """Why this analysis should not raise an alert at all; empty if it
        should. Flags a model left out (the keyword engine has none of them)
        never count against an alert."""
        reasons = []
        if sentiment not in ('POSITIVE', 'NEGATIVE'):
            reasons.append(f"sentiment is {sentiment} (neutral)")
        # Threshold read live from the module, not captured at import: the
        # sensitivity slider rewrites it and a running watcher must pick the
        # change up without a restart.
        if not config.impact_passes(impact):
            reasons.append(f"impact is {impact} (need {config.MIN_IMPACT} or above)")
        if 'FLAT' in prediction:
            reasons.append("prediction is FLAT")
        if strategy.parse_flag(analysis.get('is_new_information')) is False:
            reasons.append("not new information (a recap or commentary)")
        if strategy.parse_flag(analysis.get('is_company_specific')) is False:
            reasons.append("not company-specific (market/sector news)")
        if confidence is not None and confidence < config.MIN_CONFIDENCE:
            reasons.append(f"confidence {confidence} is below {config.MIN_CONFIDENCE}")
        return reasons

    def _decide_trade(self, ticker, company, direction, impact, prediction,
                      expected_pct, confidence, analysis, article, url, title):
        """Turn an alert into a position, or say why not.

        Cheapest checks first: the price context and the AI trade check each
        cost a request, so they only run for alerts that could still become
        a trade. Returns a dict - 'opened', 'reason', and when the trade was
        at least sized, its entry/target/stop (a short that is notified but
        not paper-traded still shows its setup in the notification).
        """
        decision = {'opened': False, 'direction': direction, 'reason': None, 'watch': None}

        def no(reason):
            decision['reason'] = reason
            return decision

        if not ticker:
            return no("no tradable ticker")
        if direction == SHORT:
            if not config.ALLOW_SHORTS and not config.NOTIFY_SHORTS:
                return no("short selling is off in settings")
            if config.impact_rank(impact) < config.impact_rank(config.SHORT_MIN_IMPACT):
                return no(f"shorts need {config.SHORT_MIN_IMPACT} impact")
        if self.watch_mgr.has_open_watch(ticker):
            return no(f"already holding a position in {ticker}")
        tracked = direction == LONG or config.ALLOW_SHORTS
        if tracked and self.watch_mgr.open_count() >= config.MAX_OPEN_POSITIONS:
            return no(f"position limit reached ({config.MAX_OPEN_POSITIONS} open)")

        self.status(f"Checking price action: {ticker}")
        context = price_lookup.fetch_context(ticker, article.get('published_ts'))
        self.log(f"   📈 {_describe_context(context)}")
        plan = strategy.plan_trade(direction, impact, expected_pct, context)
        if not plan['ok']:
            return no(plan['reason'])

        horizon = analysis.get('horizon')
        ai_confirmed = None
        if config.AI_TRADE_CONFIRM and hasattr(self.analyzer, 'confirm_trade'):
            self.status(f"AI trade check: {ticker}")
            verdict = self.analyzer.confirm_trade(article, analysis, context, direction)
            if not verdict:
                # A provider hiccup shouldn't silently stop all trading; the
                # hard rules above have already passed.
                self.log("   (AI trade check unavailable - deciding on the rules alone)")
            else:
                take = strategy.parse_flag(verdict.get('take_trade'))
                v_conf = strategy.parse_confidence(verdict.get('confidence'))
                self.log(f"   🤖 Bull case: {verdict.get('bull_case')}")
                self.log(f"   🤖 Bear case: {verdict.get('bear_case')}")
                self.log(f"   🤖 Verdict: {'take it' if take else 'pass'} "
                         f"(confidence {_fmt(v_conf)}) - {verdict.get('reason')}")
                if take is not True:
                    return no(f"AI trade check passed on it - {verdict.get('reason') or 'no edge left'}")
                if v_conf is not None and v_conf < config.MIN_CONFIDENCE:
                    return no(f"AI trade check confidence {v_conf} is below {config.MIN_CONFIDENCE}")
                ai_confirmed = True
                if v_conf is not None:
                    confidence = v_conf
                remaining = strategy.parse_pct(verdict.get('expected_remaining_move_pct'))
                if remaining:
                    plan = strategy.plan_trade(direction, impact, expected_pct, context,
                                               remaining_pct=remaining)
                    if not plan['ok']:
                        return no(plan['reason'])
                if (verdict.get('horizon') or '').upper() in strategy.HORIZON_TRADING_DAYS:
                    horizon = verdict['horizon'].upper()

        # Entry priced the same way every watch check prices it, with the
        # benchmark riding along in the same batched call.
        wanted = [ticker] + ([PAPER_BENCHMARK] if self.paper else [])
        prices = price_lookup.fetch_prices(wanted)
        entry = prices.get(ticker) or context.get('price')
        if not entry:
            return no(f"couldn't price {ticker}")

        decision.update(
            entry_price=entry,
            target_price=round(strategy.price_at_gain(direction, entry, plan['target_pct']), 4),
            stop_price=round(strategy.price_at_gain(direction, entry, -plan['stop_pct']), 4),
            target_pct=plan['target_pct'],
            stop_pct=plan['stop_pct'],
            confidence=confidence,
        )
        if not tracked:
            return no("shorts aren't paper-traded (off in settings), so no cover signal will follow")

        watch = self.watch_mgr.add_watch(
            ticker, company, entry, impact, horizon, prediction,
            article_url=url, article_headline=title, direction=direction,
            target_pct=plan['target_pct'], stop_pct=plan['stop_pct'],
            extra={
                'confidence': confidence,
                'ai_confirmed': ai_confirmed,
                'expected_move_pct': plan['expected_move_pct'],
                'already_moved_pct': plan['already_moved_pct'],
                'atr_pct': plan['atr_pct'],
            },
        )
        if not watch:
            return no(f"already holding a position in {ticker}")
        if self.paper:
            self.paper.open_trade(watch, prices.get(PAPER_BENCHMARK))
        decision.update(opened=True, watch=watch, expires_at=watch['expires_at'])
        return decision

    def _close_on_reversal(self, ticker, direction):
        """Close an open position in `ticker` facing the other way from new
        news that has just passed the alert filters."""
        for watch in self.watch_mgr.get_open_watches():
            if watch['ticker'] != ticker or watch.get('direction', LONG) == direction:
                continue
            wanted = [ticker] + ([PAPER_BENCHMARK] if self.paper else [])
            prices = price_lookup.fetch_prices(wanted)
            price = prices.get(ticker)
            if not price:
                self.log(f"  (news contradicts the open {watch['direction']} on {ticker}, "
                         f"but it couldn't be priced - left to the next watch check)")
                return
            self.log(f"  ↩ New {'positive' if direction == LONG else 'negative'} news contradicts "
                     f"the open {watch['direction']} on {ticker} - closing it at {price:.2f}")
            self._close_position(watch, 'news_reversal', price,
                                 prices.get(PAPER_BENCHMARK), now_local())

    def _close_position(self, watch, reason, price, benchmark_price, now):
        """Close a watch, record the paper trade and send the exit signal."""
        closed = self.watch_mgr.close_watch(watch['id'], reason, price)
        if not closed:
            return

        if self.paper:
            trade = self.paper.close_trade(closed, price, benchmark_price)
            if trade:
                self.log(f"  📒 Paper trade #{len(self.paper.closed())}: "
                         f"{trade['net_pct'] * 100:+.2f}% after costs")

        direction = watch.get('direction', LONG)
        signal = 'COVER SHORT' if direction == SHORT else 'SELL'
        self.log(f"💰 {signal} SIGNAL: {watch['company']} ({watch['ticker']}) - {reason}")
        if direction == SHORT and not config.NOTIFY_SHORTS:
            self.log("  (short notifications are muted in settings)")
        else:
            self.notifier.notify_sell(
                watch['ticker'], watch['company'], reason,
                watch['entry_price'], price, watch['target_price'],
                article_url=watch.get('article_url'),
                direction=direction,
            )

        if self.alert_callback:
            try:
                self.alert_callback({
                    'time': now,
                    'kind': 'sell_signal',
                    'company': watch['company'],
                    'ticker': watch['ticker'],
                    'direction': direction,
                    'reason': reason,
                    'entry_price': watch['entry_price'],
                    'current_price': price,
                    'target_price': watch['target_price'],
                    'headline': watch.get('article_headline'),
                    'url': watch.get('article_url'),
                })
            except Exception as e:
                self.log(f"  (alert view update failed: {e})")

    def _check_watches(self):
        """Check every open watch's current price against its exits and close
        + notify any that fire - a sell signal for longs, a buy-back signal
        for shorts. The rules are in strategy.py."""
        open_watches = self.watch_mgr.get_open_watches()
        if not open_watches:
            return

        self.log(f"👀 Checking {len(open_watches)} open watch(es) for exit signals...")
        tickers = list({w['ticker'] for w in open_watches})
        if self.paper:
            tickers.append(PAPER_BENCHMARK)
        prices = price_lookup.fetch_prices(tickers)
        benchmark_price = prices.get(PAPER_BENCHMARK) if self.paper else None
        now = now_local()
        # Time exits wait for the regular session: fired at 3am they would
        # close on a stale after-hours print nobody could trade at. Price
        # exits (stops) fire whenever a price is there.
        market_open = strategy.us_market_open()
        cost = self.paper.cost_pct if self.paper else config.PAPER_COST_PCT

        for watch in open_watches:
            price = prices.get(watch['ticker'])
            if not price:
                continue

            # Note how far this position has run either way before deciding
            # whether it resolves - the worst point a trade passed through is
            # what a later stop-loss study needs, and it is unrecoverable if
            # not captured live.
            if self.paper:
                self.paper.mark_price(watch['id'], price)

            reason, target_reached = self.watch_mgr.update_exit(watch, price, cost)
            if not reason and market_open:
                if self.watch_mgr.over_age_limit(watch, now):
                    reason = 'max_age'
                    self.log(f"  📅 {watch['company']} ({watch['ticker']}) open past the age "
                             f"limit - closing at {price:.2f}")
                elif now >= _parse_time(watch.get('expires_at'), now):
                    reason = 'horizon_expired'

            if target_reached and not reason:
                self._notify_target_reached(watch, price)
            if reason:
                self._close_position(watch, reason, price, benchmark_price, now)

        # One write for the whole pass: update_exit and mark_price only
        # mutate in memory, so without this every still-open position's
        # ratcheted stop and excursions would be lost on restart.
        self.watch_mgr.save()
        if self.paper:
            self.paper.save()

        # The price call above is the single largest allocation this process
        # makes (yfinance/pandas frames), and it runs on a coarser cadence
        # than the news scan's own _release_memory() at the end of a cycle.
        # Without trimming here the high-water mark of the last price fetch
        # stays resident until the next scan finishes.
        _release_memory()

    def _notify_target_reached(self, watch, price):
        """A let-it-run position reached its target: it stays open, with its
        stop now trailing close behind. Worth telling the user - their
        broker-side stop should follow."""
        direction = watch.get('direction', LONG)
        self.log(f"  🎯 {watch['company']} ({watch['ticker']}) reached its target at "
                 f"{price:.2f} - holding, trailing stop now {watch['stop_loss_price']:.2f}")
        if direction == SHORT and not config.NOTIFY_SHORTS:
            return
        self.notifier.notify_target_reached(
            watch['ticker'], watch['company'], direction, watch['entry_price'],
            price, watch['stop_loss_price'], article_url=watch.get('article_url'))


def _parse_time(value, fallback):
    """ISO timestamp -> aware datetime; `fallback` (normally now, i.e. "due")
    for a missing or unreadable one, so a damaged record still gets its
    time exit instead of raising inside the watch pass."""
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=fallback.tzinfo)
    return parsed


def _fmt(value):
    return "n/a" if value is None else str(value)


def _fmt_pct(value):
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _describe_context(ctx):
    """One log line of the price context a trade decision was made on."""
    if not ctx:
        return "No price context available"
    parts = []
    if ctx.get('price') is not None:
        parts.append(f"price {ctx['price']:.2f}")
    if ctx.get('change_since_close_pct') is not None:
        parts.append(f"{ctx['change_since_close_pct'] * 100:+.1f}% since close")
    if ctx.get('change_since_publish_pct') is not None:
        parts.append(f"{ctx['change_since_publish_pct'] * 100:+.1f}% since published")
    if ctx.get('atr_pct') is not None:
        parts.append(f"daily range {ctx['atr_pct'] * 100:.1f}%")
    return "Price context: " + ", ".join(parts)
