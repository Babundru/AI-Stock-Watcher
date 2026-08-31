"""Wall-clock time for human-facing display (log timestamps, alert times).

Pinned to Romania regardless of the machine's own OS timezone - the GCP VM
in DEPLOY.md runs UTC by default, which otherwise makes every timestamp in
the dashboard read hours off. Don't use this for anything that compares
against article publish times (see news_collector.py) - those stay in UTC,
which is what feeds report in and what LOOKBACK_MINUTES math expects.
"""
import datetime
import pytz

TIMEZONE = pytz.timezone("Europe/Bucharest")


def now_local():
    return datetime.datetime.now(TIMEZONE)
