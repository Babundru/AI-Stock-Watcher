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
                
        print(f"Loaded custom settings: Topic={NTFY_TOPIC}")
        
    except Exception as e:
        print(f"Error loading data/settings.json: {e}")
