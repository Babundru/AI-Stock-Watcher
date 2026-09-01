# Handoff Index

Start here. This folder exists so a fresh Claude Code session (or you,
months from now) can load only the context relevant to the task at hand,
instead of re-deriving the whole codebase from scratch every time. Point a
new session at the one or two files that match what you're working on.

**No secrets live in this folder or anywhere in git.** Every real
credential (API keys, ntfy topic, dashboard password) lives only in
`data/settings.json`, which is gitignored. These files describe where
things are, never what the values are.

## Files in this folder

| File | Read this when you're touching... |
|---|---|
| [`architecture.md`](architecture.md) | Anything cross-cutting - the scan loop, module boundaries, config/settings layering, data persistence, entry points |
| [`ai_engines.md`](ai_engines.md) | The analysis engines (cloud AI / local Ollama / keyword matcher), prompts, Routera/provider routing |
| [`data_sources.md`](data_sources.md) | News collection - RSS feeds, webpage scraping, Twitter/Nitter, custom sources, the keyword-scoring algorithm |
| [`portfolio_and_notifications.md`](portfolio_and_notifications.md) | Portfolio tracking, sell-signal watches, ntfy notifications |
| [`ui.md`](ui.md) | The web dashboard (`web/index.html` + `server.py`'s API) or the desktop GUI (`gui.py`) |
| [`api_keys_and_secrets.md`](api_keys_and_secrets.md) | Config/settings loading, where every credential lives, adding a new one |
| [`deployment_vm.md`](deployment_vm.md) | The actual running VM - what's deployed, how to update it, ops commands |

`DEPLOY.md` (repo root, not in this folder) is the generic "how to
provision a fresh VM from scratch" guide - still the right place for that.
`deployment_vm.md` here is specific to the one VM that's actually running
right now.

## What this project is

A stock-market news watcher: it continuously scans financial news (RSS
feeds + user-added custom sources), runs each new article through an AI
(or keyword-based) relevance/sentiment analyzer, and pushes a phone
notification for anything judged high-impact. It also tracks a portfolio,
opens a "watch" on positive alerts to later tell you when to sell, and
exposes all of this through either a desktop GUI or a web dashboard.

## Two entry points, one backend

- **`gui.py`** - desktop app (customtkinter/Tkinter). Runs on your own
  machine, shows a native window, can use a local Ollama model.
- **`server.py`** - headless Flask app. Runs on the VM 24/7, serves
  `web/index.html` as a browser dashboard reachable over Tailscale, gated
  by HTTP Basic Auth. This is what's actually deployed.

Both are thin wrappers around the same **`main.py: StockAppBackend`** -
the scan loop, article processing, and alert logic are identical either
way; only how progress is displayed differs (native widgets vs. an
in-memory log/alert buffer polled over HTTP). See `architecture.md` for
the full module map.

## Quick orientation for a fresh session

1. If the task is "fix/change how X is analyzed" -> `ai_engines.md`
2. If it's "add a news source type" or "tune keyword scoring" -> `data_sources.md`
3. If it's "change the dashboard" or "add a GUI feature" -> `ui.md`
4. If it's "the VM is doing something weird" or "deploy an update" -> `deployment_vm.md`
5. If it's "add a new API key / provider" -> `api_keys_and_secrets.md`
6. If it's "I don't know where anything is" -> `architecture.md`, then drill into the specific file above
