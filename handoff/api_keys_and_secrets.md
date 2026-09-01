# API Keys & Secrets

**No credential values appear anywhere in this file or this repo.** This
describes the mechanism only.

## The pattern

`config.py` declares every setting with a safe, committed default (never a
real secret). At the bottom, it reads `data/settings.json` (gitignored -
see `.gitignore`) and overrides anything present there:

```python
DASHBOARD_USERNAME = ""       # config.py default
...
if "DASHBOARD_USERNAME" in user_settings:
    DASHBOARD_USERNAME = user_settings["DASHBOARD_USERNAME"]
```

Every module reads settings via `from config import X` - there's no
separate secrets-loading path to keep in sync, `config.py` is the single
source of truth at runtime, `data/settings.json` is just the private
overlay on top of it.

## Every settings.json-overridable field

| Key | What it gates | Notes |
|---|---|---|
| `NTFY_TOPIC` | Phone notifications | Public channel on free ntfy - the name itself is the only privacy, must be long/random |
| `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` | Web dashboard login (`server.py` only, `gui.py` ignores these) | Empty password = no login prompt at all - `server.py` prints a startup warning if unset |
| `USE_CLOUD_AI`, `CLOUD_AI_PROVIDER`, `CLOUD_AI_MODEL`, `CLOUD_AI_API_KEY`, `CLOUD_AI_BASE_URL` | Cloud AI engine | See `ai_engines.md` for current provider/model choice |
| `USE_LOCAL_LLM`, `LOCAL_MODEL_NAME`, `OLLAMA_NUM_THREADS` | Local Ollama engine | Desktop-only in practice, see `ai_engines.md` |
| `NOTIFY_OWNERSHIP` | Whether a NEGATIVE alert on an owned stock gets `[OWNED]` in the *push notification* itself | Off by default - the ntfy topic is public, this would leak holdings to anyone who finds the topic |

Settings **not** overridable via `data/settings.json` (edit `config.py`
directly and redeploy/restart to change): `LOOKBACK_MINUTES`,
`CHECK_INTERVAL`, `WATCH_CHECK_INTERVAL`, `TARGET_COMPANIES`,
`GLOBAL_SCAN`, `OLLAMA_URL`.

## Where the real values actually live

- **Local dev machine**: `data/settings.json` in the repo root, gitignored.
- **The VM**: its own separate `data/settings.json`, created by hand over
  SSH (not synced from local, not in git) - see `deployment_vm.md`.
- Nowhere else. Not in `config.py`, not in any handoff doc, not in shell
  history you'd commit, not in a `.env` file (though `.env`/`.env.*`/`*.key`
  are also gitignored as a blanket safety net even though this project
  doesn't currently use them).

## Adding a new secret/setting

1. Add it to `config.py` with a safe default (empty string / `False` /
   whatever "off" means) and a comment explaining what it's for and why
   the default is safe to commit.
2. Add an `if "X" in user_settings: X = user_settings["X"]` line in the
   loading block at the bottom of `config.py`.
3. If it should be editable from the desktop GUI: extend `gui.py`'s
   `open_settings()` dialog (reads current value, adds a field, includes
   it in the merge-and-save dict) - see `ui.md`.
4. If it should be editable from the web dashboard: there's no Settings
   UI there yet (see `ui.md`) - document in `deployment_vm.md` that it
   needs hand-editing on the VM, or build the equivalent of step 3 as a
   small `/api/settings` endpoint + form.
5. Never put the real value in anything that gets committed - double
   check `git status`/`git diff` before pushing if you were testing with
   a real credential locally.

## Dashboard auth mechanics (`server.py`)

HTTP Basic Auth, enforced by a Flask `@app.before_request` hook
(`require_login`) that runs before every route, including the static HTML
page itself. Credentials are compared with `hmac.compare_digest` (constant
time, avoids a timing side-channel). If `DASHBOARD_PASSWORD` is empty, the
hook is a no-op and the dashboard is fully open - that's intentional for
local testing, but `server.py`'s `__main__` block prints a loud warning if
it's about to bind on anything but localhost with no password set.

**This alone is not safe on the open internet** - Basic Auth only
base64-encodes credentials, it doesn't encrypt them. What actually makes
this safe in the current deployment is that the port is *never* exposed
publicly at all - Tailscale provides the encryption and access control;
Basic Auth is a second layer on top (so a compromised Tailscale peer, or
anyone else already on the tailnet, still needs the password too). See
`deployment_vm.md`.

## ntfy topic privacy

Worth repeating because it's easy to forget: ntfy.sh's free tier makes a
topic a **public, unauthenticated channel** - anyone who knows or guesses
the topic name can read every notification sent to it. There's no access
control beyond the name being hard to guess. This is why `NTFY_TOPIC`'s
default in `config.py` is empty (a shipped default would mean every user
who never changed it broadcasts to the same public channel), why it's
never committed, and why `NOTIFY_OWNERSHIP` defaults off.
