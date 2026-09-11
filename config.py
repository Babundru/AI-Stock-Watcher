import os

# How far back to consider an article "fresh", in minutes.
# The RSS feeds we poll only publish every 10-30 minutes, so a window
# narrower than that filters out everything and the app finds no news.
# Duplicates are not a concern: processed URLs are cached (see main.py),
# so a wider window costs nothing but catches slower-updating feeds.
LOOKBACK_MINUTES = 30

# Check interval in seconds
# With 1-minute intervals: 24 hours * 60 requests/hour = 1440 requests/day
# Fast scanning to catch breaking news immediately
# Note: App tracks last 120 processed URLs to avoid duplicates
CHECK_INTERVAL = 60  # 1 minute

# How often to check open watches (stocks with a pending sell signal) against
# their current price, in seconds. Coarser than CHECK_INTERVAL on purpose:
# a price target/horizon doesn't need per-minute polling, and each check is
# one more batched yfinance call per open watch.
WATCH_CHECK_INTERVAL = 300  # 5 minutes

# --- WATCHLIST SETTINGS ---
# List of companies to track specifically (Ignored if GLOBAL_SCAN = True)
TARGET_COMPANIES = [] 

# --- DISCOVERY & GLOBAL MODE ---
# If True, the app scans "Top Business Headlines" instead of just specific tickers.
# This allows finding opportunities for ANY company.
GLOBAL_SCAN = True

# --- LOCAL LLM (OLLAMA) SETTINGS ---
# True  -> articles are analysed by a local Ollama model (the LLM variant)
# False -> falls back to the offline weighted-keyword analyzer, which needs
#          no model and no Ollama install.
# Ignored when USE_CLOUD_AI is True (cloud AI takes priority).
USE_LOCAL_LLM = True

# Ollama's generate endpoint. Change the host if Ollama runs elsewhere.
OLLAMA_URL = "http://localhost:11434/api/generate"

# Model to run. Must already be pulled: `ollama pull <name>`.
# This is the model the local Ollama server currently serves. (The deleted
# settings.json held a 9-character name, almost certainly "llama3:8b" -
# a llama3 manifest is still in ~/.ollama - but the running server no
# longer serves it. Change this here or in SYSTEM OPTIONS.)
LOCAL_MODEL_NAME = "gemma3:12b"

# Threads the model may use. Higher is faster but competes with the rest of
# the machine; 1 keeps the app unobtrusive at the cost of slow analysis.
OLLAMA_NUM_THREADS = 1

# --- CLOUD AI (API KEY) SETTINGS ---
# True -> articles are analysed via a hosted AI API instead of a local
# model. Takes priority over USE_LOCAL_LLM when both are enabled - there is
# no local model to keep running once a cloud key is configured.
USE_CLOUD_AI = False

# Which backend to call - a key into the PROVIDERS registry in
# cloud_providers.py. Ships with "anthropic", "openai" (also covers any
# OpenAI-compatible third-party API via CLOUD_AI_BASE_URL below), and two
# one-key-many-models routers, "openrouter" and "routera"; add more by
# registering a provider class there.
CLOUD_AI_PROVIDER = "anthropic"

# Model to call. Configurable because this analyzer runs on every discovered
# article (potentially dozens per CHECK_INTERVAL) - a cheaper/faster model
# can be swapped in here for that volume without changing any code.
#
# May also be a comma-separated list, most preferred first, e.g.
# "openai/gpt-5.6-luna, xai/grok-4.6": when a model fails, the same article
# goes to the next one (see cloud_analyzer.py). All of them are called
# through CLOUD_AI_PROVIDER with the same key. Read it via cloud_models().
CLOUD_AI_MODEL = "claude-opus-5"

# API key for the selected provider. Never put a real key here - this file
# is committed. Set it in data/settings.json (gitignored) via Settings.
CLOUD_AI_API_KEY = ""

