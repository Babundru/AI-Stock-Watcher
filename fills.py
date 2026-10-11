"""Fill prices for paper trades: what a real order would have paid or got.

The watch checker prices everything off the last trade on the 1-minute
chart. That is the right number for deciding *whether* an exit fires - it is
what a broker's stop order watches too - but it is not a price anyone trades
at. A buy crosses to the ask, a sell to the bid, and on the small, freshly
news-hit stocks this app tends to find the gap between them is anything
from a tenth of a percent to several. Recording last trades as fills left
all of that out of the ledger, charged instead as one flat PAPER_COST_PCT
that fits a mega-cap and nothing else.

So every paper trade now opens at the ask (a long) or the bid (a short) and
closes at the other side, and stores the spread it paid. Yahoo's quote is
used where it is believable; where it isn't - missing, or one of the
nonsense quotes it serves for US listings (a 3% AAPL spread, a 55% one on a
$6 stock) - the spread is estimated from the company's size.

A fill also has to be *possible*: entry_problem refuses a price older than
config.ENTRY_MAX_PRICE_AGE_MIN (plus the exchange's reporting delay), one
from before the article was published, and a spread wider than
config.ENTRY_MAX_SPREAD_PCT.

Everything here is pure: prices and quotes come in (price_lookup), a price
and a verdict go out. Spreads are the full bid-ask gap as a fraction of the
mid price; a single fill pays half of it.
"""

import datetime

import config
import strategy

# Typical quoted bid-ask spread by market cap in US dollars, largest first -
# the fallback when Yahoo's own quote can't be believed. Round figures for
# a regular session: a mega-cap trades a cent or two wide, a $30M company
# a few percent.
SPREAD_BY_MARKET_CAP = (
    (10e9, 0.001),
    (2e9, 0.0025),
    (300e6, 0.006),
    (50e6, 0.015),
    (0, 0.03),
)
# No market cap to go on: assume a small company rather than a large one.
UNKNOWN_SPREAD = 0.01
# Pre-market, after hours, or an exchange that is shut: fewer market makers,
# wider quotes. Applied to an estimate only - a real quote is what it is.
OUT_OF_HOURS_SPREAD_MULT = 2.0

# A Yahoo quote is believed only if it is at most MAX_SANE_QUOTE_SPREAD
# wide, no more than MAX_QUOTE_VS_ESTIMATE times what a company that size
# typically trades at, and the last trade sits inside it (give or take
# QUOTE_LAST_TOLERANCE). The ones that fail are almost always a single
# odd-lot order on one venue, not the market: a 3% quote on a $5 trillion
# company says nothing about what buying it costs.
MAX_SANE_QUOTE_SPREAD = 0.10
MAX_QUOTE_VS_ESTIMATE = 5.0
QUOTE_LAST_TOLERANCE = 0.01

# Rough dollars per unit of currency - only to put a market cap into one of
# the size bands above, where 20% either way changes nothing. Yahoo gives
# market caps in the major unit (pounds for a London listing priced in
# pence), hence the minor units mapping to their major one.
_USD_PER = {
    'USD': 1.0, 'EUR': 1.15, 'GBP': 1.33, 'CHF': 1.25, 'SEK': 0.105, 'NOK': 0.098,
    'DKK': 0.155, 'PLN': 0.27, 'CZK': 0.046, 'HUF': 0.0029, 'ISK': 0.0078,
    'JPY': 0.0068, 'KRW': 0.00072, 'HKD': 0.128, 'CNY': 0.14, 'TWD': 0.031,
    'SGD': 0.77, 'INR': 0.012, 'AUD': 0.66, 'NZD': 0.6, 'CAD': 0.73, 'ILS': 0.27,
    'ZAR': 0.056, 'BRL': 0.18, 'MXN': 0.054, 'TRY': 0.025,
}
_MAJOR_UNIT = {'GBp': 'GBP', 'GBX': 'GBP', 'ZAc': 'ZAR', 'ILA': 'ILS'}


def estimate_spread(market_cap, currency, in_session=True):
    """Typical spread for a company this size, from SPREAD_BY_MARKET_CAP."""
    rate = _USD_PER.get(_MAJOR_UNIT.get(currency, currency))
    if market_cap and rate:
        usd = market_cap * rate
        spread = next(s for floor, s in SPREAD_BY_MARKET_CAP if usd >= floor)
    else:
        spread = UNKNOWN_SPREAD
    if in_session is False:
        spread *= OUT_OF_HOURS_SPREAD_MULT
    return spread


