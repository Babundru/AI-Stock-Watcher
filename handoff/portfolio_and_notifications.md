# Portfolio, Exit-Signal Watches & Notifications

Three tightly-coupled pieces: what you own, what to do about it after an
alert fires, and how you're actually told.

## Portfolio (`portfolio_manager.py`, `data/portfolio.json`)

Flat dict, `{TICKER: {"buy_price": float, "shares": float, "buy_date":
"YYYY-MM-DD"}}`. `shares`/`buy_date` were added later for the value/profit
graph (see below) - `_load_portfolio()` defaults them on old entries so
nothing has to guard for a missing key. `buy_date` defaults to today if
not supplied, so a history can still be reconstructed forward even for a
holding added without one.

Used for two things in `main.py`: tagging alerts with an `OWNED` badge,
and passing `portfolio_tickers` into every analysis call so the
prompt/keyword-matcher can treat portfolio-affecting news as
higher-priority context.

## Exit-signal watches (`watch_manager.py`, `data/watches.json`)

**The idea**: an alert tells you something might move a stock, but not
when to get back out. A watch answers that. Opened automatically in
`main.py: _process_article` for any alert with a clear direction and a
resolvable ticker (skipped silently if `price_lookup.fetch_prices` can't
price it):

- **POSITIVE -> `direction: "LONG"`** - buy now, the app later says sell.
- **NEGATIVE -> `direction: "SHORT"`** - open a short CFD now, the app
  later says buy it back.

Records written before shorting existed have no `direction`; `_load()`
defaults them to `LONG`, so old `data/watches.json` files keep working.

The rules live in `strategy.py` (pure functions); `watch_manager.py`
stores each position's state between checks. Strategy `v2` (Sep 2026)
replaced rules that closed winners at a fixed +5/10% but gave losers no
floor, or postponed their time exit until they recovered - so the closed
record hovered near zero while losers piled up in the open book. Every
watch and paper trade records `strategy` so the two can be compared.

**Entry** (`main.py:_decide_trade`, cheapest checks first):
- Short gates: `ALLOW_SHORTS` / `NOTIFY_SHORTS` (both off = negative news
  on an unowned stock is skipped outright) and `SHORT_MIN_IMPACT`
  (default CRITICAL). `ALLOW_SHORTS` off with `NOTIFY_SHORTS` on sends the
  setup marked "not tracked" and opens no watch.
- One position per ticker; at most `MAX_OPEN_POSITIONS` (20) open.
- `price_lookup.fetch_context` (two small single-ticker requests), then
  `strategy.plan_trade`: skip if already moved `PRICED_IN_FRACTION` (50%)
  of the expected move since the previous close or since publication;
  stop = `STOP_ATR_MULT` (1.5) x 14-day ATR clamped to [`STOP_MIN_PCT`,
  `STOP_LOSS_PCT`]; target = expected move still ahead, clamped to
  1.5-20%; skip if target < `MIN_REWARD_RISK` (1.2) x stop.
- The AI trade confirmation (see `ai_engines.md`), then entry pricing.
- An alert whose direction contradicts an open position in the same
  ticker closes it first (`news_reversal`).

**Exit** (`strategy.update_exit` from `main.py:_check_watches`):
- **Stop** always on, stored as `stop_gain` (the stop's level as a gain on
  the position) and mirrored to `stop_loss_price` for the UI. It only
  ratchets up: it trails the best price by the initial distance, jumps to
  break-even once `BREAKEVEN_AT` (50%) of the target is gained, and - for
  a watch opened with `LET_WINNERS_RUN` (`let_run`) - tightens to
  `TRAIL_AFTER_TARGET_MULT` (0.5) x the distance once the target is hit
  (`trailing: true`, one "TARGET REACHED" notification). Fires as
  `stop_loss` below break-even, `trailing_stop` at or above it.
- **Target** closes the watch only when `let_run` is false.
- **Time exit**: `expires_at` = 15:45 ET on the Nth trading day after the
  opening session (INTRADAY 0, DAYS 3, WEEKS 15; a position opened after
  12:00 ET or at a weekend counts from the next session). Time exits,
  like `max_age`, only fire while the US regular session is open, so they
  close on a live price. Nothing is ever postponed.
- Watches from before v2 are upgraded on load (`_upgrade`): an open one
  gets its original stop or the maximum stop, and keeps closing at its
  target.
- **Only one open watch per ticker at a time, in either direction**
  (`has_open_watch` guards `add_watch`) - prevents stacking duplicate exit
  notifications if the same stock gets re-alerted while already being
  watched. Opposite-direction news closes the open one (`news_reversal`)
  before a new one can be considered.
- **Checked every `WATCH_CHECK_INTERVAL`** (5 min, coarser than the news
  scan on purpose - price doesn't need per-minute polling, and it's one
  batched `price_lookup.fetch_prices` call per check) by `main.py:
  _check_watches`, called from inside `_run_loop`. Ratcheted stops are
  saved once per pass.
- **Close reasons**, all sent through `notifier.notify_sell(...)` (a short's
  are muted when `NOTIFY_SHORTS` is off): `stop_loss`, `trailing_stop`,
  `target_hit` (only for watches without `let_run`), `news_reversal`,
  `horizon_expired`, and `max_age` - open longer than `MAX_OPEN_DAYS` (30),
  a safety net a working time exit never reaches. `max_postponed` only
  appears on records from before v2.

  A closed watch stays in `data/watches.json` (status `CLOSED`) for the
  dashboard's history; only the oldest *closed* ones get trimmed once
  total storage exceeds `MAX_STORED_WATCHES=200`. `_trim()` never drops an
  **open** watch.

