"""Which exchange a ticker trades on, and when that exchange is open.

The app used to assume every stock it found was US-listed: one session
(09:30-16:00 ET), one benchmark (SPY), one time-exit clock. That is wrong
for a European stock in every one of those places - a Frankfurt listing has
already been trading for three hours when New York opens, and is shut before
New York's afternoon - so exits waited for a session the stock wasn't in and
fired on prints from one it had long left.

Everything here keys off the Yahoo suffix the ticker carries, because that is
the only market marker the rest of the app has: "BMW.DE" is XETRA, "SHEL.L"
is London, a bare "TSLA" is US. Sessions are the regular (continuous) ones
Yahoo itself reports for each venue; holidays are not modelled, matching the
rest of the app - a missed holiday only means an exit waits for the next open
day, never that one fires at a price nobody could trade at.

No currency conversion anywhere, and none is needed: every number the app
acts on is a percentage of the same ticker's own price (entry vs stop vs
target), and a benchmark only ever contributes its own percentage move. A
London price in pence and a Paris price in euros both work out unchanged.
"""

import datetime

import pytz

# A market's time exit fires this long before its close, so the position is
# closed in the last of the session's real liquidity rather than in the
# closing auction. For the US this is the 15:45 ET it has always been.
TIME_EXIT_BEFORE_CLOSE = datetime.timedelta(minutes=15)

# A position opened after this point in the session counts its horizon from
# the *next* session: an INTRADAY trade opened half an hour before the close
# should not be closed a quarter of an hour later. Expressed from the close
# so it means the same thing on a venue whose day is longer or starts
# earlier; for the US it is the 12:00 ET it has always been.
SESSION_CUTOFF_BEFORE_CLOSE = datetime.timedelta(hours=4)


class Market:
    """One exchange's regular session and the index it is judged against."""

    def __init__(self, name, region, tz, open_at, close_at, benchmark_setting):
        self.name = name
        # "US" or "Europe" - the granularity anything but the exit gate wants.
        # Naming all seventeen European venues in a log line or a prompt is
        # noise; which one a given stock is on is a per-ticker question, and
        # session_open answers it.
        self.region = region
        self.tz = pytz.timezone(tz)
        self.open_at = open_at
        self.close_at = close_at
        # Name of the config setting holding the benchmark symbol, not the
        # symbol itself: read live, so a benchmark changed in config (or, one
        # day, in the dashboard) applies without a restart.
        self.benchmark_setting = benchmark_setting

    @property
    def time_exit_at(self):
        """Local time of day the time exit fires."""
        close = datetime.datetime.combine(datetime.date.min, self.close_at)
        return (close - TIME_EXIT_BEFORE_CLOSE).time()

    @property
    def session_cutoff(self):
        """Local time of day after which a new position's horizon counts
        from the next session."""
        close = datetime.datetime.combine(datetime.date.min, self.close_at)
        return (close - SESSION_CUTOFF_BEFORE_CLOSE).time()

    def is_open(self, now=None):
        """Whether the regular session is running (holidays not modelled)."""
        local = (now or datetime.datetime.now(pytz.utc)).astimezone(self.tz)
        return local.weekday() < 5 and self.open_at <= local.time() < self.close_at


def _t(hour, minute=0):
    return datetime.time(hour, minute)


US = "US"
EUROPE = "Europe"