def quoted_spread(bid, ask, last, estimate=None):
    """The quote's spread if it can be believed (see MAX_SANE_QUOTE_SPREAD),
    else None. `estimate` is estimate_spread's figure for the company."""
    if not bid or not ask or not last or bid <= 0 or ask < bid:
        return None
    spread = (ask - bid) / ((ask + bid) / 2)
    if spread > MAX_SANE_QUOTE_SPREAD:
        return None
    if estimate is not None and spread > estimate * MAX_QUOTE_VS_ESTIMATE:
        return None
    if not bid * (1 - QUOTE_LAST_TOLERANCE) <= last <= ask * (1 + QUOTE_LAST_TOLERANCE):
        return None
    return spread


def _fill(direction, buying, last, quote, in_session, fallback_spread=None, at_level=False):
    quote = quote or {}
    bid, ask = quote.get('bid'), quote.get('ask')
    estimate = estimate_spread(quote.get('market_cap'), quote.get('currency'), in_session)
    if not quote.get('market_cap') and fallback_spread is not None:
        estimate = fallback_spread
    # A level seen earlier needn't sit inside this moment's quote, so the
    # quote is then checked against its own middle instead.
    check = (bid + ask) / 2 if at_level and bid and ask else last
    spread = quoted_spread(bid, ask, check, estimate)
    if spread is not None and not at_level:
        price, source = (ask if buying else bid), 'quote'
    else:
        # `at_level`: the order went through at a price seen earlier (a stop
        # triggered between two checks), not at this moment's bid or ask -
        # so only the quote's width is used, around that price.
        source = 'quote' if spread is not None else 'estimate'
        if spread is None:
            spread = estimate
        price = last * (1 + spread / 2) if buying else last * (1 - spread / 2)
    return {
        'price': round(price, 6),
        'last': last,
        'spread_pct': round(spread, 6),
        'spread_source': source,
    }


def entry_fill(direction, last, quote, in_session):
    """Open a position: a long buys at the ask, a short sells at the bid.
    Returns {'price', 'last', 'spread_pct', 'spread_source'}."""
    return _fill(direction, direction != strategy.SHORT, last, quote, in_session)


def exit_fill(direction, last, quote, in_session, fallback_spread=None, at_level=False):
    """Close a position: a long sells at the bid, a short buys back at the
    ask. Without a quote or a market cap to go on, `fallback_spread` (the
    spread paid on the way in) is assumed rather than a generic guess.

    `at_level`: `last` is the price an order triggered at earlier - a stop
    hit between two watch checks - so the fill is half the spread beyond
    it, not the bid or ask of the moment the check runs."""
    return _fill(direction, direction == strategy.SHORT, last, quote, in_session,
                 fallback_spread, at_level)


def _as_datetime(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.datetime.fromtimestamp(value, datetime.timezone.utc)
    if isinstance(value, str):
        try:
            value = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)
    return None


def entry_problem(fill, price_time, delay_min=0, published_at=None, now=None):
    """Why a position may not open at this fill: (rule, reason), or None.

    'stale' - no trade recent enough, or none since the article came out,
              to be a price the story's trade could have got.
    'spread' - the bid-ask spread is wider than ENTRY_MAX_SPREAD_PCT.

    `price_time` is when the last trade printed (epoch seconds or datetime);
    `delay_min` is how late Yahoo reports that exchange's trades.
    """
    now = _as_datetime(now) or datetime.datetime.now(datetime.timezone.utc)
    printed = _as_datetime(price_time)
    if printed is None:
        return 'stale', "couldn't tell how old the last price is"
    age = (now - printed).total_seconds() / 60
    allowed = config.ENTRY_MAX_PRICE_AGE_MIN + (delay_min or 0)
    if age > allowed:
        return 'stale', (f"last trade is {age:.0f} min old (allowed {allowed:.0f}) - "
                         f"not a price anyone could buy at now")
    published = _as_datetime(published_at)
    if published is not None and printed < published:
        return 'stale', ("no trade since the story was published - the price "
                         "hasn't seen the news yet")
    if fill['spread_pct'] > config.ENTRY_MAX_SPREAD_PCT:
        how = 'quoted' if fill['spread_source'] == 'quote' else 'estimated'
        return 'spread', (f"bid-ask spread {fill['spread_pct'] * 100:.2f}% ({how}) is wider "
                          f"than {config.ENTRY_MAX_SPREAD_PCT * 100:.2f}%")
    return None


def exit_haircut(trade):
    """What closing an open position would cost on top of its last-trade
    value: half the spread it opened across. Zero for trades recorded
    before fills were modelled, which also opened at a last trade."""
    return (trade.get('spread_pct') or 0) / 2


def describe(fill):
    """One log line: "fill 101.23 vs last 101.00 (spread 0.45%, quoted)"."""
    how = 'quoted' if fill['spread_source'] == 'quote' else 'estimated'
    return (f"fill {fill['price']:.4g} vs last {fill['last']:.4g} "
            f"(spread {fill['spread_pct'] * 100:.2f}%, {how})")
