"""Shared live-price lookup, used by the watch checker (main.py) to see
whether a stock's alerted-on move has played out.

Batches all tickers into a single yfinance call - cheap when checking many
open watches at once instead of one request per ticker.

Prices come from the 1-minute chart with prepost=True, *not* from
fast_info/regularMarketPrice. Those report the last **regular session**
close, so outside 09:30-16:00 ET (which is most of the time this app runs)
they hand back a stale figure that ignores every pre-/post-market trade -
exactly the hours when news-driven stocks move most. That produced entry and
sell-signal prices visibly different from the quote the user sees on any
finance site. fast_info survives only as a fallback for tickers the
intraday chart has no data for (holidays, thin listings).

yfinance is imported inside fetch_prices rather than at module scope on
purpose: it drags in pandas + numpy, which cost well over 100MB of resident
memory, and both main.py and server.py import this module at startup. Doing
it lazily means that cost is only paid once something actually needs a
price - and never at all on a run with no watches and no portfolio. Repeat
imports are just a sys.modules dict lookup, so the per-call overhead after
the first is nil.
"""

import re

# "NASDAQ: TSLA", "NYSE:XOM", "$AAPL" - the forms an LLM (or a headline)
# tends to hand back instead of a bare symbol.
_EXCHANGE_PREFIX = re.compile(r'^(?:NASDAQ|NYSE|AMEX|OTC|TSX|LSE)\s*:\s*', re.IGNORECASE)
# Class shares: Yahoo spells "BRK.B" as "BRK-B". Only a single trailing
# letter after the dot is a class; "BMW.DE" is an exchange suffix and must
# be left alone.
_CLASS_SHARE = re.compile(r'^([A-Z0-9]+)\.([A-Z])$')


def normalize_ticker(ticker):
    """Turn whatever the analyser returned into a symbol Yahoo will price,
    or None if nothing usable is left.

    The LLM is asked for "TSLA" but routinely returns "NASDAQ: TSLA",
    "$tsla", "BRK.B" or "N/A". Each of those used to be looked up verbatim,
    fail, and log "couldn't price" - dropping an alert's watch and paper
    trade for a formatting quibble.
    """
    if not ticker:
        return None
    raw = _EXCHANGE_PREFIX.sub('', str(ticker).strip()).lstrip('$').strip()
    words = raw.split()
    if not words:
        return None
    # "TSLA (Tesla Inc)" is a ticker with a gloss; "Apple Inc" is a company
    # name that landed in the ticker field. Several words are only a ticker
    # when the first one is already written as a symbol.
    if len(words) > 1 and words[0] != words[0].upper():
        return None
    t = words[0].upper().strip('(),;:')
    if not t or t in ('N/A', 'NA', 'NONE', 'NULL', 'UNKNOWN', '???', '-'):
        return None
    # "BRK.B" is deliberately left with its dot: Yahoo wants "BRK-B", but a
    # one-letter suffix is also how it spells exchanges ("7203.T", "VOD.L"),
    # so fetch_prices tries the dashed form only when the dotted one fails.
    # Symbols are letters/digits with an optional .XX exchange suffix or a
    # -X class; anything else (a sentence, a company name) is not a ticker.
    if not re.fullmatch(r'[A-Z0-9]{1,6}(?:[.\-][A-Z0-9]{1,4})?', t):
        return None
    return t


def fetch_prices(tickers):
    """Return {ticker: last traded price or None} for every ticker given.

    The price includes extended-hours trading, so it matches what a quote
    page shows at the same moment rather than the last regular-session close.
    """
    prices = {}
    tickers = [t for t in (tickers or []) if t]
    if not tickers:
        return prices

    prices = {t: None for t in tickers}
    # Duplicates confuse yfinance's column layout; the result dict is keyed
    # off the caller's list either way.
    unique = list(dict.fromkeys(prices))

    prices.update(_intraday_prices(unique))

    missing = [t for t in unique if prices[t] is None]
    if missing:
        # Class shares: Yahoo spells "BRK.B" as "BRK-B". The dotted form was
        # tried first because the same shape is also an exchange suffix.
        alternates = {t: t.replace('.', '-') for t in missing if _CLASS_SHARE.match(t)}
        if alternates:
            alt_prices = _intraday_prices(list(alternates.values()))
            for ticker, alt in alternates.items():
                if alt_prices.get(alt):
                    prices[ticker] = alt_prices[alt]
            missing = [t for t in unique if prices[t] is None]

    if missing:
        prices.update(_regular_session_prices(missing))

    return prices


def _intraday_prices(tickers):
    """Newest 1-minute close (extended hours included) per ticker, or None."""
    import yfinance as yf

    prices = {t: None for t in tickers}
    if not tickers:
        return prices
    try:
        data = yf.download(
            tickers, period="1d", interval="1m", prepost=True,
            progress=False, auto_adjust=False, threads=True,
        )
    except Exception as e:
        print(f"Price fetch failed: {e}")
        return prices

    if data is None or data.empty:
        return prices
    try:
        closes = data["Close"]
    except KeyError:
        return prices
    for ticker in tickers:
        if ticker not in closes:
            continue
        # A delisted/unknown ticker still gets a column, just an
        # all-NaN one, and the newest minute can be NaN mid-print.
        series = closes[ticker].dropna()
        if not series.empty:
            prices[ticker] = float(series.iloc[-1])
    return prices


def _regular_session_prices(tickers):
    """Last regular-session close for tickers the intraday chart couldn't
    price. Stale by design - it's this or nothing for those."""
    import yfinance as yf

    prices = {t: None for t in tickers}
    try:
        data = yf.Tickers(" ".join(tickers))
    except Exception as e:
        print(f"Fallback price fetch failed: {e}")
        return prices

    for ticker in tickers:
        price = None
        try:
            price = data.tickers[ticker].fast_info.get('lastPrice')
            if not price:
                info = data.tickers[ticker].info
                price = info.get('currentPrice') or info.get('regularMarketPrice')
        except Exception:
            price = None
        prices[ticker] = float(price) if price else None

    return prices