# Session times are the continuous-trading hours Yahoo reports for each
# venue, which is also the window the price feed has real volume in.
MARKETS = {
    US:      Market("US (NYSE/Nasdaq)", US, "America/New_York", _t(9, 30), _t(16), "PAPER_BENCHMARK"),
    "LSE":   Market("London", EUROPE, "Europe/London", _t(8), _t(16, 30), "PAPER_BENCHMARK_EU"),
    "XETRA": Market("Frankfurt (XETRA)", EUROPE, "Europe/Berlin", _t(9), _t(17, 30), "PAPER_BENCHMARK_EU"),
    "EURONEXT": Market("Euronext", EUROPE, "Europe/Paris", _t(9), _t(17, 30), "PAPER_BENCHMARK_EU"),
    # Brussels and Lisbon run a few minutes past the other Euronext venues;
    # a separate entry costs nothing and keeps the last ten minutes of their
    # session tradeable.
    "EURONEXT_BE": Market("Brussels", EUROPE, "Europe/Brussels", _t(9), _t(17, 40), "PAPER_BENCHMARK_EU"),
    "DUBLIN": Market("Dublin", EUROPE, "Europe/Dublin", _t(8), _t(16, 30), "PAPER_BENCHMARK_EU"),
    "SIX":   Market("Zurich (SIX)", EUROPE, "Europe/Zurich", _t(9), _t(17, 30), "PAPER_BENCHMARK_EU"),
    "MILAN": Market("Milan", EUROPE, "Europe/Rome", _t(9), _t(17, 30), "PAPER_BENCHMARK_EU"),
    "MADRID": Market("Madrid", EUROPE, "Europe/Madrid", _t(9), _t(17, 30), "PAPER_BENCHMARK_EU"),
    "VIENNA": Market("Vienna", EUROPE, "Europe/Vienna", _t(8, 55), _t(17, 35), "PAPER_BENCHMARK_EU"),
    "STOCKHOLM": Market("Stockholm", EUROPE, "Europe/Stockholm", _t(9), _t(17, 30), "PAPER_BENCHMARK_EU"),
    "COPENHAGEN": Market("Copenhagen", EUROPE, "Europe/Copenhagen", _t(9), _t(17), "PAPER_BENCHMARK_EU"),
    "OSLO":  Market("Oslo", EUROPE, "Europe/Oslo", _t(9), _t(16, 20), "PAPER_BENCHMARK_EU"),
    "HELSINKI": Market("Helsinki", EUROPE, "Europe/Helsinki", _t(10), _t(18, 30), "PAPER_BENCHMARK_EU"),
    "WARSAW": Market("Warsaw", EUROPE, "Europe/Warsaw", _t(9), _t(17, 5), "PAPER_BENCHMARK_EU"),
    "PRAGUE": Market("Prague", EUROPE, "Europe/Prague", _t(9), _t(16, 30), "PAPER_BENCHMARK_EU"),
    "BUDAPEST": Market("Budapest", EUROPE, "Europe/Budapest", _t(9), _t(17), "PAPER_BENCHMARK_EU"),
    "ATHENS": Market("Athens", EUROPE, "Europe/Athens", _t(10, 30), _t(17, 20), "PAPER_BENCHMARK_EU"),
}

# Yahoo's exchange suffix -> market. A ticker with no suffix is US; one whose
# suffix is not here (".TO", ".T", ".AX", ...) is listed somewhere this app
# doesn't model, which is not the same as being American - see market_key.
#
# The German regional venues (.F Frankfurt floor, .MU, .SG, .BE, .DU, .HM,
# .HA) quote from 08:00 to 22:00, but the liquidity is on XETRA and a fill
# outside its hours on one of them is the same thin print the session gate
# exists to ignore, so they are given XETRA's session.
SUFFIX_MARKETS = {
    "L": "LSE", "IL": "LSE",
    "DE": "XETRA", "F": "XETRA", "MU": "XETRA", "SG": "XETRA",
    "BE": "XETRA", "DU": "XETRA", "HM": "XETRA", "HA": "XETRA",
    "PA": "EURONEXT", "AS": "EURONEXT", "LS": "EURONEXT",
    "BR": "EURONEXT_BE",
    "IR": "DUBLIN",
    "SW": "SIX", "VX": "SIX",
    "MI": "MILAN",
    "MC": "MADRID",
    "VI": "VIENNA",
    "ST": "STOCKHOLM",
    "CO": "COPENHAGEN",
    "OL": "OSLO",
    "HE": "HELSINKI",
    "WA": "WARSAW",
    "PR": "PRAGUE",
    "BD": "BUDAPEST",
    "AT": "ATHENS",
}

EUROPEAN_MARKETS = frozenset(key for key in MARKETS if key != US)

# Every region, open or not, so a caller can name the closed ones too.
REGIONS = sorted({m.region for m in MARKETS.values()})

# How a headline, a model or a finance site writes an exchange in front of a
# symbol, mapped to the Yahoo suffix that prices it: "ETR: BMW" -> "BMW.DE",
# "LON:VOD" -> "VOD.L". Without this the prefix was simply stripped and the
# bare symbol looked up on the US market, which either found nothing or -
# worse - found an unrelated American company trading under those letters.
EXCHANGE_PREFIX_SUFFIX = {
    "NASDAQ": "", "NYSE": "", "AMEX": "", "NYSEAMERICAN": "", "OTC": "", "BATS": "",
    "LON": "L", "LSE": "L", "IOB": "L",
    "ETR": "DE", "XETR": "DE", "XETRA": "DE", "FWB": "DE", "GER": "DE", "FRA": "F",
    "EPA": "PA", "PAR": "PA", "PARIS": "PA",
    "AMS": "AS", "AEX": "AS",
    "EBR": "BR", "BRU": "BR",
    "ELI": "LS", "LIS": "LS",
    "ISE": "IR", "DUB": "IR",
    "SWX": "SW", "SIX": "SW", "VTX": "SW", "EBS": "SW",
    "BIT": "MI", "MIL": "MI", "MTA": "MI",
    "BME": "MC", "MCE": "MC", "MAD": "MC",
    "WBAG": "VI", "VIE": "VI",
    "STO": "ST", "OMX": "ST",
    "CPH": "CO",
    "OSL": "OL", "OB": "OL",
    "HEL": "HE",
    "WSE": "WA", "GPW": "WA",
    "PSE": "PR",
    "BUD": "BD",
    "ATH": "AT", "ATSE": "AT",
}


