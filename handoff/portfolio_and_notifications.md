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

- **Target price**: `entry_price * (1 +/- target_pct)` - above entry for a
  long, below it for a short - where `target_pct` is 10% for CRITICAL
  impact, 5% for HIGH (or DEFAULT 5% for anything else), `TARGET_PCT` in
  `watch_manager.py`.
- **Expiry**: `HORIZON_DAYS` maps the LLM/keyword-analyzer's `horizon`
  field (INTRADAY/DAYS/WEEKS) to 1/5/21 calendar days. Defaults to DAYS if
  the analyzer didn't provide one.
- **Only one open watch per ticker at a time, in either direction**
  (`has_open_watch` guards `add_watch`) - prevents stacking duplicate exit
  notifications if the same stock gets re-alerted while already being
  watched, and stops a later opposite-sentiment article opening a
  contradictory position on top of the first.
- **Checked every `WATCH_CHECK_INTERVAL`** (5 min, coarser than the news
  scan on purpose - price doesn't need per-minute polling, and it's one
  batched `price_lookup.fetch_prices` call per check) by `main.py:
  _check_watches`, called from inside `_run_loop`.
- **Closes** when either the current price reaches `target_price`
  (`reason="target_hit"`, direction-aware: `>=` target for a long, `<=`
  for a short, via `WatchManager.target_reached`) or `now >= expires_at`
  with no target hit (`reason="horizon_expired"`) - fires
  `notifier.notify_sell(...)` either way. A closed watch stays in `data/watches.json` (status `CLOSED`) for
  the dashboard's history; only the oldest *closed* ones get trimmed once
  total storage exceeds `MAX_STORED_WATCHES=200` - open watches are never
  dropped.

Dashboard surface: `GET /api/watches` (`server.py`) backs the "Watching"
card on the Alerts tab (open watches only) - see `ui.md`.

## Live prices (`price_lookup.py`)

Single shared helper, `fetch_prices(tickers) -> {ticker: price|None}`,
used by both watch-checking and the portfolio summary endpoint. Batches
all tickers into one `yfinance.Tickers(...)` call rather than one request
per ticker. Tries the lightweight `fast_info` first, falls back to the
slower full `.info` only if that has nothing. Returns `None` for a ticker
it couldn't price - callers must handle that (watch-checking just skips
that watch for the cycle; a new watch simply isn't opened if the entry
price can't be resolved).

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
  target_price, article_url, direction)` - exit-signal closes. Titled
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
