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


def fetch_prices(tickers):
    """Return {ticker: last traded price or None} for every ticker given.

    The price includes extended-hours trading, so it matches what a quote
    page shows at the same moment rather than the last regular-session close.
    """
    prices = {}
    if not tickers:
        return prices

    import yfinance as yf

    prices = {t: None for t in tickers}
    # Duplicates confuse yfinance's column layout; the result dict is keyed
    # off the caller's list either way.
    unique = list(dict.fromkeys(prices))

    try:
        data = yf.download(
            unique, period="1d", interval="1m", prepost=True,
            progress=False, auto_adjust=False, threads=True,
        )
    except Exception as e:
        print(f"Price fetch failed: {e}")
        data = None

    if data is not None and not data.empty:
        try:
            closes = data["Close"]
        except KeyError:
            closes = None
        if closes is not None:
            for ticker in unique:
                if ticker not in closes:
                    continue
                # A delisted/unknown ticker still gets a column, just an
                # all-NaN one, and the newest minute can be NaN mid-print.
                series = closes[ticker].dropna()
                if not series.empty:
                    prices[ticker] = float(series.iloc[-1])

    missing = [t for t in unique if prices[t] is None]
    if missing:
        prices.update(_regular_session_prices(missing))

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
