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
3. Click **Save**. It applies immediately, including to a running watcher.
4. In the phone app, tap **+** and subscribe to that exact topic name.
5. Back on the desktop, click **Send test alert**. It
   should arrive on your phone within a few seconds.

### Muting alerts

The **Phone alerts** switch in the sidebar (or the **Alerts** toggle in the web
dashboard header) mutes notifications without touching your topic — so you can
silence the app overnight and turn it back on without retyping anything.

Muting stops only the push to ntfy.sh. The app keeps scanning, analysing,
opening and closing watches, and recording paper trades; alerts still appear in
the Alerts tab and the log. Nothing about the track record changes, so muting
does not create a gap in it.

The switch takes effect immediately — no restart — and is saved to
`data/settings.json`, so it survives one.

> **There is no default topic — phone alerts are off until you set one.**
> That is deliberate: a shipped default would be a shared public channel, with
> every user who never changed it publishing alerts to the same place and
> reading everyone else's.
>
> On the free ntfy server a topic is a public channel — anyone who knows or
> guesses the name can read every alert you send. A long random name is the
> only thing keeping it private, so make it long and random.
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

There are two front ends over the same engine, and they have the same
features:

- **The desktop window** (`run_app.bat` / `gui.py`), described below.
- **The web dashboard** (`server.py`, see `DEPLOY.md`), for a backend that
  runs unattended on a server and is opened from a phone or laptop over
  Tailscale. Its tabs mirror the desktop ones, and its **Settings** tab
  covers everything the desktop Settings dialog does - notification topic,
  AI engine and API key, paper cost, dashboard login - and applies changes
  to the running service immediately, with no restart.

The sidebar controls the watcher; the four tabs configure it.

| Sidebar button | What it does |
|---|---|
| `Start watching` | Starts the background scanner |
| `Stop` | Stops it and saves state |
| `Reload config` | Re-reads the JSON files in `data/` if you edited them by hand |
| `Settings` | Notification topic and AI model |
| `Phone alerts` | Master switch — mutes or unmutes phone notifications |
| `Alert on` | Slider — the weakest impact rating that raises an alert |
| `Send test alert` | Sends a test notification |

The panel under the logo shows whether the watcher is running, which engine and
model are active, and what it is doing right now. The counters at the bottom
track articles scanned, alerts raised, and articles skipped — worth watching,
since a single LLM analysis can take a minute with no other sign of progress.
These counters persist across restarts (`data/stats.json`), so they keep
accumulating rather than resetting to zero each time you start the app.

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
Two views, switched with the toggle at the top.

**Holdings** — enter a ticker and optionally your buy price, then click
**Add**. The app fetches live prices and shows profit/loss plus performance
charts. **Refresh** re-fetches prices; **Clear all** wipes the list.

**Paper trades** — the app's own track record: what each alert would have
earned or lost. Open positions are marked to live prices; closed ones are the
actual record. Nothing here is a stock you own, and nothing you own appears
here. See *Is it actually making money?* below.

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

Edits made in the tab reach a running watcher immediately, as do holdings
and sources. **Reload config** is for changes made to the JSON files by hand
while the app is open.

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
*and* impact clears the sensitivity threshold.** Everything else is logged but
not sent.

### Alert sensitivity

The **Alert on** slider in the sidebar — or the one at the top of the web
dashboard's Alerts view — sets the weakest impact rating that raises an alert.
It applies immediately, with no restart, and is saved to `data/settings.json`.

| Setting | What gets through | Expected move |
|---|---|---|
| `CRITICAL` | Game-changing news only, a handful a month | 15%+ |
| `HIGH` | **Default.** Significant events | 5–15% |
| `MEDIUM` | Routine news as well — many more alerts | 2–5% |
| `LOW` | Everything the analyser did not reject outright | noise |

The threshold also sets the exit target, since a smaller expected move needs a
nearer one: 10% for CRITICAL, 5% for HIGH, 3% for MEDIUM, 2% for LOW. Holding a
MEDIUM alert out for the 5% a HIGH gets would time-stop almost every time.

> **Lowering it does not make the app smarter — it makes it louder.** The
> analyser is least reliable exactly where it rates news weakest, so MEDIUM and
> below carry a much higher share of misreads. The honest way to decide is the
> paper record: get enough closed trades at HIGH to compare against, then try a
> lower setting and see whether expectancy survives.

### Entry and exit alerts

Every alert that passes the filter comes in two parts: the news alert telling
you to open a position, and a later alert telling you to close it.

| News | Entry alert says | The app then watches for | Exit alert says |
|---|---|---|---|
| Positive | **BUY** — open a long CFD | the price rising to the target | **SELL SIGNAL** — close the long |
| Negative | **SHORT** — open a short CFD | the price falling to the target | **COVER SHORT** — buy the CFD back |

The target is 5% away from the price at the moment of the alert, or 10% for
CRITICAL impact — above entry for a long, below it for a short. If the move
does not happen within the expected window (1 day, 5 days, or 21 days,
depending on how far ahead the analyser expects the news to matter), the exit
alert fires anyway as a time stop, telling you to reassess rather than sit in
the position indefinitely.

