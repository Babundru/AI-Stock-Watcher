# Data Directory

This directory contains all JSON configuration and data files used by the Stocks AI application.

## Files:

- **settings.json** - User configuration (NTFY topic for notifications)
- **keywords.json** - Sentiment analysis keywords with weights (230+ keywords)
- **news_sources.json** - Custom news sources for scraping
- **portfolio.json** - Your stock portfolio
- **processed_urls.json** - Cache of processed article URLs (auto-managed)

## Note:

All files are automatically created with defaults if they don't exist. You can safely delete any file to reset it to defaults, except `processed_urls.json` which tracks which articles have been analyzed.
