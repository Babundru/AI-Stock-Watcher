# Stocks Watcher

A Windows desktop app that continuously scans financial news, has a **local AI
model** judge each article's market impact, and pushes a notification to your
phone when it finds something significant.

Everything runs on your machine. There are no API keys, no accounts, and no
cloud service — news comes from public RSS feeds and your own list of sources,
and the analysis runs on a local [Ollama](https://ollama.com) model.

---

## 1. Requirements

- **Windows** (the launcher is a `.bat` file)
- **Python 3.12 or newer**, installed from [python.org](https://www.python.org/downloads/)
  - During install, tick **"Add Python to PATH"**
  - The **`py` launcher** must be installed (it is included by default)
- **[Ollama](https://ollama.com/download)**, with one model pulled
- An internet connection
- Enough RAM for your chosen model (roughly 8 GB for a 12B model)

## 2. Install

Open a terminal in the project folder and install the dependencies:

```
py -3.13 -m pip install -r requirements.txt
```

Then pull a model for Ollama to run:

```
ollama pull gemma3:12b
```

You do not need to start Ollama yourself — the app starts it when you click
**Start watching**, and attaches to it if it is already running. If you
pull a different model, set its name in **Settings**.

> **Use the same Python version you intend to run the app with.** This is the
> single most common setup problem: if you install the packages on one Python
> version and the app starts under another, it will fail to launch. The
> launcher checks versions in the order 3.13, 3.12, 3.14, then whatever `py -3`
> resolves to, and uses the first one that has every package installed. If you
> are unsure, just run the command above and the launcher will find it.

## 3. Set up phone notifications

Alerts are delivered through [ntfy.sh](https://ntfy.sh), a free push service.

1. Install the **ntfy** app on your phone ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) / [iOS](https://apps.apple.com/us/app/ntfy/id1625396347)).
2. Start the desktop app (see below), click **Settings** in the sidebar,
   and set **Notification topic** to something long and unique —
   for example `stockwatch-7f3a91c2b8e4`.
3. Click **Save**, then restart the app.
4. In the phone app, tap **+** and subscribe to that exact topic name.
5. Back on the desktop, click **Send test alert**. It
   should arrive on your phone within a few seconds.

> **Choose your own topic before using the app.** On the free ntfy server a
> topic is a public channel — anyone who knows or guesses the name can read
> every alert you send. A long random name is the only thing keeping it
> private. Do not keep the default.
>
> Set it in the app (Settings), which writes to `data/settings.json`. That file
> is gitignored. **Never put your real topic in `config.py`** — that file is
> tracked, so it would be published along with the code.

## 4. Run

Double-click **`run_app.bat`**.

If something is missing, the launcher prints the problem and waits, rather than
failing silently. If it reports that no Python install has the required
packages, re-run the install command from step 2.

In the app, click **Start watching** to begin, and **Stop** to halt. The
sidebar shows whether it is watching or offline.

---

## Using the app

The sidebar controls the watcher; the four tabs configure it.

| Sidebar button | What it does |
|---|---|
| `Start watching` | Starts the background scanner |
| `Stop` | Stops it and saves state |
| `Reload config` | Applies keyword/source changes without restarting |
| `Settings` | Notification topic and AI model |
| `Send test alert` | Sends a test notification |

The panel under the logo shows whether the watcher is running, which engine and
model are active, and what it is doing right now. The counters at the bottom
track articles scanned, alerts raised, and articles skipped this session —
worth watching, since a single LLM analysis can take a minute with no other
sign of progress.

### Alerts
The default view, and the app's actual output: every alert it has raised this
session, newest first. Each card shows the company and ticker, an impact badge,
sentiment, the headline, the analyst explanation, the price prediction, and an
**Open article** button. Holdings from your portfolio are tagged `Owned`.

Alerts are kept for the current session only — **Clear** empties the list.

### Logs
Live activity, colour-coded: alerts in green, errors in red, routine chatter
dimmed. This is the place to look if the app seems quiet.

Two toggles in the toolbar:

- **AI traffic** — off by default. Turn it on to see the full prompt sent to
  the model and its raw reply. Useful for debugging, but it is verbose enough
  to bury everything else.
- **Auto-scroll** — turn it off to read back through history without being
  yanked to the bottom by new lines.

### Portfolio
Enter a ticker and optionally your buy price, then click **Add**. The app
fetches live prices and shows profit/loss plus performance
charts. **Refresh** re-fetches prices; **Clear all** wipes the list.

Holding a stock does not change whether you get alerted — it adds an `Owned`
tag to negative alerts so you can tell risk to your holdings apart from general
market news.

### Sources
Add your own news sources by name and URL, then **Add source**. Three kinds
are detected automatically:

- **RSS feeds** — best results; use these when available
- **Web pages** — the app looks for a feed link, otherwise scrapes headlines
- **Twitter/X profile URLs** — automatically converted to a Nitter mirror

Use the toggle to enable or disable a source, and `×` to remove it.

### Keywords
The scoring vocabulary used by the offline analyzer, split into positive and
negative lists, each with a weight from 1–10. Add your own terms, remove ones
causing noise, or use **Reset** to restore the built-in set
(~200 keywords).

**These only take effect when `USE_LOCAL_LLM = False`.** While the LLM is doing
the analysis the tab shows an inactive banner, because editing keywords then
would change nothing.

After editing, click **Reload config** to apply the changes to a running
watcher.

---

## How alerts are decided

Each article is sent to the local model, which is asked to first judge whether
the news could actually move a stock price. Routine reports, rehashes, opinion
pieces and general fluff are rejected outright; earnings, mergers, FDA
decisions, lawsuits, contracts, analyst moves and macro events are analysed
further. For anything it keeps, the model returns the affected company, its
ticker, a sentiment, an impact rating, and a short explanation.

Stocks in your Portfolio are named in the prompt and explicitly treated as
higher relevance.

**You will only be notified when sentiment is clearly positive or negative
*and* impact is HIGH or CRITICAL.** Everything else is logged but not sent.

**Analysis is slow, and that is normal.** A local model takes roughly one
minute per article on CPU, so a scan of a dozen articles can take well over ten
minutes. The Logs tab shows the prompt sent and the reply received, so you can
watch it work. Raising `OLLAMA_NUM_THREADS` speeds this up at the cost of
making the machine busier.

**Most articles produce no alert, and that is intended.** Long quiet stretches
are normal — check the Logs tab to confirm it is working.

### Running without a model

If you would rather not run an LLM, set `USE_LOCAL_LLM = False` in `config.py`.
The app falls back to an offline weighted-keyword scorer that needs no model
and analyses instantly. It is cruder — it matches vocabulary rather than
understanding the article — but it weights headline mentions far above body
text, handles negation, and filters non-market news. Its vocabulary is editable
in the Keywords tab.

---

## Tuning

Edit `config.py`:

| Setting | Default | Meaning |
|---|---|---|
| `CHECK_INTERVAL` | `60` | Seconds between scans |
| `LOOKBACK_MINUTES` | `30` | How recent an article must be to be considered |
| `GLOBAL_SCAN` | `True` | Scan all market news. Set `False` to watch only `TARGET_COMPANIES` |
| `TARGET_COMPANIES` | `[]` | Company names to track when `GLOBAL_SCAN` is off |
| `USE_LOCAL_LLM` | `True` | `False` uses the offline keyword scorer instead |
| `LOCAL_MODEL_NAME` | `gemma3:12b` | Ollama model to run. Must be pulled first |
| `OLLAMA_NUM_THREADS` | `1` | Threads the model may use. Higher is faster, but busier |
| `OLLAMA_URL` | localhost:11434 | Change if Ollama runs on another host |

> Do not set `LOOKBACK_MINUTES` much below 15. The news feeds only publish
> every 10–30 minutes, so a narrower window filters out everything and the app
> will find nothing at all.

To change how the model judges articles, edit the prompt in
`analyzer.py` — the relevance rules and the impact definitions (what counts as
CRITICAL vs HIGH) are written out in plain English there.

If you switched to the keyword scorer, its tunables are at the top of
`keyword_analyzer.py`. Lower `IMPACT_HIGH` for more alerts, raise it for fewer.

---

## Privacy

Your holdings are the most sensitive thing here. Where they go:

| Destination | What it learns | Notes |
|---|---|---|
| **Local AI (Ollama)** | Your ticker list, included in each prompt | Stays on your machine, as long as `OLLAMA_URL` points at localhost |
| **Yahoo Finance** | Your tickers, whenever prices refresh | Unavoidable if you want live prices. Leave the Portfolio empty to avoid it |
| **ntfy.sh** | The alerts you receive | The topic is public — see above |

`portfolio.json` and `settings.json` are gitignored, so holdings and your topic
are never committed.

By default the app does **not** disclose ownership in notifications.
`NOTIFY_OWNERSHIP = False` in `config.py` keeps the `[OWNED]` marker out of the
notification, since it would tell anyone reading the public topic which stocks
you actually hold. The Alerts tab still shows an `Owned` badge locally. Set it
to `True` only if your topic is genuinely private.

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
Usually normal (see above). Check the Logs tab: if you see articles being
fetched and analysed, it is working. If you see no articles at all, check your
internet connection and the Sources tab.

**Log shows `ERROR: 'ollama' command not found`**
Ollama is not installed or not on PATH. Install it from
[ollama.com/download](https://ollama.com/download) and restart the app.

**Log shows `Local LLM Error:` or the model never responds**
The model named in Settings is probably not pulled. Run `ollama list` to
see what you have, then `ollama pull <name>`, or change the name to match. If
analysis simply times out, the model may be too large for your machine — try a
smaller one such as `phi3:mini`.

**Analysis is extremely slow**
Expected on CPU. Raise `OLLAMA_NUM_THREADS`, use a smaller model, or set
`USE_LOCAL_LLM = False` to use the instant keyword scorer.

**Log shows `HTTP 401` or `HTTP 403` while scraping**
Some publishers (MarketWatch, Investing.com) block automated access to full
article text. Those articles are still analysed using their headline and
summary — just with less to go on. This is expected and not a failure.

**Test notification never arrives**
Confirm the topic in Settings matches exactly what you subscribed to on
your phone — it is case-sensitive. Restart the app after changing it.

**Prices show `ERR` in the portfolio**
The ticker symbol was not recognised by Yahoo Finance. Check the symbol, and
note that some non-US listings need a suffix (for example `BMW.DE`).

---

## Important

This tool is a **news-alerting aid, not investment advice**. A small local
language model can misread an article, invent a ticker, or miss that news is
already priced in, and it will state wrong conclusions just as confidently as
right ones. It can fail in both directions — flagging harmless articles and
missing significant ones. Always read the linked article and do your own
research before acting.

Market-hours detection covers weekends and regular US session hours, but does
not account for market holidays.
