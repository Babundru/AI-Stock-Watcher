"""Shared live-price lookup, used by the watch checker (main.py) to see
whether a stock's alerted-on move has played out.

Batches all tickers into a single yfinance call - cheap when checking many
open watches at once instead of one request per ticker.
"""
import yfinance as yf


def fetch_prices(tickers):
    """Return {ticker: last_price or None} for every ticker given.

    fast_info is a lightweight quote lookup; .info pulls the full company
    profile and is far slower per ticker, so it's only used as a fallback
    when fast_info has nothing.
    """
    prices = {}
    if not tickers:
        return prices

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
