"""Shared live-price lookup, used by the watch checker (main.py) to see
whether a stock's alerted-on move has played out.

Batches all tickers into a single yfinance call - cheap when checking many
open watches at once instead of one request per ticker.

yfinance is imported inside fetch_prices rather than at module scope on
purpose: it drags in pandas + numpy, which cost well over 100MB of resident
memory, and both main.py and server.py import this module at startup. Doing
it lazily means that cost is only paid once something actually needs a
price - and never at all on a run with no watches and no portfolio. Repeat
imports are just a sys.modules dict lookup, so the per-call overhead after
the first is nil.
"""


def fetch_prices(tickers):
    """Return {ticker: last_price or None} for every ticker given.

    fast_info is a lightweight quote lookup; .info pulls the full company
    profile and is far slower per ticker, so it's only used as a fallback
    when fast_info has nothing.
    """
    prices = {}
    if not tickers:
        return prices

    import yfinance as yf

    try:
        data = yf.Tickers(" ".join(tickers))
    except Exception as e:
        print(f"Price fetch failed: {e}")
        return {t: None for t in tickers}

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
