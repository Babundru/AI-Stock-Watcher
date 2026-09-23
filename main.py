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
from shadow_trades import ShadowBook
from source_manager import article_trust, OPINION
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
        # Signals the entry rules refused, followed as if they had been
        # traded, so the rules themselves can be judged - see
        # shadow_trades.py. Part of the paper record, so the same switch.
        self.shadows = ShadowBook(cost_pct=PAPER_COST_PCT) if PAPER_TRADING else None
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
                      expected_pct, confidence, analysis, article, url, title):
        """Turn an alert into a position, or say why not.

        Cheapest checks first: the price context costs a few requests, so it
        only runs for alerts that could still become a trade. Returns a dict
        - 'opened', 'reason', and when the trade was at least sized, its
        entry/target/stop (a short that is notified but not paper-traded
        still shows its setup in the notification). A signal the price rules
        refuse is followed as a skipped trade (shadow_trades.py).
        """
        # 'ticker' rides along so the notification can show the time exit on the
        # exchange's own clock - 17:15 CET for a Frankfurt position, not its ET
        # equivalent in the small hours of the user's evening.
        decision = {'opened': False, 'direction': direction, 'ticker': ticker,
                    'reason': None, 'watch': None}

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
            capped=strategy.parse_flag(analysis.get('value_is_capped')) is True)
        if not plan['ok']:
            if tracked:
                self._follow_skipped(ticker, company, direction, impact, horizon, confidence,
                                     trust, title, url, context, plan)
            return no(plan['reason'])

        # The context was fetched seconds ago from the same 1-minute,
        # extended-hours chart the watch checks price from, for the stock and
        # the benchmark alike - so its prices are the entry. Downloading both
        # again here cost two more Yahoo requests per trade for nothing.
        entry = context.get('price')
        benchmark_entry = context.get('market_price')
        if not entry:
            prices = price_lookup.fetch_prices([ticker] + ([benchmark] if self.paper else []))
            entry, benchmark_entry = prices.get(ticker), prices.get(benchmark)
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
                'source_trust': trust,
                'expected_move_pct': plan['expected_move_pct'],
                'already_moved_pct': plan['already_moved_pct'],
                'moved_from': plan['moved_from'],
                'market_move_pct': plan['market_move_pct'],
                'atr_pct': plan['atr_pct'],
            },
        )
        if not watch:
            return no(f"already holding a position in {ticker}")
        if self.paper:
            self.paper.open_trade(watch, benchmark_entry, benchmark=benchmark)
        decision.update(opened=True, watch=watch, expires_at=watch['expires_at'])
        return decision

    def _follow_skipped(self, ticker, company, direction, impact, horizon, confidence,
                        trust, title, url, context, plan):
        """Follow a signal the entry rules refused as if it had been traded,
        so the rule that refused it can be judged later (shadow_trades.py).
        Never notified."""
        if not self.shadows:
            return
        ctx = context or {}
        record = self.shadows.track(
            ticker, company, direction, ctx.get('price'), plan,
            horizon=horizon, impact=impact, confidence=confidence, source_trust=trust,
            headline=title, url=url, benchmark_price=ctx.get('market_price'))
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

    def close_watch_manually(self, watch_id):
        """User-initiated exit - the "Sell" button on an open paper
        position, for whenever someone doesn't want to wait for the
        strategy's own exit rules to fire. Goes through the same
        _close_position path as an automatic exit (paper trade recorded,
        SELL/COVER notification sent, Alerts panel updated), just with
        reason='manual' and a price fetched fresh rather than one already in
        hand from a watch-check pass.

        Returns the exit price on success, or None if there's no such open
        watch or it couldn't be priced.
        """
        watch = next((w for w in self.watch_mgr.get_open_watches() if w['id'] == watch_id), None)
        if not watch:
            return None
        benchmark = markets.benchmark_for(watch['ticker'])
        wanted = [watch['ticker']] + ([benchmark] if self.paper else [])
        prices = price_lookup.fetch_prices(wanted)
        price = prices.get(watch['ticker'])
        if not price:
            return None
        self._close_position(watch, 'manual', price, prices.get(benchmark), now_local())
        return price

    def _check_watches(self):
        """Check every open watch's current price against its exits and close
        + notify any that fire - a sell signal for longs, a buy-back signal
        for shorts. Skipped trades (shadow_trades.py) go through the same
        exits in the same pass, silently. The rules are in strategy.py."""
        open_watches = self.watch_mgr.get_open_watches()
        skipped = self.shadows.open_records() if self.shadows else []
        if not open_watches and not skipped:
            return

        # Each position is gated on its own exchange's session, not on New
        # York's: a London holding is managed 08:00-16:30 London time, of
        # which New York is open for barely an hour and a half.
        sessions = {t: strategy.market_open(t)
                    for t in {w['ticker'] for w in open_watches} | {s['ticker'] for s in skipped}}
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
        # One batched price call for all of it: skipped trades ride along
        # with the real positions instead of costing requests of their own,
        # and so does each market's benchmark - two at most, and only the
        # ones an open position is actually measured against.
        tickers = list(sessions)
        benchmarks = {t: markets.benchmark_for(t) for t in tickers} if self.paper else {}
        tickers += sorted(set(benchmarks.values()))
        prices = price_lookup.fetch_prices(tickers)
        now = now_local()
        cost = self.paper.cost_pct if self.paper else config.PAPER_COST_PCT

        def benchmark_price_for(ticker):
            return prices.get(benchmarks[ticker]) if self.paper else None

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

            reason, target_reached = self._exit_reason(
                watch, price, now, sessions[watch['ticker']], cost)
            if reason == 'max_age':
                self.log(f"  📅 {watch['company']} ({watch['ticker']}) open past the age "
                         f"limit - closing at {price:.2f}")
            if target_reached and not reason:
                self._notify_target_reached(watch, price)
            if reason:
                self._close_position(watch, reason, price,
                                     benchmark_price_for(watch['ticker']), now)

        for record in skipped:
            price = prices.get(record['ticker'])
            if not price:
                continue
            self.shadows.mark_price(record, price)
            reason, _ = self._exit_reason(record, price, now, sessions[record['ticker']], cost)
            if reason:
                self.shadows.close(record, reason, price,
                                   benchmark_price_for(record['ticker']), now)
                self.log(f"  👻 Skipped trade {record['ticker']} ({record['skip_rule']}) would have "
                         f"closed at {price:.2f}: {record['net_pct'] * 100:+.2f}% after costs ({reason})")

        # One write per file for the whole pass: update_exit and mark_price
        # only mutate in memory, so without this every still-open position's
        # ratcheted stop and excursions would be lost on restart.
        if open_watches:
            self.watch_mgr.save()
            if self.paper:
                self.paper.save()
        if skipped:
            self.shadows.save()

        # The price call above is the single largest allocation this process
        # makes (yfinance/pandas frames), and it runs on a coarser cadence
        # than the news scan's own _release_memory() at the end of a cycle.
        # Without trimming here the high-water mark of the last price fetch
        # stays resident until the next scan finishes.
        _release_memory()

    @staticmethod
    def _exit_reason(position, price, now, market_open, cost):
        """Ratchet a position's stop from `price` and say which exit fires,
        if any: (reason or None, target_just_reached). The same rules for a
        real position and a skipped trade.

        `market_open` is this position's own exchange's session - True,
        False, or None when the exchange isn't one markets.py models, in
        which case there is nothing to wait for and the exits run as they
        did before any of them were gated.

        Every exit waits for that regular session - see
        config.EXITS_REGULAR_HOURS_ONLY. Prices come from the 1-minute
        chart with prepost=True, so outside it (09:30-16:00 ET in New York,
        09:00-17:30 CET in Frankfurt) they are prints - often a handful of
        shares at a spread no real exit would have crossed. Acting on one
        records a fill nobody could have got: a stop "hit" at 3am on a
        single thin print goes into the ledger as fact, and the ledger then
        measures the quote feed rather than the strategy.

        Nothing is lost by waiting. An overnight gap is still there at the
        open, and that is the first moment it could have been traded, so
        the first in-session check exits at the price the position would
        really have got. The stop's ratchet is held back along with the
        exit, not merely the exit itself: letting an after-hours spike
        raise peak_gain would pull the trailing stop up to a level that
        never traded, and stop the position out against it at the open.

        Time exits are gated on the session whoever your broker is - one
        fired at 03:00 would close on a stale print either way.
        """
        if config.EXITS_REGULAR_HOURS_ONLY and market_open is False:
            return None, False
        reason, target_reached = strategy.update_exit(position, price, cost)
        if not reason and market_open is not False:
            if WatchManager.over_age_limit(position, now):
                reason = 'max_age'
            elif now >= _parse_time(position.get('expires_at'), now):
                reason = 'horizon_expired'
        return reason, target_reached

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
