# UI - Web Dashboard & Desktop GUI

Two independent frontends over the same `main.py: StockAppBackend`. Pick
the section below that matches what you're changing.

## Web dashboard (`web/index.html` + `server.py`)

Single static HTML file, vanilla JS, no build step, no framework, no npm.
Served at `GET /` by `server.py` (`send_from_directory`). Everything is
poll-based against a small JSON API - there's no websocket/SSE.

### Layout

Header (always visible): connection dot + Start/Stop + alerts mute toggle
+ "Send Test Notification" button, stat tiles (Scanned/Alerts/Skipped),
current activity line, engine label. Below that, tabs: **Alerts**
(default), **Logs**, **Portfolio**, **Sources**, **Keywords**,
**Settings**.

The web dashboard is the primary front end (the backend runs as a service
on a Debian VM, reached over Tailscale from phones/laptops), so it has
everything the desktop GUI has. Feature map, with the endpoint behind it:

| Feature | Endpoint |
|---|---|
| Start / Stop | `POST /api/control` |
| Mute phone alerts | `GET/POST /api/notifications` |
| Alert sensitivity slider | `GET/POST /api/sensitivity` |
| Stop-loss % | `GET/POST /api/stop-loss` |
| Clear alerts | `DELETE /api/alerts` |
| Logs: AI-traffic filter, auto-scroll, clear | client-side only |
| Portfolio: add / remove / clear all / live P/L per holding | `POST /api/portfolio`, `DELETE /api/portfolio/<t>`, `DELETE /api/portfolio`, `GET /api/portfolio/summary` |
| Sources: add / toggle / remove / reset | `/api/sources...`, `POST /api/sources/reset` |
| Keywords: add / remove / reset, "inactive" banner | `/api/keywords...`, `POST /api/keywords/reset`, `GET /api/engine` (`ai_active`) |
| Settings: ntfy topic, ownership tag, engine + provider/model/key/base URL, Ollama model/threads/URL, paper cost, dashboard login | `GET/POST /api/settings` |
| Reload files edited by hand on the server | `POST /api/reload` |

`POST /api/settings` persists through `config.save_settings` and then
calls `backend.apply_settings()`, which rebuilds the analyzer, refreshes
the notifier's topic, and updates the paper ledger's cost - so every
change applies live, no service restart. Secrets (API key, dashboard
password) are never returned by GET; a blank value on POST keeps the
stored one, `clear_api_key: true` removes the key.

### Polling

- `pollState()` every 2s -> `GET /api/state?since=<lastLogSeq>` - returns
  incremental logs (server tracks a monotonic `seq` per log line so the
  client only receives what's new), the full current alert list, running
  status, and stats. Drives the header, the Logs tab, and the Alerts tab.
- `loadWatches()` every 15s -> `GET /api/watches`, filtered client-side to
  `status === 'OPEN'` for the "Watching" card above the alert list (hidden
  entirely when there are none). Each row shows a Long/Short badge from the
  watch's `direction`.
- Portfolio tab loads on-demand (tab click, not polled): `GET
  /api/portfolio` (holdings table), `GET /api/portfolio/summary` (cheap
  live value/profit stat tiles), `GET /api/portfolio/history` (heavier -
  drives the canvas chart, see `portfolio_and_notifications.md` for why
  it's split from `summary`).
- Sources/Keywords tabs also load on-demand via their respective
  `GET /api/{sources,keywords}`.

### Alert rendering (`renderAlerts`)

Two shapes share the same list: a regular news alert (sentiment badge,
impact/prediction line, explanation, source link) and an exit-signal alert
(`a.kind === 'sell_signal'`, shows entry->current price and % change
instead of sentiment). The exit card reads off `a.direction`: an amber
"SELL SIGNAL" badge for a long, a blue "COVER SHORT" one for a short, with
the matching close action ("Sell to close the long CFD" / "Buy back to
close the short CFD") and the P/L restated from the position's side, since
a short earns when the price falls. Both come back
from the same `/api/state` alerts array - `main.py`'s two different
`alert_callback` payload shapes (see `main.py: _process_article` vs.
`_check_watches`) are what `kind` distinguishes.

### Portfolio chart (`drawPortfolioChart`)

Hand-rolled `<canvas>` line chart, no charting library - two lines
(portfolio value, solid; cost basis, dashed) with the area between them
shaded green/red depending on whether the latest value is above or below
cost basis. Redrawn from scratch on every `loadPortfolioHistory()` call;
not incremental, fine at this data volume (one point per day since the
earliest buy date).

### Auth

The dashboard itself has no login UI - it relies entirely on the
browser's native HTTP Basic Auth prompt, triggered by `server.py`'s
`@app.before_request` hook returning 401 with a `WWW-Authenticate` header
whenever `DASHBOARD_PASSWORD` is set. Once entered, the browser caches and
resends credentials automatically on every same-origin `fetch()` call - no
token handling in the JS at all. See `api_keys_and_secrets.md`.

### Common tasks

- **New API-backed widget**: add a Flask route in `server.py`, then a
  `fetch()` + render function in the `<script>` block - follow the
  existing `loadPortfolio`/`loadSources`/`loadKeywords` pattern (fetch on
  tab activation, re-fetch after any mutation).
- **New tab**: add a `<nav.tabs>` button with `data-view="X"`, a matching
  `<section id="view-X" class="view">`, and a load call in the tab-click
  handler if it needs on-demand data.
- **Theme/styling**: all CSS is inline in the `<head>`, driven by CSS
  custom properties (`:root { --bg: ...; --accent: ...; }`) - change once,
  applies everywhere.

## Desktop GUI (`gui.py`)

customtkinter/Tkinter app. Sidebar buttons: **Start watching**, **Stop**,
**Reload config**, **Settings**, ..., **Send test alert**. Uses
`yfinance`+`matplotlib` directly for charts (`FigureCanvasTkAgg`) - this
is exactly why those two packages are GUI-only in `requirements.txt` and
excluded from `requirements-server.txt` (portfolio charting server-side
now goes through `portfolio_history.py` + a hand-rolled canvas chart
instead, specifically to avoid needing matplotlib on the VM).

- `ConsoleRedirector` (near the top of the file) pipes stdout into the
  GUI's log widget.
- **Settings dialog** (`open_settings`, ~line 459): the one place that
  writes `data/settings.json` from a UI - merges into the existing file
  rather than overwriting it (so fields the dialog doesn't expose, like
  `NOTIFY_OWNERSHIP`, don't silently revert to default). If you add a new
  `config.py` setting that should be user-editable from the desktop app,
  this is the function to extend - see `api_keys_and_secrets.md` for the
  full add-a-setting checklist.
- Requires a local Ollama install for the local-LLM engine (see
  `ai_engines.md`) - cloud AI or the keyword engine work fine without one.

The web dashboard has **no equivalent Settings dialog** - `data/
settings.json` on the VM is edited by hand over SSH (see
`deployment_vm.md`). If that becomes painful, an `/api/settings` +
dashboard form is the natural next step, mirroring `gui.py`'s dialog.
