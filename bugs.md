# Bugs and inefficiencies in the scan pipeline

Found in a review on 2026-09-23. Target machine: a small VM with **1 GB of RAM
and 30 GB of disk**, so fixes are chosen to add no new dependencies and hold
no more than a few hundred KB of extra state.

Status: **fixed** / **open** / **won't fix** (with the reason).

## Bugs

### B1. Reddit posts lost when they aren't analysed — fixed
`main.py:_process_article`, `reddit_source.py:_on_thread`

`RedditPoller` returns each ready post once and then forgets it. When the
scan loop didn't analyse it, the post was gone for good:
- the engine had already failed earlier in the scan (`_engine_down`): the post
  was skipped without a word;
- the engine failed on the post itself: the log said "retrying on the next
  scan", but Reddit never offered it again;
- `_engine_down` was never cleared during the weekend wait, so one failure
  dropped every Reddit post for the next 25 minutes;
- a stop in the middle of the loop dropped the rest of the batch.

### B2. Short API outages permanently drop articles — fixed
`main.py:_analysis_failed`

After a failure the rest of the scan waits, so only the **first** article of
each scan was charged a retry. A three-minute outage (three scans) used up all
`MAX_ANALYSIS_ATTEMPTS` on that same article and wrote it off, even though the
article was never the problem.

### B3. Brotli responses can't be read — fixed
`news_collector.py:BROWSER_HEADERS`

The session sent `Accept-Encoding: gzip, deflate, br`, but neither `brotli` nor
`brotlicffi` is installed, so a server that picked `br` returned unreadable
bytes. Tested: City AM (a built-in feed) does. The feed only worked because a
second fetch went through feedparser on every scan, and every City AM article
page came back empty, so those stories were judged on the headline alone.

### B4. Wrong sites get turned into Twitter sources — fixed
`source_manager.py:_is_twitter_url` / `_convert_to_nitter`

`'x.com/' in url` matches `vox.com/`, `fedex.com/` and so on. Adding
`https://www.vox.com/business` became a Nitter source for user `www.vo`. Even a
real `https://www.x.com/name` produced the user name `www.`.

### B5. The ntfy topic is printed to the logs — fixed
`config.py:reload_from_disk`

Loading the config printed `Topic=<topic>` to stdout, which ends up in journald
on the VM. The topic name is the only thing keeping the alerts private.

### B6. Links found on scraped pages skip the private-address check — fixed
`news_collector.py:_fetch_from_webpage`

A feed link found inside a page is checked with `is_public_url`, but article
links pulled out of a page were scraped without that check.

### B7. Previous close is wrong for European stocks — fixed
`price_lookup.py:_context_for`

The previous close was chosen with a hard-coded `hour >= 16`, which is the US
close. European venues close between 16:20 and 18:30 local time, so the choice
could include today's unfinished daily bar (affecting the close move and ATR).

### B8. AI replies may be cut off — fixed
`cloud_providers.py`

`max_tokens=1024`, while the prompt asks for step-by-step reasoning first. With
reasoning models the budget can run out before the JSON is written. The reply
then came back empty or cut off, counted as "engine down", and the stop reason
was never logged.

### B9. Startup crash on a damaged URL cache — fixed
`main.py:_load_processed_urls`

If `processed_urls.json` held valid JSON that was neither a list nor a dict,
the function returned `None` and `set(None)` crashed startup.

### B10. The text "false" is read as true — fixed
`config.py:_BOOL`

`bool("false")` is `True`, so a hand-edited `"false"` string in
`settings.json` switched a setting on.

### B11. Dead setting in settings.json — open (user data)
`data/settings.json` contains `AI_TRADE_CONFIRM: true`, which nothing reads.
It's harmless and is ignored on load. It's in a personal, gitignored file,
so it's left for you to delete by hand.

## Inefficiencies

### I1. Every article goes to the AI, even obvious noise — fixed
Earnings-call transcripts, "stocks to buy" lists, conference-presentation
notices and personal-finance pieces are all things the prompt already calls
irrelevant, yet each one cost a page scrape and a paid LLM call. A conservative
headline filter now drops them in the collector, before the scrape.

### I2. During an outage, the same articles are re-scraped every minute — fixed
Unanalysed articles aren't marked as seen, so an outage re-downloaded and
re-parsed up to about 24 feed articles plus custom ones on every scan. A small
bounded cache of scraped text (64 entries, trimmed to what the analysers read)
now serves the retries.