def suffix(ticker):
    """The exchange suffix of a Yahoo symbol, without the dot and upper-cased
    ("BMW.DE" -> "DE"), or "" for an unsuffixed (US) one.

    A single-letter suffix is ambiguous - "BRK.B" is a share class, "VOD.L"
    is London - so only suffixes this module actually knows count as one.
    """
    if not ticker or "." not in str(ticker):
        return ""
    tail = str(ticker).rsplit(".", 1)[1].upper()
    return tail if tail in SUFFIX_MARKETS else ""


def market_key(ticker):
    """Which market in MARKETS prices `ticker`, or None when its suffix names
    an exchange this module doesn't model (".TO" Toronto, ".AX" Sydney). An
    unsuffixed symbol is US, which is what Yahoo means by one."""
    tail = suffix(ticker)
    if tail:
        return SUFFIX_MARKETS[tail]
    # A lone trailing letter is read as a share class ("BRK.B"), so the few
    # single-letter suffixes that are really exchanges and aren't in the
    # table (".T" Tokyo) are taken for US symbols. They were before this
    # module existed too, and no European venue is spelled that way.
    if "." in str(ticker or "") and not _is_class_share(ticker):
        return None
    return US


def _is_class_share(ticker):
    """"BRK.B" - a dot followed by one letter that is not an exchange."""
    parts = str(ticker or "").rsplit(".", 1)
    return len(parts) == 2 and len(parts[1]) == 1 and parts[1].isalpha()


def market(ticker):
    """The Market record for `ticker`, or None when it isn't modelled."""
    key = market_key(ticker)
    return MARKETS.get(key) if key else None


def is_european(ticker):
    return market_key(ticker) in EUROPEAN_MARKETS


def session_open(ticker, now=None):
    """Whether `ticker`'s exchange is in its regular session: True, False, or
    None when the exchange isn't modelled and there is no telling.

    Callers gate on `is False` rather than on falsiness, so a ticker from an
    unmodelled venue is managed as before instead of being frozen out of
    every exit it has.
    """
    m = market(ticker)
    return m.is_open(now) if m else None


def open_markets(now=None):
    """Which regions are trading right now - ["Europe"], ["Europe", "US"],
    [] - for the "Market Status" line and the analysis prompt.

    Regions rather than the seventeen venue names, which would be a line of
    noise saying one thing. A region counts as trading when any of its
    exchanges is, which is what the prompt needs it for (whether this news
    moves a price now or gaps it at an open); anything that turns on one
    particular stock's exchange asks session_open about that ticker instead.
    """
    return sorted({m.region for m in MARKETS.values() if m.is_open(now)})


def any_open(now=None):
    return any(m.is_open(now) for m in MARKETS.values())


def benchmark_for(ticker):
    """The index/ETF this ticker's move is measured against: the US one for a
    US listing, the European one for a European listing. Read from config on
    every call so a changed setting needs no restart."""
    import config

    m = market(ticker) or MARKETS[US]
    return getattr(config, m.benchmark_setting, None) or config.PAPER_BENCHMARK


def timezone(ticker):
    """The tz a ticker's session (and so its time exit) is expressed in."""
    m = market(ticker) or MARKETS[US]
    return m.tz


def tz_label(ticker):
    """Short label for that timezone at this moment - "ET", "CEST", "BST" -
    so a time shown to the user says which clock it is on."""
    m = market(ticker) or MARKETS[US]
    if m.tz.zone == "America/New_York":
        # pytz spells these EST/EDT; the app has always said "ET".
        return "ET"
    return datetime.datetime.now(m.tz).strftime("%Z")


def describe(ticker):
    """"Frankfurt (XETRA)" / "US (NYSE/Nasdaq)" / "an unmodelled exchange"."""
    m = market(ticker)
    return m.name if m else "an exchange this app doesn't model"
