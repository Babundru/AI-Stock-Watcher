# Architecture

## Entry points

| File | What it is | Runs on |
|---|---|---|
| `gui.py` | Desktop app (customtkinter/Tkinter), native window, matplotlib charts, can drive local Ollama | Your desktop (Windows-only for the Ollama auto-start feature - see `ai_engines.md`) |
| `server.py` | Headless Flask app, serves `web/index.html`, HTTP Basic Auth, no GUI deps | The GCP VM, 24/7 via systemd |

Both construct one `main.py: StockAppBackend` and pass it three callbacks
(`log_callback`, `alert_callback`, `status_callback`). `gui.py` wires these
to Tkinter queues drained on the main thread; `server.py` wires them to
`DashboardState` (an in-memory, lock-protected buffer the Flask API polls
from). Neither entry point contains any scanning/analysis logic itself -
that's entirely in `main.py`, so the two UIs can never drift in what
counts as an alert.

## The scan loop (`main.py: StockAppBackend._run_loop`)

Runs in a daemon thread, started by `.start()`. Every `CHECK_INTERVAL`
(60s, `config.py`):

1. Fetch custom sources (`news_collector.py: fetch_from_custom_sources`)
2. If `GLOBAL_SCAN` (default on): fetch top business headlines from 4
   hardcoded RSS feeds (`NewsCollector.MARKET_RSS_FEEDS`)
3. If `GLOBAL_SCAN` is off instead: fetch news per `TARGET_COMPANIES`
4. Every article goes through `_process_article`: dedup check (last 120
   processed URLs, `data/processed_urls.json`) -> analyze
   (`self.analyzer.analyze_article`, one of three interchangeable engines,
   see `ai_engines.md`) -> notify if POSITIVE/NEGATIVE + HIGH/CRITICAL
   impact + not FLAT prediction -> open a sell-signal watch if POSITIVE
5. Every `WATCH_CHECK_INTERVAL` (5 min): `_check_watches` prices every
   open watch and closes+notifies any that hit their target or expired
   (see `portfolio_and_notifications.md`)
6. Sleeps in 1-second increments (so `.stop()` is responsive) until the
   next cycle

**Log routing gotcha**: `self.log(...)` calls in `main.py` go through
whatever `log_callback` the entry point supplied - `server.py` routes
these to the dashboard's in-memory buffer only, **not** to stdout/journal.
Only raw `print()` calls (scattered in `news_collector.py`,
`cloud_providers.py`, etc.) reach `journalctl` on the VM. If you're
debugging via SSH logs and something looks quiet, check the dashboard's
Logs tab before assuming the loop is stuck.

## Module map

```
main.py              StockAppBackend - the scan loop, orchestrates everything below
├── news_collector.py    Fetches/scrapes articles (RSS, webpage, Twitter/Nitter)
├── source_manager.py     User-added custom sources (data/news_sources.json)
├── analyzer.py           Local Ollama analysis engine
├── cloud_analyzer.py     Cloud AI analysis engine
│   ├── cloud_providers.py   Anthropic/OpenAI/OpenRouter/Routera clients
│   └── llm_prompts.py       The shared prompt (same text for local+cloud)
├── keyword_analyzer.py   Offline weighted-keyword fallback engine
│   └── keyword_manager.py   Keyword weights (data/keywords.json)
├── ollama_manager.py     Spawns/manages a local `ollama serve` process (Windows-only)
├── notifier.py            ntfy.sh push notifications + US market-hours check
├── portfolio_manager.py  Holdings (data/portfolio.json)
├── watch_manager.py       Sell-signal watches opened after a POSITIVE alert (data/watches.json)
├── price_lookup.py        Batched yfinance quote lookup, shared by watch-checking + portfolio summary
└── portfolio_history.py  Reconstructs daily portfolio value history via yfinance (server.py only)

config.py             Defaults + data/settings.json overlay (see api_keys_and_secrets.md)
local_time.py          now_local() - Europe/Bucharest, for all human-facing timestamps

gui.py                Desktop entry point
server.py              Headless entry point + Flask API + DashboardState
web/index.html          The web dashboard (vanilla JS, no build step, no framework)
```

## Config layering

`config.py` defines every setting with a safe default (committed to git,
no secrets). At import time it reads `data/settings.json` (gitignored) and
overrides anything present there. Every module imports settings directly
from `config` (`from config import X`) rather than through a passed-around
object - simple, but means changing a setting requires either restarting
the process or (for a few settings exposed via the dashboard, like
keywords) an explicit reload call. See `api_keys_and_secrets.md` for the
full settings list and how to add a new one.

## Data persistence

Everything in `data/` is flat JSON, no database. Pattern used consistently
across `portfolio_manager.py`, `source_manager.py`, `keyword_manager.py`,
`watch_manager.py`: load whole file into memory on construction, mutate,
write whole file back on every change. Fine at this scale (a handful of
tickers/sources/keywords/watches, single-process access).

| File | Managed by | In git? |
|---|---|---|
| `data/settings.json` | `config.py` (read-only there) | No - secrets |
| `data/portfolio.json` | `portfolio_manager.py` | No - personal holdings |
| `data/watches.json` | `watch_manager.py` | No - personal |
| `data/processed_urls.json` | `main.py` directly | No - runtime cache |
| `data/stats.json` | `main.py` directly | No - runtime counters |
| `data/keywords.json` | `keyword_manager.py` | Yes - just tuning, no secrets |
| `data/news_sources.json` | `source_manager.py` | Yes |

## Dependencies split

Two requirements files on purpose:
- `requirements.txt` - everything, including `customtkinter`/`matplotlib`
  (GUI-only) and `yfinance`/`pandas` (now needed server-side too, for
  sell-signal watches and the portfolio graph - see
  `portfolio_and_notifications.md`).
- `requirements-server.txt` - same minus the GUI-only packages, since the
  VM is a 1GB `e2-micro` and every unnecessary package is real RAM
  pressure. Keep this file's comment block up to date if you add a new
  dependency - it explains *why* each exclusion/inclusion exists, which
  matters more than the list itself when memory gets tight again.