- **Why the open set is bounded (Sep 2026 VM incident)**: it once could
  only grow - `_trim()` skips open watches, and a postponed loser was
  re-postponed forever. Every open watch is priced on every check, so the
  price call's cost grew with uptime, and on the 1GB VM that is the most
  likely cause of the whole machine freezing (see `incidents.md`). Now
  every position has a stop and a time exit, postponing is gone, and
  `MAX_OPEN_POSITIONS` (20, max 50 via the dashboard) caps the set -
  measured peak ~140MB RSS for a 20-position check plus the price-context
  fetches. If you raise the cap, watch the per-cycle `Memory in use` log
  line afterwards.

Dashboard surface: `GET /api/watches` (`server.py`) backs the "Watching"
card on the Alerts tab (open watches only) - see `ui.md`.

## Live prices (`price_lookup.py`)

Single shared helper, `fetch_prices(tickers) -> {ticker: price|None}`,
used by watch-checking, entry pricing, the paper ledger (`/api/paper`) and
the portfolio summary endpoint. Three stages:

1. **`_intraday_prices`** - `yf.download(period="1d", interval="1m",
   prepost=True)`, newest 1-minute close. Includes extended hours, which
   is the point: `fast_info`/`regularMarketPrice` report the last
   regular-session close and were visibly stale outside 09:30-16:00 ET.
   **Chunked to `MAX_BATCH` (25) tickers per call** - each ticker is ~960
   rows x 6 columns and yfinance builds them all before concatenating, so
   an unchunked call's peak memory scaled with the number of open
   watches.
2. Class-share retry - `BRK.B` is retried as `BRK-B` (same chunking).
3. **`_regular_session_prices`** fallback for whatever is still missing -
   `fast_info['lastPrice']` only, **at most `MAX_FALLBACK` (25) tickers**.
   It used to fall back further to `.info` (a full quote-summary fetch per
   symbol) with no cap; when Yahoo rate-limits a big download it returns
   empty, so every ticker landed here at once and the scan thread stalled
   on hundreds of sequential heavy requests. Don't reintroduce `.info`
   here.

Returns `None` for a ticker it couldn't price - callers must handle that
(watch-checking skips that watch for the cycle; a new watch simply isn't
opened if the entry price can't be resolved). yfinance/pandas are imported
lazily inside these functions (>100MB resident) - keep it that way.

## Portfolio value/profit history (`portfolio_history.py`, server-only)

`compute_history(portfolio) -> {"dates": [...], "value": [...],
"cost_basis": [...], "profit": [...]}`, backing `GET
/api/portfolio/history` and the dashboard's canvas chart. Reconstructs
**daily** value since each holding's `buy_date` using `yfinance`
historical closes (not stored snapshots - the chart is populated
immediately for existing holdings instead of only filling in from
whenever this feature shipped). Forward/back-fills gaps (weekends,
holidays, a `buy_date` that wasn't itself a trading day) so the series has
no NaNs. This is the heaviest endpoint in the app (per-ticker historical
fetch, not a cheap quote) - `GET /api/portfolio/summary` is the cheap one
for frequent polling, `history` is only refetched when the Portfolio tab
loads.

## Notifications (`notifier.py`, ntfy.sh)

All phone notifications go through `_send_ntfy()`, which POSTs to
`https://ntfy.sh/{NTFY_TOPIC}` with a 10s timeout and returns `True`/
`False` for success (added specifically so the dashboard's test button
can report whether it actually worked, not just fire-and-forget). Header
values are transliterated to ASCII (`_header_safe`) since a company name
with a curly apostrophe or CJK characters would otherwise raise
`UnicodeEncodeError` and silently drop the alert.

Three call sites, three message shapes:
- `notify_system(title, message)` - startup ("Stocks Watcher Started") and
  the dashboard's test button. Always sends if a topic is configured, no
  gating.
- `notify(company, article, analysis, is_owned)` - regular news alerts.
  **Gated to HIGH/CRITICAL impact only** inside this method (a second,
  redundant gate on top of `main.py`'s own filtering) - `NOTIFY_OWNERSHIP`
  controls whether a NEGATIVE alert on an owned stock gets an `[OWNED]`
  tag in the *notification* itself (off by default, since ntfy topics are
  public - see `api_keys_and_secrets.md`); the dashboard's Alerts tab
  shows ownership regardless, since that stays local.
- `notify_sell(ticker, company, reason, entry_price, current_price,
  target_price, article_url, direction)` - exit-signal closes. Each of the
  five close reasons (`target_hit`, `stop_loss`, `horizon_expired`,
  `max_age`, `max_postponed`) has its own emoji and explanation line;
  an unknown reason falls through to the horizon wording. Titled
  "SELL SIGNAL" for a long and "COVER SHORT SIGNAL" for a short, and the
  body reports both the raw price move and the P/L *from the position's
  side* (a short earns when the price falls, so its percentage is
  negated). Always high priority, no impact-based gating (rarer and always
  actionable, unlike news noise).

  `notify()` (the entry alert) also spells out the implied trade on its
  last line - "Action: BUY (open a long CFD)" or "Action: SHORT (open a
  short CFD)" - so the entry notification is as actionable as the exit one
  that follows it.

**Market hours** (`is_market_open()`): NYSE/Nasdaq hours, `US/Eastern`,
Mon-Fri 9:30-16:00 - used to phrase predictions as RALLY/DROP (open) vs.
GAP UP/GAP DOWN (closed), not to suppress scanning outside those hours.

**ntfy topic privacy**: the free ntfy server makes a topic a **public**
channel - the topic name is the only thing keeping it private, which is
why it's never in `config.py` (committed) and only ever in
`data/settings.json` (gitignored). See `api_keys_and_secrets.md`.
