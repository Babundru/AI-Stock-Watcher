# Stocks Watcher

A Windows desktop app that continuously scans financial news, scores each
article with a weighted-keyword algorithm, and pushes a notification to your
phone when it finds high-impact news about a traded company.

Everything runs locally. There are no API keys, no accounts, and no server —
news comes from public RSS feeds and your own list of sources.

---

## 1. Requirements

- **Windows** (the launcher is a `.bat` file)
- **Python 3.12 or newer**, installed from [python.org](https://www.python.org/downloads/)
  - During install, tick **"Add Python to PATH"**
  - The **`py` launcher** must be installed (it is included by default)
- An internet connection

## 2. Install

Open a terminal in the project folder and install the dependencies:

```
py -3.13 -m pip install -r requirements.txt
```

> **Use the same Python version you intend to run the app with.** This is the
> single most common setup problem: if you install the packages on one Python
> version and the app starts under another, it will fail to launch. The
> launcher checks versions in the order 3.13, 3.12, 3.14, then whatever `py -3`
> resolves to, and uses the first one that has every package installed. If you
> are unsure, just run the command above and the launcher will find it.

## 3. Set up phone notifications

Alerts are delivered through [ntfy.sh](https://ntfy.sh), a free push service.

1. Install the **ntfy** app on your phone ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) / [iOS](https://apps.apple.com/us/app/ntfy/id1625396347)).
2. Start the desktop app (see below), click **SYSTEM OPTIONS** in the sidebar,
   and set **NTFY NOTIFICATION TOPIC** to something long and unique —
   for example `stockwatch-7f3a91c2b8e4`.
3. Click **SAVE CONFIGURATION**, then restart the app.
4. In the phone app, tap **+** and subscribe to that exact topic name.
5. Back on the desktop, click **SYSTEM_DIAGNOSTIC** to send a test alert. It
   should arrive on your phone within a few seconds.

> **Choose your own topic before using the app.** On the free ntfy server a
> topic is effectively a public channel — anyone who knows or guesses the name
> receives your alerts. A long random name is what keeps it private. Do not
> keep the default.

## 4. Run

Double-click **`run_app.bat`**.

If something is missing, the launcher prints the problem and waits, rather than
failing silently. If it reports that no Python install has the required
packages, re-run the install command from step 2.

In the app, click **INITIALIZE WATCHER** to start scanning and **TERMINATE
PROCESS** to stop. The status indicator in the sidebar shows `ONLINE` or
`OFFLINE`.

---

## Using the app

The sidebar controls the watcher; the four tabs configure it.

| Sidebar button | What it does |
|---|---|
| `INITIALIZE WATCHER` | Starts the background scanner |
| `TERMINATE PROCESS` | Stops it and saves state |
| `RELOAD SETTINGS` | Applies keyword/source changes without restarting |
| `SYSTEM OPTIONS` | Sets your ntfy topic |
| `SYSTEM_DIAGNOSTIC` | Sends a test notification |

### LOGS
Live activity: what was fetched, how each article scored, and why an alert was
or was not sent. This is the place to look if the app seems quiet.

### PORTFOLIO
Enter a `TICKER` and optionally your `BUY PRICE`, then click **+ ADD**. The app
fetches live prices and shows profit/loss plus allocation and performance
charts. **REFRESH** re-fetches prices; **RESET DATA** wipes the list.

Holding a stock does not change whether you get alerted — it adds an `[OWNED]`
tag to negative alerts so you can tell risk to your holdings apart from general
market news.

### SOURCES
Add your own news sources by name and URL, then **+ ADD SOURCE**. Three kinds
are detected automatically:

- **RSS feeds** — best results; use these when available
- **Web pages** — the app looks for a feed link, otherwise scrapes headlines
- **Twitter/X profile URLs** — automatically converted to a Nitter mirror

Use `✓`/`○` to enable or disable a source and `×` to remove it.

### KEYWORDS
The scoring vocabulary, split into positive and negative lists, each with a
weight from 1–10. Add your own terms, remove ones causing noise, or use
**RESET TO DEFAULTS** to restore the built-in set (~200 keywords).

After editing, click **RELOAD SETTINGS** to apply the changes to a running
watcher.

---

## How alerts are decided

Understanding this will save you from thinking the app is broken.

An article is scored by which keywords it contains and, importantly, **where**:

- A keyword in the **headline** counts about **3×** what the same word counts
  in the article body — a phrase in the headline is the story, the same phrase
  buried in paragraph 30 is usually incidental.
- Only the strongest handful of signals count, so a long article cannot
  out-score a decisive headline just by being long.
- Negations are detected: *"did not show breakthrough results"* is not read as
  good news.
- Overlapping phrases are not double-counted: *"FDA approval"* scores once, not
  once as the phrase and again as *"approval"*.
- The article must look like it concerns a traded company. General news,
  politics, and personal-finance pieces are filtered out.

**You will only be notified when sentiment is clearly positive or negative
*and* impact is HIGH or CRITICAL.** Everything else is logged but not sent.

**Most articles produce no alert, and that is intended.** In a typical scan of
around 100 headlines only one or two qualify. Long quiet stretches are normal —
check the LOGS tab to confirm it is working. If you want more or fewer alerts,
see Tuning below.

---

## Tuning

Edit `config.py`:

| Setting | Default | Meaning |
|---|---|---|
| `CHECK_INTERVAL` | `60` | Seconds between scans |
| `LOOKBACK_MINUTES` | `30` | How recent an article must be to be considered |
| `GLOBAL_SCAN` | `True` | Scan all market news. Set `False` to watch only `TARGET_COMPANIES` |
| `TARGET_COMPANIES` | `[]` | Company names to track when `GLOBAL_SCAN` is off |

> Do not set `LOOKBACK_MINUTES` much below 15. The news feeds only publish
> every 10–30 minutes, so a narrower window filters out everything and the app
> will find nothing at all.

For alert sensitivity, edit the tunables at the top of `keyword_analyzer.py` —
they are named and commented. Lower `IMPACT_HIGH` for more alerts, raise it for
fewer.

---

## Your data

All settings and state live in the `data/` folder as plain JSON. Any file can
be deleted to reset it to defaults. See `data/README.md` for details.

`portfolio.json`, `settings.json`, and `processed_urls.json` are excluded from
version control, so your holdings and notification topic stay private if you
share or publish the project.

**Never put API keys or passwords in these files or in `config.py`.** The app
does not need any. If you add a secret, it can end up committed to git.

---

## Troubleshooting

**Nothing happens when I double-click `run_app.bat`**
Open a terminal in the folder and run `run_app.bat` directly to see the error.
Most likely the dependencies are installed on a different Python version — redo
step 2.

**The app runs but never alerts**
Usually normal (see above). Check the LOGS tab: if you see articles being
fetched and scored, it is working. If you see no articles at all, check your
internet connection and the SOURCES tab.

**Log shows `HTTP 401` or `HTTP 403` while scraping**
Some publishers (MarketWatch, Investing.com) block automated access to full
article text. Those articles are still analysed using their headline and
summary — just with less to go on. This is expected and not a failure.

**Test notification never arrives**
Confirm the topic in SYSTEM OPTIONS matches exactly what you subscribed to on
your phone — it is case-sensitive. Restart the app after changing it.

**Prices show `ERR` in the portfolio**
The ticker symbol was not recognised by Yahoo Finance. Check the symbol, and
note that some non-US listings need a suffix (for example `BMW.DE`).

---

## Important

This tool is a **news-alerting aid, not investment advice**. It matches
keywords; it does not understand context, sarcasm, or whether news is already
priced in. It can be wrong in both directions — flagging harmless articles and
missing significant ones. Always read the linked article and do your own
research before acting.

Market-hours detection covers weekends and regular US session hours, but does
not account for market holidays.