### I3. Exit checks wait for the news scan to finish — fixed
`_check_watches` ran only after a full scan. With articles analysed one after
another (up to 120 s x 2 attempts per model), stops and targets could be
checked long after their 5-minute interval. The watch check now also runs
between articles once it is due.

### I4. The same story is analysed several times — fixed
Duplicates were spotted only by exact URL, so the same story from Yahoo US and
Yahoo UK, or with tracking parameters (`?mod=...`, `?source=...`, `utm_*`), cost
another AI call and could send a duplicate alert. Tracking parameters are now
stripped, and recent headlines are remembered (bounded, in memory).

### I5. Watchlist mode re-downloads every feed for each company — fixed
With `GLOBAL_SCAN` off, each company caused a fresh download of every feed
(N x 8 per scan). Feeds are now downloaded once per scan and matched per
company, as whole words ("Meta" no longer matches "metal").

### I6. Extra Yahoo downloads per trade — fixed
`_decide_trade` built the price context (which includes the latest 1-minute
price for the stock and the benchmark), then downloaded both again with
`fetch_prices` for the entry. The context's own prices are used now; the
download only happens if the context has no price.

### I7. Dead Nitter instances cost time every scan — fixed
Each Twitter source tried up to 5 instances in turn with 15 s timeouts, every
scan. Instances that fail now back off for 30 minutes, and the last working one
is tried first.

### I8. Failing feeds are fetched twice every scan — fixed
Any failure, including timeouts, connection errors and consent walls, retried
the feed through feedparser's own fetch (30 s timeout, no size cap). It now
falls back only when the site's response itself was the problem (HTTP error or
a body that didn't parse).

### I9. Parsed pages aren't cleaned up — fixed
The custom-webpage and Nitter paths built full BeautifulSoup trees and never
called `decompose()`, so their reference cycles lived until the next gc pass.

### I10. Chatty prints on every scan — fixed
`print`s per scraped article and per feed per scan went to journald on the VM,
against the rule in `handoff/architecture.md`. Success and progress lines are
gone; failures still print one line each.

### I11. Custom copies of built-in feeds skip the scan cap — fixed
A custom source with the same URL as a built-in feed replaced it, so it skipped
the 24-article cap and the one-per-feed rotation, and got only 10 entries
instead of 25. A duplicate that adds nothing (same "reporting" trust) is now
skipped in favour of the built-in feed. One whose trust you changed still
replaces it.

### I12. Out-of-date doc — fixed
`handoff/architecture.md` said 120 processed URLs are kept; the code keeps 500.
The scan-loop section now also describes the retry, backlog and dedup
behaviour above.

## Memory added by the fixes

All bounded, and all small next to one yfinance call:

| State | Bound | Rough size |
|---|---|---|
| Scrape cache (`NewsCollector._scraped`) | 64 entries x 6000 chars | < 0.5 MB |
| Reddit backlog (`_reddit_backlog`) | 60 posts x ~4.5 KB | < 0.3 MB |
| Recent headlines (`processed_titles`) | 500 short strings | < 0.1 MB |
| Outage strikes (`_outage_strikes`) | 500 URLs | < 0.1 MB |

Article text is also now capped at 6000 characters as it is scraped, which
lowers the peak while a scan holds all its articles at once.

## Known limits

- The Reddit backlog lives in memory: a restart while the engine is down
  still loses the posts waiting in it.
- A pass now makes two failing calls before calling the engine down (it was
  one), so it can tell an outage apart from one unreadable article.
- The headline filter (I1) and headline dedup (I4) are deliberately
  narrow. A headline has to be 5+ words to be deduplicated. Anything the
  filter drops is logged nowhere, so if a real story ever seems to be
  missing, check `_NOISE_HEADLINE` in `news_collector.py` first.

## Verification

- `py -m unittest discover -s tests`: 108 tests pass. The new ones are in
  `tests/test_pipeline_fixes.py` (outage and retry charging, Reddit backlog,
  headline dedup, URL cleaning, noise filter, Twitter detection, bool
  settings, European close) and `tests/test_news_collector.py` (scrape cache).
  The European-close test needs pandas; it ran in a scratch venv.
- Live, on the desktop: one full scan through `StockAppBackend` with the
  keyword engine, notifications muted and a temp data directory. City AM
  pages now decode, custom duplicates of built-in feeds were skipped, and no
  tracking parameters were left in URLs.
- Not run live: the yfinance paths (`fetch_context`, entry pricing) and the
  cloud-AI token warning. yfinance isn't installed on the desktop, so check
  the first trade and the first "ran out of tokens" line on the VM.
