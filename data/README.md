# Data Directory

This directory contains all JSON configuration and data files used by the
Stocks Watcher application.

## Files

| File | Contents | In version control |
|---|---|---|
| `settings.json` | Your ntfy notification topic | No — kept private |
| `portfolio.json` | Your stock holdings and buy prices | No — kept private |
| `processed_urls.json` | Cache of the last 120 analysed article URLs | No — runtime state |
| `keywords.json` | Scoring keywords with weights (~200 by default) | Yes |
| `news_sources.json` | Your custom news sources | Yes |

## Notes

All files are created automatically with sensible defaults if missing, so you
can safely delete any of them to reset. Deleting `processed_urls.json` only
means recently-seen articles may be analysed once more.

The first three files are listed in `.gitignore` so your holdings and
notification topic are not committed if you share or publish the project.

**Do not store API keys or passwords here.** The app does not require any.