# Optional: override the provider's default API endpoint. Leave empty for
# Anthropic or OpenAI itself; set it to route CLOUD_AI_PROVIDER = "openai"
# at an OpenAI-compatible third-party host instead (Groq, Together,
# DeepSeek, OpenRouter, a local vLLM/llama.cpp server, ...).
CLOUD_AI_BASE_URL = ""

# --- PAPER TRADING (TRACK RECORD) ---
# Records every alert's round trip - the entry price the app saw when it
# opened the watch, and the exit price it saw when the sell/cover signal
# fired - to data/paper_trades.json, so the app's profitability can be
# judged from its own numbers after it has been running a while. See
# paper_trader.py, and `py paper_report.py` for the summary.
#
# This changes no behaviour: it only writes down the trades the app already
# makes. Leave it on - the ledger is the only record of what the alerts were
# worth, and none of it can be reconstructed after the fact.
PAPER_TRADING = True

# Round-trip cost of a trade, as a fraction: CFD spread on both sides, plus
# commission, plus overnight financing on anything held for days. 0.002 =
# 0.2%, a plausible retail CFD figure on a liquid US stock. It is subtracted
# from every trade, so it is the whole difference between "the price moved my
# way" and "I made money" - worth setting to your broker's real numbers
# before reading the results.
PAPER_COST_PCT = 0.002

# Ticker priced alongside each trade to record what the market did over the
# same window. Without it there is no telling an analyser that works from a
# month in which everything went up.
PAPER_BENCHMARK = "SPY"

# Notional sizing, used only to draw an equity curve and a drawdown figure in
# the report - the app itself never sizes a position. Each trade puts
# PAPER_POSITION_PCT of the current capital to work.
PAPER_START_CAPITAL = 10000.0
PAPER_POSITION_PCT = 0.10

# --- STRATEGY: RISK PER TRADE (stop-loss) ---
# Every position has a stop-loss; there is no "off" any more. The old rules
# closed winners at a fixed +5/10% target but gave losers no floor (or, with
# a stop set, postponed their time exit until they recovered) - so the
# closed record hovered around zero while the open book filled with losers.
# See strategy.py for the full set of exit rules.
#
# The stop's distance from entry is sized to the stock's own volatility:
# STOP_ATR_MULT x its 14-day average true range (ATR - roughly how far it
# moves on a normal day), clamped between STOP_MIN_PCT and STOP_LOSS_PCT. A
# calm stock gets a tight stop and a jumpy one a wide one, instead of both
# getting the same percentage and the jumpy one being stopped out by noise.
#
# STOP_LOSS_PCT is therefore the most any single trade may risk, and the
# stop used when no volatility data could be fetched. 0 - the old "off"
# value, possibly still in data/settings.json - means DEFAULT_STOP_LOSS_PCT.
STOP_LOSS_PCT = 0.08
DEFAULT_STOP_LOSS_PCT = 0.08
STOP_ATR_MULT = 1.5
STOP_MIN_PCT = 0.02

# Once a position has gained this fraction of its target, its stop moves up
# to break-even (entry plus trading costs): a trade that was clearly working
# can no longer turn into a loss.
BREAKEVEN_AT = 0.5

# Let winners run. On reaching its target a position is not closed; its stop
# is tightened to trail TRAIL_AFTER_TARGET_MULT x the initial stop distance
# behind the best price seen, and it closes when that is hit (or at its time
# exit). False closes at the target, as the app used to.
LET_WINNERS_RUN = True
TRAIL_AFTER_TARGET_MULT = 0.5

# A trade is skipped when the move still expected is not at least this many
# times the stop distance - i.e. when the news is not expected to move the
# stock clearly more than it moves on an ordinary day anyway.
MIN_REWARD_RISK = 1.2

# --- STRATEGY: ENTRIES ---
# The AI rates its own confidence (0-100) in direction and size. Alerts
# below this are dropped before they notify or trade.
MIN_CONFIDENCE = 60

