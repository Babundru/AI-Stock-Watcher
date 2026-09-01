# Data Sources & Collection

Everything here lives in `news_collector.py`, `source_manager.py`, plus
the keyword-scoring side (`keyword_analyzer.py` + `keyword_manager.py` -
also covered here since "what counts as a signal" and "where articles
come from" are the two halves of the same discovery pipeline).

## Two independent article streams, per scan cycle

1. **Global market scan** (`fetch_general_market_news`, only when
   `GLOBAL_SCAN=True`, the default): polls 4 hardcoded RSS feeds in
   parallel (`NewsCollector.MARKET_RSS_FEEDS` - CNBC, MarketWatch, Yahoo
   Finance, Investing.com), sorts all candidates by publish time, keeps
   only the newest 20, and *only then* scrapes full article text for
   those 20 (scraping happens after the cap specifically so the app never
   pays for full-page fetches on articles that get discarded anyway).
2. **Custom sources** (`fetch_from_custom_sources`): whatever the user
   added via the dashboard/GUI, stored in `data/news_sources.json`
   (`source_manager.py`). Fetched concurrently, one thread per source.

If `GLOBAL_SCAN=False`, `fetch_news(company)` is used instead, once per
`TARGET_COMPANIES` entry - same fetch-then-cap-then-scrape shape, capped
at 10.

## Custom source types (`source_manager.py` decides + `news_collector.py` fetches)

A source is auto-classified by URL shape when added
(`_is_twitter_url`/`_is_rss_feed` in `source_manager.py`/`news_collector.py`):

- **RSS/Atom** (`_fetch_from_rss`): standard feedparser path, filters by
  `LOOKBACK_MINUTES` (default 30 - narrower would miss slower feeds, wider
  is free since dedup happens separately) and optionally by a
  company-name match string.
- **Webpage** (`_fetch_from_webpage`): scrapes a page directly for
  article-shaped links, more fragile than RSS (whatever HTML structure
  the site currently has). Anything not RSS/Twitter falls here by
  default.
- **Twitter/X** (`_fetch_from_nitter`): a raw `twitter.com`/`x.com` URL
  added via the dashboard is auto-converted to a Nitter mirror URL
  (`source_manager.py: _convert_to_nitter`) - Nitter instances are
  unreliable (frequently down/blocked), so `_fetch_from_nitter` tries a
  hardcoded list of 5 instances in order and falls back through them on
  failure. Expect this source type to be the flakiest one; a 403/503 here
  is normal, not a bug to chase.

## Scraping mechanics (`news_collector.py`)

- One shared `requests.Session()` with browser-like headers
  (`BROWSER_HEADERS`) to get past basic anti-bot checks - some sites
  (investing.com, in practice) still 403 anyway; that's the site blocking
  automated clients, not an app bug, and other sources cover the gap.
- Downloads are capped at `MAX_DOWNLOAD_BYTES` (3MB) via streaming reads -
  logged as "Response exceeded 3MB, truncating" when hit (routinely true
  for CNBC's homepage-style URLs); this is a safety cap, not an error.
- `MAX_SCRAPE_WORKERS = 6` - full-page scraping runs concurrently via
  `ThreadPoolExecutor`, since it's by far the slowest part of a cycle.
- `is_public_url()` is an SSRF guard applied to URLs *discovered inside*
  fetched pages (e.g. a link found while scraping) - blocks private/
  loopback/link-local/reserved IPs. It does **not** apply to sources the
  user explicitly configured, so pointing the app at a self-hosted feed
  on your own LAN still works on purpose.
- Dedup: `NewsCollector.is_seen` is wired by `main.py` to a
  `processed_set.__contains__` check, so an already-analyzed URL is
  skipped before it's even scraped, not just before it's re-analyzed.

## Keyword scoring algorithm (`keyword_analyzer.py`)

Used either as the sole engine (`USE_LOCAL_LLM=USE_CLOUD_AI=False`) or
implicitly as the fallback semantics every LLM prompt is calibrated
against. It's salience-based, not cumulative - the whole design exists to
stop a long article from out-scoring a decisive headline purely by
matching many weak keywords:

- **Field weighting**: title 3x, description 1.5x, body 1x (`FIELD_WEIGHTS`)
- **Body is truncated** to the first 2500 chars (`BODY_CHAR_LIMIT`) -
  scraped pages carry nav/footer/related-stories noise past that
- **Only the strongest 10 signals count** (`MAX_SIGNALS`) - decouples
  score from article length
- **Diminishing returns on repeats**: 1x, 1.4x, 1.8x, then flat
  (`REPEAT_BONUS`, `MAX_REPEATS`)
- **Negation handling**: scans the 4 words before a match
  (`NEGATION_WINDOW`) against a hardcoded negator list ("no", "not",
  "fails", "denies", ...); if most occurrences of a keyword are negated,
  its contribution flips sign and damps (`NEGATION_FACTOR = -0.5`)
- **Relevance gate** (`_is_relevant`): needs either one keyword with
  `|weight| >= 7` (`STRONG_KEYWORD`) or 3+ distinct matching keywords, AND
  either an explicit ticker, a strong keyword, or generic market-context
  vocabulary (`MARKET_CONTEXT` regex: "shares", "nasdaq", "earnings",
  etc.) - this is what stops random general-interest articles that
  happen to contain a scoring word from triggering
- **Thresholds**: `SENTIMENT_THRESHOLD=8.0` (|score| below this =
  NEUTRAL), `IMPACT_HIGH=18.0`, `IMPACT_CRITICAL=32.0`
- **Ticker/company-name extraction**: layered regexes against the
  original-case text (deliberately not upper-cased first - that made
  "(the)"/"(ceo)" look like tickers), with a stoplist (`TICKER_STOPLIST`)
  for common all-caps acronyms (CEO, USA, FDA, ...) that would otherwise
  false-positive as tickers

`data/keywords.json` (`keyword_manager.py`) holds ~230 default
positive/negative phrases with hand-tuned weights (e.g. `"bankruptcy":
-10`, `"fda approval": 9`, `"layoffs": -6`). Editable via the dashboard's
Keywords tab; `_reload_analyzer_keywords()` in `server.py` recompiles the
matcher's combined regex pattern after any add/remove so changes take
effect without a restart.

## Where to look for common tasks

- **Add a new RSS-only global source**: append to
  `NewsCollector.MARKET_RSS_FEEDS`.
- **Support a new source type** (e.g. Reddit, a different scraping
  target): add detection logic alongside `_is_twitter_url`/`_is_rss_feed`
  in `source_manager.py`, and a `_fetch_from_X` method in
  `news_collector.py`, dispatched from `fetch_from_custom_sources`'s
  `fetch_one()`.
- **Tune what counts as relevant/high-impact for the keyword engine**:
  edit `data/keywords.json` via the dashboard (no code change needed) for
  weights, or the constants at the top of `keyword_analyzer.py` for the
  scoring mechanics themselves.
- **A source is always returning 0 articles**: check `LOOKBACK_MINUTES`
  isn't too narrow for how often that feed actually publishes, and check
  it isn't simply blocking the scraper (403/timeout in the logs) - that's
  usually the site, not a bug.
