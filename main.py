import config
from config import (CHECK_INTERVAL, WEEKEND_CHECK_INTERVAL, WATCH_CHECK_INTERVAL,
                    TARGET_COMPANIES, PAPER_TRADING, PAPER_COST_PCT, PAPER_BENCHMARK)
from local_time import now_local
from news_collector import NewsCollector, clean_url, title_key
from analyzer import MarketAnalyzer
from cloud_analyzer import CloudAnalyzer
from keyword_analyzer import KeywordAnalyzer
from llm_prompts import AnalysisUnavailable
from notifier import Notifier
from ollama_manager import OllamaManager
from portfolio_manager import PortfolioManager
from watch_manager import WatchManager
from paper_trader import PaperTrader
from broker import Broker
from shadow_trades import ShadowBook
from source_manager import article_trust, OPINION
import costs
import fills
import markets
import price_lookup
import reddit_source
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

# How many scans an article gets for its analysis to go through. A failure is
# usually the engine being down (API outage, Ollama not running) rather than
# the article, so it is retried - but not forever, in case it is the article
# (too long for the model, say).
MAX_ANALYSIS_ATTEMPTS = 3

# Only a failure while the engine is known to be working counts as one of
# those attempts - see _analysis_failed. A failure in a pass where nothing
# succeeded is put down to an outage instead, and an article is given up on
# only after this many of those (about an hour of one-minute scans), so one
# the engine can never read doesn't wait forever either.
MAX_OUTAGE_STRIKES = 60

# A pass over the articles stops after this many failures in a row: the
# second one tells an outage (both fail) apart from one unreadable article
# (the next one goes through).
FAILURES_BEFORE_ENGINE_DOWN = 2

# Reddit posts that couldn't be analysed yet, kept for the next pass. The
# poller hands each post out once, so without this a post that met a failing
# engine was gone for good.
MAX_REDDIT_BACKLOG = 60

# Recent headlines remembered, to catch one story under a second URL.
MAX_STORED_TITLES = 500

# Most signals waiting for their entry time at once (_queue_entry). Each
# holds its article - up to news_collector.MAX_CONTENT_CHARS of text - and
# costs a price-context fetch when it comes due, so the bound is memory and
# a burst of requests at the open both.
MAX_PENDING_ENTRIES = 20


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