# Second AI pass for would-be trades only (a handful a day, not every
# article): the model sees the article again together with the live price
# context - how far the stock has already moved since the previous close and
# since the article was published, and its normal daily range - and decides
# whether a position opened *now* still has room to run. Ignored by the
# keyword engine, which trades on the hard rules alone.
AI_TRADE_CONFIRM = True

# Hard "already priced in" rule, applied whatever the AI says: skip a trade
# when the stock has already moved this fraction of the expected move in the
# trade's direction. By the time a story reaches an RSS feed, the fast money
# has often already traded it; buying at that point is buying the top.
PRICED_IN_FRACTION = 0.5

# Most positions open at once. Also a memory guard for the 1GB VM: every
# open position is priced on every watch check (see price_lookup.MAX_BATCH).
MAX_OPEN_POSITIONS = 20

# --- SHORT SELLING ---
# Whether negative news on a stock you don't own opens a SHORT position
# (sell a CFD short, buy it back on the cover signal) and records it in the
# paper-trading ledger. Off suits anyone who doesn't use CFDs or shorting.
ALLOW_SHORTS = True

# Whether short setups - and the cover signals of short positions - are
# pushed to your phone. Independent of ALLOW_SHORTS: on while shorts are not
# traded still tells you about the setup (marked as not tracked); off keeps
# them out of your notifications but still paper-trades them if allowed.
# Negative news about a stock you own is a risk warning, not a short setup,
# and is always notified.
NOTIFY_SHORTS = True

# Shorts carry costs longs don't (borrow/financing) and fight the market's
# long-run upward drift, so they need a stronger signal than longs do.
SHORT_MIN_IMPACT = "CRITICAL"

# --- NOTIFICATION SETTINGS ---
# Ntfy.sh topic name.
#
# On the free ntfy server a topic is a PUBLIC channel: anyone who knows or
# guesses the name can read every alert you send. The name is the only thing
# keeping it private, so it must be long and random.
#
# Set your real topic in data/settings.json (via Settings in the app). That
# file is gitignored; this file is not, so never put your real topic here -
# it would be published with the code.
#
# Empty on purpose: phone notifications stay off until you choose a topic.
# A shipped default would be a shared public channel - every user who never
# changed it would be publishing their alerts to the same place, and reading
# everyone else's.
NTFY_TOPIC = ""

# --- ALERT SENSITIVITY ---
# Impact ratings the analysers produce, weakest first. The order is what
# makes MIN_IMPACT a threshold rather than a list.
IMPACT_LEVELS = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]

# The weakest impact rating that still raises an alert. Anything below it is
# analysed and logged, but neither notified nor traded on paper.
#
#   CRITICAL  15%+ expected move, game changer. A handful a month.
#   HIGH      5-15%, a significant event. The default.
#   MEDIUM    2-5%, standard news. Many more alerts, much more noise.
#   LOW       everything the analyser did not reject outright.
#
# Lowering this does not make the app smarter - it makes it louder. The
# analyser's confidence is weakest exactly where the ratings are lowest, so
# MEDIUM and below carry a far higher share of misreads. Worth trying only
# once the paper record at HIGH has enough trades to compare against.
#
# Set from the sensitivity slider in the app; both surfaces persist it to
# data/settings.json and apply it live, with no restart.
MIN_IMPACT = "HIGH"


def impact_rank(level):
    """Position of an impact rating in IMPACT_LEVELS, or -1 if unknown.

    An unrecognised rating (a model inventing its own wording, say) ranks
    below everything, so it is filtered out rather than treated as important.
    """
    try:
        return IMPACT_LEVELS.index((level or "").upper())
    except ValueError:
        return -1


