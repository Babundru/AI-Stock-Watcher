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

# --- NOTIFICATION SETTINGS ---
# Ntfy.sh Topic Name
# IMPORTANT: This is public if you use the free server! Choose a long, random name to make it "private" effectively.
NTFY_TOPIC = "stocks-watcher-change-me" # CHANGE THIS TO SOMETHING UNIQUE

# --- DYNAMIC SETTINGS LOADING ---
import json
SETTINGS_FILE = "data/settings.json"

if os.path.exists(SETTINGS_FILE):
    try:
        with open(SETTINGS_FILE, 'r') as f:
            user_settings = json.load(f)
            
        # Override defaults if keys exist
        if "NTFY_TOPIC" in user_settings:
            NTFY_TOPIC = user_settings["NTFY_TOPIC"]

        if "LOCAL_MODEL_NAME" in user_settings and user_settings["LOCAL_MODEL_NAME"]:
            LOCAL_MODEL_NAME = user_settings["LOCAL_MODEL_NAME"]

        if "OLLAMA_NUM_THREADS" in user_settings and user_settings["OLLAMA_NUM_THREADS"]:
            # Stored as a string by the settings dialog.
            OLLAMA_NUM_THREADS = int(user_settings["OLLAMA_NUM_THREADS"])

        if "USE_LOCAL_LLM" in user_settings:
            USE_LOCAL_LLM = bool(user_settings["USE_LOCAL_LLM"])

        print(f"Loaded custom settings: Topic={NTFY_TOPIC}, Model={LOCAL_MODEL_NAME}")
        
    except Exception as e:
        print(f"Error loading data/settings.json: {e}")