def is_weekend(now=None):
    """Saturday or Sunday in New York, when WEEKEND_CHECK_INTERVAL applies:
    Saturday 07:00 to Monday 07:00 in Romania (06:00 for the few weeks a
    year when only one side has changed its clocks)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now.astimezone(strategy.ET).weekday() >= 5


class SellRefused(Exception):
    """A manual Sell that could not be carried out; the message says why,
    in words meant for the user."""


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
        # Trading hours of exchanges outside markets.py's table, looked up
        # from Yahoo on earlier runs (price_lookup.learn_market).
        markets.load_learned()
        # Passive record of every watch's round trip, for judging whether the
        # alerts are actually worth acting on. Changes nothing about how the
        # app behaves - see paper_trader.py.
        self.paper = PaperTrader(cost_pct=PAPER_COST_PCT,
                                 benchmark=PAPER_BENCHMARK) if PAPER_TRADING else None
        # Signals the entry rules refused, followed as if they had been
        # traded, so the rules themselves can be judged - see
        # shadow_trades.py. Part of the paper record, so the same switch.
        self.shadows = ShadowBook(cost_pct=PAPER_COST_PCT) if PAPER_TRADING else None
        # The paper trades mirrored on an IBKR paper account, to check the
        # simulated fills against real ones - see broker.py. Follows the
        # paper ledger, so needs it on.
        self.broker = (Broker(log=self.log)
                       if config.IBKR_ENABLED and PAPER_TRADING else None)
        self._last_watch_check = 0
        self.processed_urls_file = 'data/processed_urls.json'
        # Oldest processed URLs are forgotten past this. Was 120: Reddit
        # sources add dozens of posts an hour, which would push feed articles
        # still inside their lookback window out early - and a forgotten URL
        # gets analysed (and alerted) again.
        self.max_stored_urls = 500
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
        self._init_pipeline_state()
        self.collector.is_seen_title = self.processed_titles_set.__contains__
        self.collector.on_warning = self.log
        # (time.time() the next scan starts at, seconds in the whole wait)
        # while the loop waits between scans, else None. One tuple, so the
        # UIs' countdown (next_scan_countdown) never reads half of an update.
        self.next_scan = None

    def _init_pipeline_state(self):
        """Per-article bookkeeping of the scan loop - retries, the Reddit
        backlog, recent headlines. Separate from __init__ so a test can build
        a bare backend with just this."""
        # url -> failed analysis attempts, for articles waiting on a retry.
        self._failed_attempts = {}
        # url -> passes it failed in while nothing else succeeded either.
        self._outage_strikes = {}
        # State of the current pass over the articles (a scan, or a Reddit
        # poll between scans) - see _begin_pass and _analysis_failed.
        self._engine_down = False
        self._pass_success = False
        self._consecutive_failures = 0
        self._uncharged = []
        # url -> Reddit article waiting for a working engine.
        self._reddit_backlog = collections.OrderedDict()
        # title_key()s of recently analysed articles, oldest evicted first.
        self.processed_titles = collections.deque(maxlen=MAX_STORED_TITLES)
        self.processed_titles_set = set()
        # Signals waiting for their entry time (_queue_entry), soonest
        # first. In memory only: a restart drops them, like an alert missed.
        self._pending_entries = []

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
            return f"Cloud AI · {config.CLOUD_AI_PROVIDER} · {' → '.join(config.cloud_models())}"
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
        if self.shadows:
            self.shadows.cost_pct = config.PAPER_COST_PCT
        self.log(f"Settings applied: engine = {self.engine_description()}")

    def _load_processed_urls(self):
        """Load previously processed URLs from disk (the last max_stored_urls only)."""
        if os.path.exists(self.processed_urls_file):
            try:
                with open(self.processed_urls_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    
                    # If data is a list, load directly
                    if isinstance(data, list):
                        # Keep only the most recent max_stored_urls
                        # Stored before tracking parameters were stripped, some
                        # of these still carry them; cleaned, they match the
                        # URLs the collector hands out now.
                        urls = collections.deque((clean_url(u) for u in data[-self.max_stored_urls:]
                                                  if isinstance(u, str)),
                                                 maxlen=self.max_stored_urls)
                        self.log(f"Loaded {len(urls)} previously processed URLs")
                        return urls
                    
                    # If data is dict (old format), convert to list
                    elif isinstance(data, dict):
                        # Sort by timestamp and take most recent
                        sorted_urls = sorted(data.items(), key=lambda x: x[1])
                        urls = [clean_url(url) for url, _ in sorted_urls[-self.max_stored_urls:]]
                        urls_deque = collections.deque(urls, maxlen=self.max_stored_urls)
                        self.log(f"Loaded {len(urls_deque)} previously processed URLs (converted from old format)")
                        return urls_deque
                    
            except Exception as e:
                self.log(f"Error loading processed URLs: {e}")
        # No file, an unreadable one, or JSON of neither shape.
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
        """Save processed URLs to disk (the last max_stored_urls only)."""
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
        if self.broker:
            self.broker.start()
        self.log(f"Monitoring started. Check interval: {CHECK_INTERVAL // 60} min, "
                 f"{WEEKEND_CHECK_INTERVAL // 60} min at weekends (New York time)")
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
        if self.broker:
            self.broker.stop()
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

    def _wait(self, seconds, generation, status=None):
        """Sleep until the next scan, polling Reddit every CHECK_INTERVAL
        meanwhile if the wait is longer (the weekend one). `status` is put
        back once a post analysed meanwhile has replaced it.

        The feeds don't need this - the next scan's window reaches back over
        the wait (see NewsCollector.window_start) - but Reddit does: it
        allows one request a minute and the poller makes at most one per
        call, so with one call per scan a busy subreddit's posts, queued for
        their comments, would expire before they were got through.
        """
        deadline = time.time() + seconds
        self.next_scan = (deadline, seconds)
        try:
            while self._alive(generation):
                left = deadline - time.time()
                if left < 1:
                    break
                self._sleep(min(round(left), CHECK_INTERVAL), generation)
                if deadline - time.time() >= 1 and self._alive(generation):
                    if self._poll_reddit(generation) and status:
                        self.status(status)
        finally:
            self.next_scan = None

    def next_scan_countdown(self):
        """(seconds left, seconds in the whole wait) until the next scan while
        the loop waits for it, else None - for the UIs' countdown timer."""
        next_scan = self.next_scan
        if next_scan is None or not self.running:
            return None
        deadline, total = next_scan
        return max(0.0, deadline - time.time()), total

    def _poll_reddit(self, generation):
        """Analyse whatever Reddit posts have become ready, plus any still
        waiting from a pass the engine failed in. Returns how many were new.
        Each poll is a pass of its own: an engine that failed during the
        scan gets a fresh chance here, as it would at the next scan."""
        posts = self.collector.fetch_reddit_posts()
        if posts or self._reddit_backlog:
            items = [("Custom Source News", article, True) for article in posts]
            self._run_pass(items, generation, self.notifier.open_markets())
        return len(posts)

    def _run_pass(self, items, generation, open_now, check_watches=False):
        """Analyse (company_hint, article, is_discovery) `items` in one pass.

        Reddit posts left over from an earlier pass go first. Articles that
        failed before go last, behind the fresh ones: if the engine is up,
        a fresh article proves it before a suspect one is charged an
        attempt; if it is down, the pass stops without one article taking
        every hit (see _analysis_failed).

        A Reddit post that isn't analysed - the engine down, or a stop in the
        middle - goes back into the backlog: the poller won't offer it again.
        """
        self._begin_pass()
        backlog = [("Custom Source News", a, True) for a in self._reddit_backlog.values()]
        self._reddit_backlog.clear()
        backlog_urls = {b[1].get('url') for b in backlog}
        queued = backlog + [i for i in items if i[1].get('url') not in backlog_urls]
        queued.sort(key=lambda i: (i[1].get('url') in self._failed_attempts
                                   or i[1].get('url') in self._outage_strikes))
        done = 0
        try:
            for hint, article, discovery in queued:
                if not self._alive(generation):
                    break
                # Counted before it runs: a post whose processing raises is
                # not put back, or it would abort every pass after this one.
                done += 1
                if discovery:
                    result = self._process_article(hint, article, open_now, is_discovery=True)
                else:
                    result = self._process_article(hint, article, open_now)
                if result is False:
                    self._defer_reddit(article)
                if check_watches:
                    # A pass can take many minutes - an analysis is up to two
                    # minutes a model - and stops and targets shouldn't wait
                    # for it to finish. Its own failure mustn't end the pass.
                    try:
                        self._maybe_check_watches()
                    except Exception as e:
                        self.log(f"Error checking watches: {e}")
        finally:
            # A stop, or an error, part-way through: the Reddit posts not
            # reached yet would never be offered again.
            for _, left, _ in queued[done:]:
                self._defer_reddit(left)
            self._end_pass()

    def _defer_reddit(self, article):
        """Keep a Reddit post that wasn't analysed for the next pass."""
        url = article.get('url')
        if not url or not reddit_source.is_reddit_article(article) or url in self.processed_set:
            return
        self._reddit_backlog[url] = article
        while len(self._reddit_backlog) > MAX_REDDIT_BACKLOG:
            self._reddit_backlog.popitem(last=False)

    def _maybe_check_watches(self):
        """Run the watch check if WATCH_CHECK_INTERVAL has passed since the last."""
        if time.time() - self._last_watch_check >= WATCH_CHECK_INTERVAL:
            self._check_watches()
            self._last_watch_check = time.time()

    def _run_loop(self, generation):
        self.log("Stocks Watcher Started...")

        # Send startup notification
        self.notifier.notify_system("Stocks Watcher Started", "The Stocks Watcher is now running and monitoring for news.")

        from config import GLOBAL_SCAN

        if GLOBAL_SCAN:
             self.log("🌍 GLOBAL MARKET SCAN: Enabled. Checking all major business sources.")
        else:
             self.log(f"Tracking companies: {', '.join(TARGET_COMPANIES)}")

        # When the last complete scan started. Each scan's window reaches
        # LOOKBACK_MINUTES back past it, so a story that reached its feed just
        # after that scan looked is still caught, however long the wait since
        # (25 minutes at weekends) or the scan itself took.
        prev_scan_start = None
        while self._alive(generation):
            try:
                scan_start = datetime.datetime.now(datetime.timezone.utc)
                self.collector.window_start = (
                    prev_scan_start - datetime.timedelta(minutes=config.LOOKBACK_MINUTES)
                    if prev_scan_start else None)
                self.log(f"\nScanning for news at {now_local().strftime('%H:%M:%S')}...")
                
                # Which exchanges are trading, not just New York's: this is
                # what the analysis prompt is told, and it decides whether a
                # story gaps a stock at the next open or moves it now.
                open_now = self.notifier.open_markets()
                self.log("Market Status: "
                         + (f"OPEN - {', '.join(open_now)}" if open_now else "CLOSED"))

                # --- CUSTOM SOURCES (Priority) ---
                # First, check custom user-defined sources
                self.log("🔗 Checking custom sources...")
                self.status("Fetching custom sources")
                custom_articles = self.collector.fetch_from_custom_sources()
                self.log(f"   Found {len(custom_articles)} articles from custom sources")
                
                # Everything is fetched first and analysed in one pass (see
                # _run_pass), custom sources first.
                items = [("Custom Source News", a, True) for a in custom_articles]

                # --- GLOBAL SCAN vs WATCHLIST ---
                if GLOBAL_SCAN and self._alive(generation):
                    self.log("Running Global Market Scan...")
                    self.status("Fetching market news")
                    # Hint "General Market" so the analyzer identifies the entity itself
                    items += [("General Market News", a, True)
                              for a in self.collector.fetch_general_market_news()]
                elif not GLOBAL_SCAN and self._alive(generation):
                    # Every feed downloaded once for all the companies.
                    self.status("Fetching company news")
                    for company, articles in self.collector.fetch_company_news(TARGET_COMPANIES).items():
                        items += [(company, a, False) for a in articles]

                self._run_pass(items, generation, open_now, check_watches=True)

                if not self._alive(generation):
                    break
                # Only a scan that got this far counts as complete: after an
                # error the next one reaches back past the last good one.
                prev_scan_start = scan_start

                # --- QUEUED ENTRIES ---
                # Signals that came in too early in their session, or while
                # it was shut, open now if their time has come.
                self._run_pending_entries()

                # --- WATCH CHECK (sell signals) ---
                # Coarser cadence than the news scan - price doesn't need to
                # be polled every minute, and it's a batched API call per
                # open watch.
                self._maybe_check_watches()

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
                    # Also to stdout, i.e. journald on the VM: the dashboard's
                    # log is in memory only and gone after the restart a
                    # memory problem ends in - the one time it is needed.
                    print(f"Memory in use: {rss:.0f} MB", flush=True)

                # --- SCHEDULER ---
                # Longer at weekends, when the market is shut. The next
                # scan's window stretches back over the wait, so it skips
                # nothing.
                interval = WEEKEND_CHECK_INTERVAL if is_weekend() else CHECK_INTERVAL
                next_run_time = now_local() + datetime.timedelta(seconds=interval)
                waiting = f"Waiting until {next_run_time.strftime('%H:%M:%S')}"
                self.log(f"Sleeping until {next_run_time.strftime('%H:%M:%S')} ({interval}s"
                         f"{', weekend schedule' if interval != CHECK_INTERVAL else ''})...")
                self.status(waiting)
                
                self._wait(interval, generation, waiting)

            except Exception as e:
                self.log(f"Error in main loop: {e}")
                # Same responsive sleep as the normal path: a flat 60s
                # sleep here made Stop take up to a minute after an error.
                self._sleep(60, generation)

    def _mark_processed(self, url, title=None):
        """Record `url` as analysed, so it is neither fetched nor analysed
        again - nor, when `title` is given, the same headline under another
        URL."""
        self._failed_attempts.pop(url, None)
        self._outage_strikes.pop(url, None)
        key = title_key(title)
        if key and key not in self.processed_titles_set:
            if len(self.processed_titles) == self.processed_titles.maxlen:
                self.processed_titles_set.discard(self.processed_titles[0])
            self.processed_titles.append(key)
            self.processed_titles_set.add(key)
        # Add to processed deque (automatically capped at max_stored_urls).
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

    def _begin_pass(self):
        """A new pass over the articles: whatever failed last time gets a
        new chance."""
        self._engine_down = False
        self._pass_success = False
        self._consecutive_failures = 0
        self._uncharged = []

    def _end_pass(self):
        """Settle a pass. Failures in a pass where nothing succeeded were
        most likely an outage and cost no attempt - but each is a strike,
        and MAX_OUTAGE_STRIKES of them give the article up all the same."""
        for url, title in self._uncharged:
            strikes = self._outage_strikes.pop(url, 0) + 1
            if strikes >= MAX_OUTAGE_STRIKES:
                self._mark_processed(url, title)
                self.log(f"   ✗ Giving up on an article that failed in {strikes} scans in a row: "
                         f"{(title or url)[:60]}")
                continue
            self._outage_strikes[url] = strikes
            if len(self._outage_strikes) > self.max_stored_urls:
                del self._outage_strikes[next(iter(self._outage_strikes))]
        self._uncharged = []

    def _analysis_succeeded(self):
        """The engine answered: it is up, so whatever failed earlier in this
        pass failed on its own account and is charged an attempt now."""
        self._pass_success = True
        self._consecutive_failures = 0
        uncharged, self._uncharged = self._uncharged, []
        for url, title in uncharged:
            self._charge_attempt(url, title, "failed while the engine was working")

    def _analysis_failed(self, url, error, title=None):
        """The engine couldn't analyse an article. Leave it unmarked so a
        later pass retries it.

        Only a failure the article can be blamed for counts towards
        MAX_ANALYSIS_ATTEMPTS: one in a pass where the engine has answered
        for another article. Charging every failure made a three-minute
        outage write off whichever article happened to be first in each of
        three scans. After FAILURES_BEFORE_ENGINE_DOWN in a row the rest of
        the pass is held back rather than failing on each in turn."""
        self._consecutive_failures += 1
        if self._pass_success:
            self._charge_attempt(url, title, error)
        else:
            self._uncharged.append((url, title))
            self.log(f"   ⚠ Analysis failed ({error}) - will retry")
        if self._consecutive_failures >= FAILURES_BEFORE_ENGINE_DOWN:
            self._engine_down = True
            self.log("   ⚠ The engine looks down - the rest of this pass's new articles "
                     "wait for the next one")

    def _charge_attempt(self, url, title, error):
        attempts = self._failed_attempts.pop(url, 0) + 1
        if attempts >= MAX_ANALYSIS_ATTEMPTS:
            self._mark_processed(url, title)
            self.log(f"   ✗ Analysis failed ({error}) - giving up on this article "
                     f"after {attempts} attempts")
            return
        self._failed_attempts[url] = attempts
        # An article that drops out of its feed before it succeeds would
        # otherwise stay here forever; dicts keep insertion order, so this
        # forgets the oldest.
        if len(self._failed_attempts) > self.max_stored_urls:
            del self._failed_attempts[next(iter(self._failed_attempts))]
        self.log(f"   ⚠ Analysis failed ({error}) - retrying on the next scan "
                 f"(attempt {attempts}/{MAX_ANALYSIS_ATTEMPTS})")

    def _process_article(self, company_hint, article, open_markets, is_discovery=False):
        """Analyse one article and act on the verdict. Returns False when it
        was left for a later pass (the engine down or failing), True
        otherwise."""
        url = article.get('url')
        title = article.get('title', 'No Title')

        if not url:
            self.log(f"⊘ Skipping article (no URL): {title[:60]}...")
            return True

        # Check if already processed
        if url in self.processed_set:
            self.log(f"⊘ Already processed: {title[:60]}...")
            return True  # Already processed

        if self._engine_down:
            # The engine already failed earlier in this pass. Left unmarked,
            # the article comes back on the next one instead of failing too.
            return False

        key = title_key(title)
        if key and key in self.processed_titles_set:
            # The same story from another feed, or with other tracking
            # parameters: already analysed (and alerted on, if it was news).
            self.log(f"⊘ Same story already analysed under another link: {title[:60]}...")
            self._mark_processed(url)
            return True

        self.log(f"\n📰 Processing article: {title}")
        self.log(f"   URL: {url[:80]}...")

        
        # Get portfolio tickers for context
        portfolio_tickers = list(self.portfolio_mgr.get_portfolio().keys())
        
        # Analyze
        self.log(f"   🔍 Analyzing with {self.engine_name()}...")
        self.status(f"Analyzing: {title[:48]}")
        try:
            analysis = self.analyzer.analyze_article(company_hint, article, open_markets, portfolio_tickers)
        except AnalysisUnavailable as e:
            self._analysis_failed(url, e, title)
            self.status("Idle")
            return False
        self._analysis_succeeded()

        # Marked only once the engine has actually judged the article.
        # Marking it before the call wrote off every article that arrived
        # during an outage - the collector never offers a seen URL again.
        self._mark_processed(url, title)
        self.stats['scanned'] += 1
        if analysis:
            analysis['analysed_at'] = now_local().isoformat()
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
        # A source that may not open positions doesn't get to close them
        # either - a Reddit thread alone shouldn't unwind a news-driven trade.
        if ticker and self._may_trade_on(article):
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
    def _may_trade_on(article):
        """Whether an alert from this article may open or close positions.
        Reddit posts are retail opinion, not reporting, so by default they
        notify only (REDDIT_CAN_TRADE, read live like every other setting)."""
        return config.REDDIT_CAN_TRADE or not reddit_source.is_reddit_article(article)

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
                      expected_pct, confidence, analysis, article, url, title, queued=False):
        """Turn an alert into a position, or say why not.

        Cheapest checks first: the price context costs a few requests, so it
        only runs for alerts that could still become a trade. Returns a dict
        - 'opened', 'reason', and when the trade was at least sized, its
        entry/target/stop (a short that is notified but not paper-traded
        still shows its setup in the notification). A signal the price rules
        refuse is followed as a skipped trade (shadow_trades.py).

        A signal that comes too early in its session, or while its market is
        shut, is queued for its entry time instead (_queue_entry); `queued`
        marks the call that runs it then.
        """
        # 'ticker' rides along so the notification can show the time exit on the
        # exchange's own clock - 17:15 CET for a Frankfurt position, not its ET
        # equivalent in the small hours of the user's evening.
        decision = {'opened': False, 'direction': direction, 'ticker': ticker,
                    'reason': None, 'watch': None}
        # A move the headline already reports - the model's figure or the
        # headline's own (strategy.reported_move), as a fraction.
        reported = strategy.reported_move(analysis.get('reported_move_pct'), title)
        analysis['reported_move_pct'] = round(reported, 4) if reported else None
        analysis['event_type'] = strategy.parse_event_type(analysis.get('event_type'))
        signal = self._signal_info(analysis, article)

        def no(reason):
            decision['reason'] = reason
            return decision

        if not ticker:
            return no("no tradable ticker")
        if not self._may_trade_on(article):
            return no("Reddit posts only raise alerts (trading on them is off in settings)")
        if markets.is_european(ticker) and not config.SCAN_EUROPE:
            # The alert still went out; only the position is refused, for a
            # broker that can't deal outside the US.
            return no(f"{ticker} is listed in Europe, which is off in settings")
        # Only exchanges the broker deals on (C8): the ones markets.py models
        # - New York and the main European venues - narrowed further by
        # ALLOWED_MARKETS. Seoul, Mumbai, Tokyo and Sydney listings were
        # traded before: no modelled session, FX fees, and likely no CFD.
        key = markets.market_key(ticker)
        if key is None and not config.TRADE_OTHER_EXCHANGES:
            return no(f"{ticker} is listed on an exchange outside the ones traded")
        allowed = config.allowed_markets()
        if key is not None and allowed and key not in allowed:
            return no(f"{ticker} is listed on {markets.MARKETS[key].name}, which is not among "
                      f"the exchanges traded (ALLOWED_MARKETS)")
        # Positions open only while the stock's own exchange is in its
        # regular session - the same gate every exit waits for. Outside it
        # the price is a thin extended-hours print, and a position opened on
        # one has no working stop until the open: QURE was shorted at 07:34 ET
        # on a pre-market print after a 60% crash, rallied through its 8% stop
        # within minutes, and the stop could first fire at 09:35, at -55%.
        # Since v9 such a signal waits for the session (and
        # ENTRY_DELAY_AFTER_OPEN_MIN into it) instead - see _queue_entry -
        # and is judged on the price of that moment; one it won't wait for
        # is not followed as a skipped trade, since it would open on that
        # same thin print. An exchange markets.py doesn't model has its hours
        # looked up from Yahoo first; one whose hours can't be found isn't
        # traded at all - there would be no telling when its stop works.
        if markets.needs_learning(ticker):
            price_lookup.learn_market(ticker)
        session = strategy.market_open(ticker)
        if session is None:
            return no(f"{ticker} is listed on an exchange whose trading hours couldn't be "
                      f"found - no position without them")
        closed = (f"{ticker}'s market is closed - positions only open during its "
                  f"regular session")
        if session is False and queued:
            return no(closed)
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
        if not queued:
            # Too early in the session, or the market shut: the signal
            # waits for its entry time (strategy.entry_ready_at), when this
            # runs again from the top with the price of that moment.
            ready = strategy.entry_ready_at(
                ticker, datetime.datetime.now(datetime.timezone.utc),
                _parse_time(article.get('published_ts'), None))
            if ready is False:
                return no(closed)
            if ready is not None:
                return no(self._queue_entry(ready, decision, (
                    ticker, company, direction, impact, prediction, expected_pct, confidence,
                    analysis, article, url, title)))

        self.status(f"Checking price action: {ticker}")
        # The index this listing is judged against - SPY for a US symbol,
        # the European one for a European listing (markets.benchmark_for).
        # Netting a Frankfurt stock's move against an index that was shut
        # for most of its session is noise, not the market's move.
        benchmark = markets.benchmark_for(ticker)
        context = price_lookup.fetch_context(ticker, article.get('published_ts'),
                                             benchmark=benchmark)
        self.log(f"   📈 {_describe_context(context)}")
        trust = article_trust(article)
        horizon = analysis.get('horizon')
        plan = strategy.plan_trade(
            direction, impact, expected_pct, context,
            needs_confirmation=trust == OPINION,
            capped=strategy.parse_flag(analysis.get('value_is_capped')) is True,
            reported_pct=reported, event_type=analysis.get('event_type'), horizon=horizon)
        # A merger-arbitrage plan sets its own horizon (strategy._merger_arb).
        horizon = plan.get('horizon') or horizon
        # What the trade would really have opened at - the ask for a long,
        # the bid for a short - worked out from the context's last trade,
        # which was fetched seconds ago from the same 1-minute chart the
        # watch checks price from (fills.py). Wanted for a skipped trade too,
        # so its record pays the same spread a real one would.
        fill = self._entry_fill(ticker, direction, context, article) if tracked or plan['ok'] else None
        if not plan['ok']:
            if tracked:
                self._follow_skipped(ticker, company, direction, impact, horizon, confidence,
                                     trust, title, url, context, plan, fill, signal)
            return no(plan['reason'])
        if not fill:
            return no(f"couldn't price {ticker}")
        self.log(f"   💱 {fills.describe(fill)}")
        # A price nobody could have traded at - too old, from before the
        # story, or across too wide a spread - is not an entry. Followed as
        # a skipped trade like any other refusal, so the cost of the rule
        # shows up.
        if fill['problem']:
            rule, reason = fill['problem']
            if tracked:
                self._follow_skipped(ticker, company, direction, impact, horizon, confidence,
                                     trust, title, url, context,
                                     dict(plan, rule=rule, reason=reason), fill, signal)
            return no(reason)
        # Too small or too thinly traded to trade at a real broker (C8):
        # followed as a skipped trade, so what the floor costs shows up.
        thin = self._universe_problem(context, fill)
        if thin:
            if tracked:
                self._follow_skipped(ticker, company, direction, impact, horizon, confidence,
                                     trust, title, url, context,
                                     dict(plan, rule='illiquid', reason=thin), fill, signal)
            return no(thin)
        entry = fill['price']
        benchmark_entry = context.get('market_price')

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

        # How much to put in, from the paper account as it stands - and
        # whether its cash covers it at all (paper_account.position_size).
        # A signal the cash can't cover is followed as a skipped trade, like
        # one the price rules refused, so the cost of the limit shows up.
        extra_sizing = {}
        if self.paper:
            sizing = self.paper.size_new_trade(plan['stop_pct'], confidence, impact)
            if sizing['size'] is None:
                self._follow_skipped(ticker, company, direction, impact, horizon, confidence,
                                     trust, title, url, context,
                                     dict(plan, rule='cash', reason=sizing['reason']), fill, signal)
                return no(sizing['reason'])
            decision.update(position_usd=sizing['size'], risk_usd=sizing['risk_usd'],
                            account_usd=sizing['account'])
            self.log(f"   💵 Size ${sizing['size']:,.2f} of a ${sizing['account']:,.2f} account "
                     f"(risking ${sizing['risk_usd']:,.2f}, conviction x{sizing['conviction']}, "
                     f"${sizing['cash']:,.2f} cash free)")
            extra_sizing = {'position_usd': sizing['size'], 'risk_usd': sizing['risk_usd']}

        watch = self.watch_mgr.add_watch(
            ticker, company, entry, impact, horizon, prediction,
            article_url=url, article_headline=title, direction=direction,
            target_pct=plan['target_pct'], stop_pct=plan['stop_pct'],
            extra={
                'confidence': confidence,
                'source_trust': trust,
                'expected_move_pct': plan['expected_move_pct'],
                'already_moved_pct': plan['already_moved_pct'],
                'moved_from': plan['moved_from'],
                'market_move_pct': plan['market_move_pct'],
                'atr_pct': plan['atr_pct'],
                'entry_last': fill['last'],
                'spread_pct': fill['spread_pct'],
                'spread_source': fill['spread_source'],
                # None for an ordinary news trade; 'merger_arb' for a
                # takeover target held for a better bid.
                'mode': plan.get('mode'),
                'market_cap_usd': fill.get('market_cap_usd'),
                'turnover_usd': fill.get('turnover_usd'),
                # The wider news-day stop gives way to this one after the
                # first session (strategy.stop_distance).
                'settled_stop_pct': plan.get('settled_stop_pct'),
                'first_day_until': (strategy.first_session_end(now_local(), ticker).isoformat()
                                    if plan.get('settled_stop_pct') else None),
                **extra_sizing,
                **signal,
            },
        )
        if not watch:
            return no(f"already holding a position in {ticker}")
        if self.paper:
            self.paper.open_trade(watch, benchmark_entry, benchmark=benchmark)
        if getattr(self, 'broker', None):
            self.broker.open_position(watch)
        decision.update(opened=True, watch=watch, expires_at=watch['expires_at'])
        return decision

    def _queue_entry(self, ready, decision, args):
        """Hold a signal until `ready` (strategy.entry_ready_at), when
        _run_pending_entries puts it through _decide_trade again. `args` are
        _decide_trade's. Returns the no-trade-yet reason for the alert."""
        ticker = args[0]
        local = ready.astimezone(markets.timezone(ticker))
        when = f"{local:%a %H:%M} {markets.tz_label(ticker)}"
        if any(p['ticker'] == ticker for p in self._pending_entries):
            return f"an entry in {ticker} is already waiting for {when}"
        if len(self._pending_entries) >= MAX_PENDING_ENTRIES:
            return f"{MAX_PENDING_ENTRIES} entries are already waiting for their session"
        self._pending_entries.append({'ticker': ticker, 'ready_at': ready, 'args': args})
        self._pending_entries.sort(key=lambda p: p['ready_at'])
        decision['pending_until'] = ready.isoformat()
        return (f"entry waits until {when}, {config.ENTRY_DELAY_AFTER_OPEN_MIN} min into the "
                f"session - a BUY/SHORT alert follows then if the price still allows it")

    def _run_pending_entries(self):
        """Put every queued signal whose entry time has come through
        _decide_trade, and send the entry alert for the ones that open."""
        now = datetime.datetime.now(datetime.timezone.utc)
        due = [p for p in self._pending_entries if p['ready_at'] <= now]
        if not due:
            return
        self._pending_entries = [p for p in self._pending_entries if p['ready_at'] > now]
        for p in due:
            (ticker, company, direction, impact, prediction, expected_pct, confidence,
             analysis, article, url, title) = p['args']
            self.log(f"\n⏰ Entry time for {ticker} ({title[:60]})")
            try:
                decision = self._decide_trade(*p['args'], queued=True)
            except Exception as e:
                self.log(f"  ✗ Entry check failed: {e}")
                continue
            if not decision['opened']:
                self.log(f"  ✗ No trade: {decision['reason']}")
                continue
            watch = decision['watch']
            self.log(f"  {'BUY' if direction == LONG else 'SHORT'} {ticker} at "
                     f"{watch['entry_price']:.2f} -> target {watch['target_price']:.2f}, "
                     f"stop {watch['stop_loss_price']:.2f}")
            is_owned = self.portfolio_mgr.has_stock(ticker)
            if direction == SHORT and not is_owned and not config.NOTIFY_SHORTS:
                continue
            self.notifier.notify(company, article, analysis, is_owned=is_owned, decision=decision)

    def _signal_info(self, analysis, article):
        """paper_trader.SIGNAL_FIELDS for a trade or skipped trade on this
        analysis: which model and prompt judged it, where the story came
        from, and when it was published and analysed."""
        return {
            # The keyword engine names no model; its name stands in.
            'model': analysis.get('model') or self.engine_name(),
            'prompt_version': analysis.get('prompt_version'),
            'news_source': article.get('source'),
            'published_at': article.get('published_ts'),
            'analysed_at': analysis.get('analysed_at'),
            'event_type': analysis.get('event_type'),
            'reported_move_pct': analysis.get('reported_move_pct'),
        }

    def _entry_fill(self, ticker, direction, context, article):
        """The price a position in `ticker` would open at now, and whether
        it may: fills.entry_fill plus 'problem', a (rule, reason) from
        fills.entry_problem or None. None when there is no price at all.

        Costs one quote request, so it is only asked for once a signal has
        got as far as a price check."""
        last = (context or {}).get('price')
        if not last:
            return None
        quote = price_lookup.fetch_quote(ticker)
        fill = fills.entry_fill(direction, last, quote, strategy.market_open(ticker))
        fill['problem'] = fills.entry_problem(
            fill, context.get('price_time'), quote.get('delay_min'),
            article.get('published_ts'))
        # For the liquidity floors (_universe_problem), from the same quote.
        fill['market_cap'] = quote.get('market_cap')
        fill['currency'] = quote.get('currency')
        return fill

    @staticmethod
    def _universe_problem(context, fill):
        """Why a stock is too small or too thin to trade - under
        MIN_MARKET_CAP_USD, or under MIN_DAILY_TURNOVER_USD traded a day -
        or None. A figure Yahoo doesn't have doesn't count against it."""
        currency = fill.get('currency')
        cap = costs.approx_usd(fill.get('market_cap'), currency, minor_units=False)
        turnover = costs.approx_usd((context or {}).get('avg_turnover'), currency)
        fill['market_cap_usd'] = round(cap) if cap is not None else None
        fill['turnover_usd'] = round(turnover) if turnover is not None else None
        floor = config.MIN_MARKET_CAP_USD
        if floor and cap is not None and cap < floor:
            return (f"market cap ~${cap / 1e6:,.0f}M is under the ${floor / 1e6:,.0f}M "
                    f"floor (MIN_MARKET_CAP_USD)")
        floor = config.MIN_DAILY_TURNOVER_USD
        if floor and turnover is not None and turnover < floor:
            return (f"only ~${turnover / 1e6:,.2f}M traded a day, under the "
                    f"${floor / 1e6:,.1f}M floor (MIN_DAILY_TURNOVER_USD)")
        return None

    def _exit_fill(self, position, price, quote=None, at_level=False):
        """The price closing `position` would get: the bid for a long, the
        ask for a short (fills.exit_fill), from a fresh quote unless one is
        given. `price` is the last trade the exit fired on."""
        if quote is None:
            quote = price_lookup.fetch_quote(position['ticker'])
        return fills.exit_fill(position.get('direction', LONG), price, quote,
                               strategy.market_open(position['ticker']),
                               fallback_spread=position.get('spread_pct'), at_level=at_level)

    def _follow_skipped(self, ticker, company, direction, impact, horizon, confidence,
                        trust, title, url, context, plan, fill=None, signal=None):
        """Follow a signal the entry rules refused as if it had been traded,
        so the rule that refused it can be judged later (shadow_trades.py).
        Opened at `fill` - the price a real entry would have paid - when
        there is one. Never notified. `signal` is _signal_info's."""
        if not self.shadows:
            return
        ctx = context or {}
        record = self.shadows.track(
            ticker, company, direction, fill['price'] if fill else ctx.get('price'), plan,
            horizon=horizon, impact=impact, confidence=confidence, source_trust=trust,
            headline=title, url=url, benchmark_price=ctx.get('market_price'),
            spread_pct=fill['spread_pct'] if fill else None, signal=signal)
        if record:
            self.log(f"   👻 Following it as a skipped trade ({plan['rule']}) to see "
                     f"whether the rule was right")
        elif self.shadows.is_full():
            self.log(f"   (not followed as a skipped trade - {config.MAX_SHADOW_POSITIONS} "
                     f"already are)")

    def _close_on_reversal(self, ticker, direction):
        """Close an open position in `ticker` facing the other way from new
        news that has just passed the alert filters."""
        for watch in self.watch_mgr.get_open_watches():
            if watch['ticker'] != ticker or watch.get('direction', LONG) == direction:
                continue
            benchmark = markets.benchmark_for(ticker)
            wanted = [ticker] + ([benchmark] if self.paper else [])
            prices = price_lookup.fetch_prices(wanted)
            price = prices.get(ticker)
            if not price:
                self.log(f"  (news contradicts the open {watch['direction']} on {ticker}, "
                         f"but it couldn't be priced - left to the next watch check)")
                return
            self.log(f"  ↩ New {'positive' if direction == LONG else 'negative'} news contradicts "
                     f"the open {watch['direction']} on {ticker} - closing it at {price:.2f}")
            self._close_position(watch, 'news_reversal', price,
                                 prices.get(benchmark), now_local())

    def _close_position(self, watch, reason, price, benchmark_price, now, quote=None,
                        at_level=False):
        """Close a watch, record the paper trade and send the exit signal.
        `price` is the last trade the exit fired on; the position closes at
        the side of the quote a real order would hit (fills.py) - or, with
        `at_level` (a stop triggered between two checks), half the spread
        beyond `price`. Returns the exit price, or None if the watch was no
        longer open."""
        fill = self._exit_fill(watch, price, quote, at_level)
        price = fill['price']
        closed = self.watch_mgr.close_watch(watch['id'], reason, price)
        if not closed:
            return None
        self.log(f"  💱 Exit {fills.describe(fill)}")

        if self.paper:
            trade = self.paper.close_trade(closed, price, benchmark_price, fill)
            if trade:
                self.log(f"  📒 Paper trade #{len(self.paper.closed())}: "
                         f"{trade['net_pct'] * 100:+.2f}% after costs")
        if getattr(self, 'broker', None):
            self.broker.close_position(closed, reason, price)

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
                position_usd=watch.get('position_usd'),
                cost_pct=self.paper.cost_pct if self.paper else config.PAPER_COST_PCT,
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
        return price

    def close_watch_manually(self, watch_id):
        """User-initiated exit - the "Sell" button on an open paper
        position, for whenever someone doesn't want to wait for the
        strategy's own exit rules to fire. Goes through the same
        _close_position path as an automatic exit (paper trade recorded,
        SELL/COVER notification sent, Alerts panel updated), just with
        reason='manual' and a price fetched fresh rather than one already in
        hand from a watch-check pass.

        Only a sale that could really happen goes through: the stock's own
        exchange must be in its regular session, and its last trade recent
        enough to be the price now (the entry's ENTRY_MAX_PRICE_AGE_MIN plus
        the exchange's reporting delay - which also catches a holiday, which
        markets.py doesn't model). Otherwise nothing is closed.

        Returns the exit price. Raises SellRefused, with a message for the
        user, when there's no such open position or it can't be sold now.
        """
        watch = next((w for w in self.watch_mgr.get_open_watches() if w['id'] == watch_id), None)
        if not watch:
            raise SellRefused("That position is no longer open - it may have just been "
                              "closed by its own exit rules.")
        ticker = watch['ticker']
        if strategy.market_open(ticker) is False:
            raise SellRefused(f"{ticker}'s market is closed. Sell it once its exchange opens.")
        price, printed = price_lookup.fetch_last_trade(ticker)
        if not price:
            raise SellRefused(f"Couldn't get a price for {ticker}. Try again in a minute.")
        quote = price_lookup.fetch_quote(ticker)
        age = (time.time() - printed) / 60
        allowed = config.ENTRY_MAX_PRICE_AGE_MIN + (quote.get('delay_min') or 0)
        if age > allowed:
            raise SellRefused(f"{ticker} hasn't traded for {age:.0f} minutes - its market may be "
                              f"shut for a holiday or the stock halted, so there's no price to "
                              f"sell at.")
        benchmark = markets.benchmark_for(ticker)
        benchmark_price = (price_lookup.fetch_prices([benchmark]).get(benchmark)
                           if self.paper else None)
        sold = self._close_position(watch, 'manual', price, benchmark_price, now_local(), quote)
        if sold is None:
            raise SellRefused("That position is no longer open - it may have just been "
                              "closed by its own exit rules.")
        self.log(f"🖐 Sold {ticker} by hand at {sold:.2f}")
        return sold

    def _check_watches(self):
        """Check every open watch against its exits and close + notify any
        that fire - a sell signal for longs, a buy-back signal for shorts.
        Skipped trades (shadow_trades.py) go through the same exits in the
        same pass, silently. The rules are in strategy.py.

        Every 1-minute bar since the last pass is checked, not only the
        newest price: a stop order is triggered by any trade through its
        level, and fills there (strategy.update_exit_bar). Only bars from
        the stock's own regular session count, and only bars at all - the
        stale close price_lookup falls back to when the minute chart has
        nothing is never acted on. A position that gets no price for
        config.NO_PRICE_CLOSE_SESSIONS sessions is closed at its last one.
        """
        open_watches = self.watch_mgr.get_open_watches()
        skipped = self.shadows.open_records() if self.shadows else []
        if not open_watches and not skipped:
            return

        tickers = sorted({w['ticker'] for w in open_watches} | {s['ticker'] for s in skipped})
        # An exchange markets.py doesn't model has its hours looked up from
        # Yahoo once (price_lookup.learn_market); until then it has none.
        for ticker in tickers:
            if markets.needs_learning(ticker):
                price_lookup.learn_market(ticker)
        # Each position is gated on its own exchange's session, not on New
        # York's: a London holding is managed 08:00-16:30 London time, of
        # which New York is open for barely an hour and a half.
        sessions = {t: strategy.market_open(t) for t in tickers}
        if open_watches:
            # Said plainly, because "checking for exit signals" overnight
            # would read as "no exit was due" when in fact none could fire.
            live = sum(1 for w in open_watches
                       if not (config.EXITS_REGULAR_HOURS_ONLY and sessions[w['ticker']] is False))
            if live == len(open_watches):
                state = "checking for exit signals..."
            elif live:
                state = (f"checking {live} for exit signals - the rest are marked only, "
                         f"their exchanges being shut")
            else:
                state = "marking prices only - exits wait for the open"
            self.log(f"👀 {len(open_watches)} open watch(es): {state}")
        # One batched call for all of it: skipped trades ride along with the
        # real positions instead of costing requests of their own, and so
        # does each market's benchmark - two at most, and only the ones an
        # open position is actually measured against.
        benchmarks = {t: markets.benchmark_for(t) for t in tickers} if self.paper else {}
        bars = price_lookup.fetch_bars(tickers + sorted(set(benchmarks.values())))
        now = now_local()
        cost = self.paper.cost_pct if self.paper else config.PAPER_COST_PCT

        def benchmark_price_for(ticker):
            rows = bars.get(benchmarks[ticker]) if self.paper else None
            return rows[-1][4] if rows else None

        # This pass's in-session prices of the open positions, for the paper
        # account's marked-to-market graph (PaperTrader.record_marks).
        marks = {}
        for watch in open_watches:
            ticker = watch['ticker']
            self._seed_last_price(watch)
            seen = self._run_bars(watch, bars.get(ticker) or [], now, cost)
            if seen['close'] is not None:
                if seen['in_session']:
                    marks[watch['id']] = seen['close']
                # Note how far this position has run either way before
                # deciding whether it resolves - the worst point a trade
                # passed through is what a later stop-loss study needs, and
                # it is unrecoverable if not captured live.
                if self.paper:
                    self.paper.mark_price(watch['id'], seen['close'], seen['low'], seen['high'])
            if seen['reached'] and not seen['reason']:
                self._notify_target_reached(watch, seen['close'])

            reason, price, at_level = seen['reason'], seen['price'], True
            if not reason:
                reason, price, at_level = self._time_exit(watch, now, sessions[ticker])
            if reason == 'max_age':
                self.log(f"  📅 {watch['company']} ({ticker}) open past the age "
                         f"limit - closing at {price:.2f}")
            elif reason == 'no_price':
                self.log(f"  ⚠ {watch['company']} ({ticker}) has had no price for "
                         f"{config.NO_PRICE_CLOSE_SESSIONS}+ sessions - closing at its last, "
                         f"{price:.2f}")
            if reason:
                self._close_position(watch, reason, price, benchmark_price_for(ticker), now,
                                     at_level=at_level)

        for record in skipped:
            ticker = record['ticker']
            seen = self._run_bars(record, bars.get(ticker) or [], now, cost)
            if seen['close'] is not None:
                for p in (seen['low'], seen['high'], seen['close']):
                    self.shadows.mark_price(record, p)
            reason, price = seen['reason'], seen['price']
            if not reason:
                reason, price, _ = self._time_exit(record, now, sessions[ticker])
            if reason:
                # Out at the other side of the spread it went in across -
                # assumed from the entry rather than a quote request per
                # skipped trade. Records from before fills were modelled
                # went in at the last trade and come out at it too.
                if record.get('spread_pct') is not None:
                    price = fills.exit_fill(record['direction'], price, {}, sessions[ticker],
                                            fallback_spread=record['spread_pct'])['price']
                self.shadows.close(record, reason, price, benchmark_price_for(ticker), now)
                self.log(f"  👻 Skipped trade {ticker} ({record['skip_rule']}) would have "
                         f"closed at {price:.2f}: {record['net_pct'] * 100:+.2f}% after costs ({reason})")

        # One write per file for the whole pass: the exit rules and
        # mark_price only mutate in memory, so without this every
        # still-open position's ratcheted stop and excursions would be lost
        # on restart.
        if open_watches:
            self.watch_mgr.save()
            if self.paper:
                self.paper.save()
                if self.paper.record_marks(marks, now):
                    self.paper.save_marks()
        if skipped:
            self.shadows.save()

        # The price call above is the single largest allocation this process
        # makes (yfinance/pandas frames), and it runs on a coarser cadence
        # than the news scan's own _release_memory() at the end of a cycle.
        # Without trimming here the high-water mark of the last price fetch
        # stays resident until the next scan finishes.
        _release_memory()

    def _seed_last_price(self, watch):
        """A position opened before last prices were kept on it starts from
        its newest mark, so the no-price clock doesn't start at its entry."""
        if watch.get('last_price_at') or not self.paper:
            return
        ts, price = self.paper.last_mark(watch['id'])
        if ts:
            watch['last_price'] = price
            watch['last_price_at'] = datetime.datetime.fromtimestamp(
                ts, datetime.timezone.utc).isoformat()

    @staticmethod
    def _run_bars(position, rows, now, cost):
        """Put the 1-minute bars `position` hasn't seen yet through its exit
        rules, oldest first, stopping at the first exit. Mutates `position`
        (stop ratchet, last_bar_ts, last_price, last_price_at).

        Only finished bars are used (the newest is usually still forming),
        and only ones from after the minute the position opened in - the
        rest of that minute traded before it existed. When the exits are
        gated on the session (config.EXITS_REGULAR_HOURS_ONLY) only bars from
        the stock's own regular session move the stop or fire it: outside it
        they are a handful of shares at a spread no real exit would cross,
        and an overnight gap is still there at the open - the first price
        the position could really have got.

        Returns {'reason', 'price' (the exit's trigger level), 'reached'
        (target just reached), 'close', 'low', 'high' (of the bars seen),
        'in_session' (whether they were from the session)}.
        """
        out = {'reason': None, 'price': None, 'reached': False, 'close': None,
               'low': None, 'high': None, 'in_session': False}
        now_ts = now.timestamp()
        after = position.get('last_bar_ts')
        if after is None:
            # Never checked against bars: from the end of the opening minute,
            # or - a position older than that - the last pass's worth.
            opened = _parse_time(position.get('opened_at'), now).timestamp()
            after = max(opened // 60 * 60, now_ts - 2 * WATCH_CHECK_INTERVAL)
        gated = config.EXITS_REGULAR_HOURS_ONLY
        ticker = position['ticker']
        if not position.get('last_price_at'):
            # Kept from the first pass on: the newest session price there is,
            # so a quiet stock isn't taken for one with no price at all.
            for ts, o, h, l, c in reversed(rows):
                when = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
                if ts + 60 <= now_ts and strategy.market_open(ticker, when) is not False:
                    position['last_price'] = c
                    position['last_price_at'] = when.isoformat()
                    break
        for ts, o, h, l, c in rows:
            if ts <= after or ts + 60 > now_ts:
                continue
            position['last_bar_ts'] = ts
            when = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
            in_session = strategy.market_open(ticker, when) is not False
            if gated and not in_session:
                out.update(close=c, in_session=False)
                continue
            out['low'] = l if out['low'] is None else min(out['low'], l)
            out['high'] = h if out['high'] is None else max(out['high'], h)
            out.update(close=c, in_session=True)
            position['last_price'] = c
            position['last_price_at'] = when.isoformat()
            reason, level, reached = strategy.update_exit_bar(position, (o, h, l, c), cost, when)
            out['reached'] = out['reached'] or reached
            if reason:
                position['last_price'] = level
                out.update(reason=reason, price=level)
                break
        return out

    @staticmethod
    def _time_exit(position, now, market_open):
        """The exits that don't need the price to do anything: the time
        exit, the age limit and - when the stock has had no price for
        config.NO_PRICE_CLOSE_SESSIONS sessions - 'no_price'. Returns
        (reason, price, at_level) or (None, None, None).

        Time exits are gated on the session whoever your broker is - one
        fired at 03:00 would close on a stale print either way. They close
        at the stock's last price from today's session; with none yet today
        (a holiday markets.py doesn't model, a halt) they wait. A price that
        never comes - a takeover closed and the stock delisted, a ticker
        Yahoo dropped - would otherwise hold the position open forever, as
        no exit at all can fire without one: after enough sessions it is
        closed at the last price it had, flagged exit_price_stale.
        """
        if market_open is False:
            return None, None, None
        last = position.get('last_price')
        last_at = _parse_time(position.get('last_price_at') or position.get('opened_at'), now)
        if last and strategy.session_started(position['ticker'], last_at, now):
            if WatchManager.over_age_limit(position, now):
                return 'max_age', last, False
            if now >= _parse_time(position.get('expires_at'), now):
                return 'horizon_expired', last, False
            return None, None, None
        missed = strategy.sessions_without_price(position['ticker'], last_at, now)
        if missed >= config.NO_PRICE_CLOSE_SESSIONS:
            position['exit_price_stale'] = True
            return 'no_price', last or position['entry_price'], True
        return None, None, None

    def _notify_target_reached(self, watch, price):
        """A let-it-run position reached its target: it stays open, with its
        stop now trailing close behind. Worth telling the user - their
        broker-side stop should follow."""
        direction = watch.get('direction', LONG)
        half = watch.get('half_exit_gain') is not None
        self.log(f"  🎯 {watch['company']} ({watch['ticker']}) reached its target at "
                 f"{price:.2f} - {'half sold, the rest' if half else 'holding'}, trailing stop "
                 f"now {watch['stop_loss_price']:.2f}")
        if half and self.paper:
            self.paper.note_half_exit(watch['id'], watch['half_exit_gain'])
        if direction == SHORT and not config.NOTIFY_SHORTS:
            return
        self.notifier.notify_target_reached(
            watch['ticker'], watch['company'], direction, watch['entry_price'],
            price, watch['stop_loss_price'], article_url=watch.get('article_url'),
            take_half=half)


def _parse_time(value, fallback):
    """ISO timestamp -> aware datetime; `fallback` (normally now, i.e. "due")
    for a missing or unreadable one, so a damaged record still gets its
    time exit instead of raising inside the watch pass."""
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=fallback.tzinfo if fallback else datetime.timezone.utc)
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
    market = [f"{ctx[key] * 100:+.1f}% since {label}"
              for key, label in (('market_since_close_pct', 'close'),
                                 ('market_since_publish_pct', 'published'))
              if ctx.get(key) is not None]
    if market:
        parts.append("market " + ", ".join(market))
    if ctx.get('published_in_session') is not None:
        parts.append("published " + ("in" if ctx['published_in_session'] else "outside")
                     + " market hours")
    return "Price context: " + ", ".join(parts)