def impact_passes(level):
    """Whether `level` is strong enough to alert on, per MIN_IMPACT.

    Read through this rather than comparing against a captured constant:
    the slider rewrites MIN_IMPACT on this module, so anything holding an
    old copy would keep filtering at the previous setting until a restart.
    """
    return impact_rank(level) >= impact_rank(MIN_IMPACT)


# Master switch for phone notifications.
#
# Separate from NTFY_TOPIC on purpose: this mutes alerts without making you
# clear (and later retype) the topic, so silencing the app overnight or while
# testing does not risk losing it. The app still scans, analyses, opens and
# closes watches, and records paper trades while muted - only the push to
# ntfy.sh is suppressed. Alerts still appear in the Alerts tab and the log.
#
# Toggled live from the sidebar switch (desktop) or the header toggle (web
# dashboard); both persist the choice to data/settings.json and take effect
# immediately, with no restart.
NOTIFICATIONS_ENABLED = True

# Whether to mark alerts about stocks you own with an "[OWNED]" tag in the
# notification itself.
#
# Off by default: the tag would tell anyone reading the public topic which
# stocks you actually hold. The app still shows an "Owned" badge on the
# Alerts tab, which stays on this machine.
NOTIFY_OWNERSHIP = False

# --- DASHBOARD LOGIN (server.py only) ---
# HTTP Basic Auth for the web dashboard. gui.py ignores these - they only
# gate server.py, which is meant to run 24/7 on a remote machine reachable
# over Tailscale (see DEPLOY.md). Tailscale already keeps the port off the
# public internet; this is a second layer so a compromised Tailscale peer -
# or anyone else on the tailnet - still needs a password.
#
# Empty on purpose: set both in data/settings.json (via Settings, or by
# hand) before running server.py anywhere reachable by more than just you.
# Leaving DASHBOARD_PASSWORD empty disables the login prompt entirely.
DASHBOARD_USERNAME = ""
DASHBOARD_PASSWORD = ""

# --- DYNAMIC SETTINGS LOADING ---
import json
SETTINGS_FILE = "data/settings.json"

# Which keys data/settings.json may override, and how to coerce each one.
# Anything not listed here is ignored on load and refused by save_settings,
# so a typo in the file (or a stray key posted to the dashboard) can never
# shadow an unrelated module global.
_STR = lambda v: str(v)
_BOOL = lambda v: bool(v)
_INT = lambda v: int(v)
_FLOAT = lambda v: float(v)
_IMPACT = lambda v: str(v).upper() if str(v).upper() in IMPACT_LEVELS else MIN_IMPACT
_SHORT_IMPACT = lambda v: str(v).upper() if str(v).upper() in IMPACT_LEVELS else "CRITICAL"


def _split_models(value):
    """Split a model list - "a, b", one per line, or a JSON array - into
    names, trimmed, with blanks and repeats dropped."""
    parts = value if isinstance(value, (list, tuple)) else str(value).replace("\n", ",").split(",")
    models = []
    for part in parts:
        name = str(part).strip()
        if name and name not in models:
            models.append(name)
    return models


def _MODELS(value):
    """A model priority list, stored as one "a, b" string."""
    models = _split_models(value)
    if not models:
        raise ValueError("at least one model name is needed")
    return ", ".join(models)


USER_SETTINGS = {
    "NTFY_TOPIC": _STR,
    "NOTIFICATIONS_ENABLED": _BOOL,
    "NOTIFY_OWNERSHIP": _BOOL,
    "MIN_IMPACT": _IMPACT,
    "STOP_LOSS_PCT": _FLOAT,
    "STOP_ATR_MULT": _FLOAT,
    "LET_WINNERS_RUN": _BOOL,
    "MIN_CONFIDENCE": _INT,
    "AI_TRADE_CONFIRM": _BOOL,
    "MAX_OPEN_POSITIONS": _INT,
    "ALLOW_SHORTS": _BOOL,
    "NOTIFY_SHORTS": _BOOL,
    "SHORT_MIN_IMPACT": _SHORT_IMPACT,
    "USE_LOCAL_LLM": _BOOL,
    "LOCAL_MODEL_NAME": _STR,
    "OLLAMA_NUM_THREADS": _INT,
    "OLLAMA_URL": _STR,
    "USE_CLOUD_AI": _BOOL,
    "CLOUD_AI_PROVIDER": _STR,
    "CLOUD_AI_MODEL": _MODELS,
    "CLOUD_AI_API_KEY": _STR,
    "CLOUD_AI_BASE_URL": _STR,
    "DASHBOARD_USERNAME": _STR,
    "DASHBOARD_PASSWORD": _STR,
    "PAPER_TRADING": _BOOL,
    "PAPER_COST_PCT": _FLOAT,
}

