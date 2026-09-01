# Data Directory

This directory contains all JSON configuration and data files used by the
Stocks Watcher application.

## Files

| File | Contents | In version control |
|---|---|---|
| `settings.json` | Your ntfy notification topic, whether alerts are muted, the alert-sensitivity threshold, local model choice, (if you enable Cloud AI) your chosen provider's API key, and (if you run `server.py`) the dashboard login | No — kept private |
| `portfolio.json` | Your stock holdings and buy prices | No — kept private |
| `processed_urls.json` | Cache of the last 120 analysed article URLs | No — runtime state |
| `stats.json` | Running counters: articles scanned, alerts (notifications) sent, skipped | No — runtime state |
| `paper_trades.json` | Track record: entry/exit price and profit or loss of every alert | No — runtime state |
| `keywords.json` | Scoring keywords with weights (~200 by default) | Yes |
| `news_sources.json` | Your custom news sources | Yes |

## Notes

All files are created automatically with sensible defaults if missing, so you
can safely delete any of them to reset. Deleting `processed_urls.json` only
means recently-seen articles may be analysed once more.

**`paper_trades.json` is the one exception — deleting it destroys history that
cannot be rebuilt.** Every other file here is either configuration or a cache
of something still obtainable; this one is the accumulated record of how the
alerts actually performed, including the intermediate prices each position
passed through. Back it up rather than resetting it. It is never trimmed.

The first three files are listed in `.gitignore` so your holdings and
notification topic are not committed if you share or publish the project.

**API keys:** if you enable Cloud AI in Settings, the API key for your
chosen provider (Anthropic, OpenAI, or an OpenAI-compatible third-party
host) is stored in `settings.json` only - never in a file tracked by git.
Don't store it anywhere else in this directory.

**Dashboard login:** `server.py` (the headless/server entry point - see
`DEPLOY.md`) reads `DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD` from
`settings.json` and requires them via HTTP Basic Auth. `gui.py` ignores
both. Leaving `DASHBOARD_PASSWORD` unset disables the login prompt.