The exit alert reports both the raw price move and your profit or loss *on the
position* — for a short those have opposite signs, since a short earns when the
price falls. Open positions awaiting their exit alert are listed in the
**Watching** card at the top of the Alerts tab, tagged Long or Short.

Only one position per stock is watched at a time: if a stock you already have
an open watch on gets alerted again, no second watch is opened.

Removing a watch by hand from the web dashboard's **Watching** card also
drops its open paper position, since no exit signal will ever fire for it.
Closed paper trades are never touched.

> Shorting via CFDs carries losses that are not capped by the amount you put
> in — a stock can rise without limit. These alerts are a news-timing aid, not
> a risk-managed strategy, and they say nothing about position size.

### Is it actually making money?

The app keeps its own track record. Every alert already makes a complete
round trip — it opens a position at a live price and closes it at a live
price — so each one is recorded as a paper trade in `data/paper_trades.json`,
along with what the market (SPY) did over the identical window.

Nothing about the app's behaviour changes when this is on. It is a record of
the trades it already makes, not a separate "test mode": a mode that entered
or exited on different rules would measure something other than the app you
would actually run.

**Where to see it.** The **Portfolio** tab has two views, switched with the
toggle at the top:

- **Holdings** — stocks you actually own, entered by hand. Unchanged.
- **Paper trades** — the app's record: open positions marked to live prices,
  every closed trade, and the totals across the top.

The web dashboard shows the same thing under the holdings table in its
Portfolio view, with an equity curve.

The two are deliberately kept apart, and the paper positions are **never**
written into your real portfolio. Your holdings list is fed to the analyser
as higher-relevance context and drives the `Owned` tag, so mixing simulated
positions in would change which articles the app alerts on — corrupting the
very record you are trying to measure. It would also mean `Clear all` wiped
your track record.

There is also a terminal report, which shows more:

```
py -3.13 paper_report.py            summary
py -3.13 paper_report.py --trades   every closed trade
py -3.13 paper_report.py --stops    what a stop loss would have done
```

The number that decides it is **expectancy** — average profit or loss per
trade, after costs. Win rate alone is misleading: 60% winners is a losing
system if the losers are twice the size of the winners.

Two things the report shows that are easy to miss:

- **Alpha, not just profit.** A profitable month during a rising market may
  just be drift. Alpha compares each trade against what simply being in the
  market that week would have paid, so a short that gained 3% while the
  market rose 2% is credited with beating its baseline by 5%.
- **A stop-loss study.** The app has no stop loss — a position runs to its
  target or to its horizon. But the worst price each position passed through
  is recorded live, so `--stops` can show retroactively what a 3% or 5% stop
  would have done. That is the one measurement that cannot be reconstructed
  later, which is why it is logged from the start.

Set `PAPER_COST_PCT` in `config.py` to your broker's real round-trip cost
(spread + commission + overnight financing). At the default 0.2% a 5% target
keeps most of its gain, but on a wide spread the same strategy can be a loser
while every price move still goes your way.

**Give it time.** Below roughly 30 closed trades the figures are noise, and
the alert filter is deliberately selective (HIGH/CRITICAL, clear sentiment,
one position per stock), so reaching that can take months. The report says so
at the top until you get there.

> Only closed positions count. A trade is recorded when its sell/cover signal
> fires, so open positions do not appear in the results.
>
> One caveat worth knowing: watches are only checked while the app is
> running. If it is off when a target is touched, the exit is recorded later
> at whatever the price has become — which usually understates the winners.
> Running `server.py` continuously (see `DEPLOY.md`) avoids this; running the
> desktop app a few hours a day does not.

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
| `NOTIFICATIONS_ENABLED` | `True` | Master mute for phone alerts (use the in-app toggle) |
| `MIN_IMPACT` | `HIGH` | Weakest impact that alerts (use the in-app slider) |
| `PAPER_TRADING` | `True` | Record every alert's profit/loss to `data/paper_trades.json` |
| `PAPER_COST_PCT` | `0.002` | Round-trip trading cost, subtracted from every trade |
| `PAPER_BENCHMARK` | `SPY` | Priced alongside each trade to measure alpha |
| `STOP_LOSS_PCT` | `0.0` | `0` = watches close on schedule as before. Above 0, a scheduled (horizon-expiry) exit at a loss is postponed instead of sold, until it's profitable, hits its target, or falls this far past entry (use the in-app control) |

> Do not set `LOOKBACK_MINUTES` much below 15. The news feeds only publish
> every 10–30 minutes, so a narrower window filters out everything and the app
> will find nothing at all.

To change how the model judges articles, edit the prompt in
`analyzer.py` — the relevance rules and the impact definitions (what counts as
CRITICAL vs HIGH) are written out in plain English there.

If you switched to the keyword scorer, its tunables are at the top of
`keyword_analyzer.py`. `IMPACT_HIGH` and `IMPACT_CRITICAL` are the score
thresholds it uses to *assign* a rating; the **Alert on** slider decides which
of those ratings you actually hear about. Adjust the slider first — it needs no
restart and no code change.

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
your phone — it is case-sensitive — and that the **Phone alerts** switch is
on.

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