# Settings whose value must not be emptied by a blank entry: a blank model
# name or thread count in the dialog means "leave the default", not "none".
_KEEP_DEFAULT_IF_BLANK = {"LOCAL_MODEL_NAME", "OLLAMA_NUM_THREADS", "OLLAMA_URL",
                          "CLOUD_AI_PROVIDER", "CLOUD_AI_MODEL"}

# The shipped defaults, captured before the file is applied, so a key that
# is later removed from the file (or blanked) falls back to them.
_DEFAULTS = {key: globals()[key] for key in USER_SETTINGS}


def _read_settings_file():
    if not os.path.exists(SETTINGS_FILE):
        return {}
    try:
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else {}
    except (ValueError, OSError) as e:
        print(f"Error loading {SETTINGS_FILE}: {e}")
        return {}


def reload_from_disk(verbose=True):
    """(Re)apply data/settings.json on top of the shipped defaults.

    Runs once at import, and again when the dashboard's "Reload config"
    asks for files edited by hand to be picked up. Every key starts from its
    default, so deleting a line from the file reverts that setting too.
    """
    user_settings = _read_settings_file()
    for key, coerce in USER_SETTINGS.items():
        value = _DEFAULTS[key]
        if key in user_settings:
            raw = user_settings[key]
            blank = raw is None or (isinstance(raw, str) and not raw.strip())
            if not (blank and key in _KEEP_DEFAULT_IF_BLANK):
                try:
                    value = coerce(raw)
                except (TypeError, ValueError):
                    print(f"Ignoring invalid {key} in {SETTINGS_FILE}: {raw!r}")
        globals()[key] = value
    if verbose and user_settings:
        # Never log the API key itself.
        print(f"Loaded custom settings: Topic={NTFY_TOPIC}, Model={LOCAL_MODEL_NAME}, "
              f"CloudAI={'on (' + CLOUD_AI_PROVIDER + '/' + CLOUD_AI_MODEL + ')' if USE_CLOUD_AI else 'off'}")


reload_from_disk()


def save_settings(values):
    """Persist several settings to data/settings.json and apply them live.

    Merges into the existing file rather than replacing it: a fresh dict
    would drop every setting the caller did not pass, silently reverting
    things like the ntfy topic or the API key to their defaults.

    Updating the module globals too is what makes a live change work - code
    that reads config.<KEY> after this call sees the new value without a
    restart. Returns the applied {key: value}.
    """
    applied = {}
    for key, raw in values.items():
        if key not in USER_SETTINGS:
            raise KeyError(f"unknown setting {key}")
        applied[key] = USER_SETTINGS[key](raw)

    existing = _read_settings_file()
    existing.update(applied)
    os.makedirs(os.path.dirname(SETTINGS_FILE) or '.', exist_ok=True)
    with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(existing, f, indent=4)

    globals().update(applied)
    return applied


def save_setting(key, value):
    """Persist one setting (see save_settings)."""
    return save_settings({key: value})[key]


def cloud_models():
    """CLOUD_AI_MODEL as a list, most preferred first. Read live, like the
    other settings, so a saved change applies when the engine is rebuilt."""
    return _split_models(CLOUD_AI_MODEL)
