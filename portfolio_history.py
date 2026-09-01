"""Reconstructs daily portfolio value/cost-basis/profit since each holding's
buy date, for the dashboard's value graph (server.py: GET /api/portfolio/history).

Uses yfinance's historical daily closes rather than our own stored snapshots,
so the chart is populated immediately instead of only filling in from the day
this feature shipped.

pandas/yfinance are imported inside compute_history rather than at module
scope: together they are the single largest block of resident memory in the
server process (>100MB), server.py imports this module at startup, and this
endpoint is only hit when the dashboard's Portfolio tab is opened. Deferring
the import means an empty portfolio never pays for them at all. See
price_lookup.py for the same reasoning on the quote path.
"""
import datetime


def compute_history(portfolio):
    """portfolio: {ticker: {"buy_price": float, "shares": float, "buy_date": "YYYY-MM-DD"}}

    Returns {"dates": [...], "value": [...], "cost_basis": [...], "profit": [...]},
    each list empty if there's nothing to plot (no holding has both shares and
    a buy date).
    """
    empty = {"dates": [], "value": [], "cost_basis": [], "profit": []}

    holdings = {
        ticker: data for ticker, data in portfolio.items()
        if data.get("shares", 0) > 0 and data.get("buy_date")
    }
    if not holdings:
        return empty

    import pandas as pd
    import yfinance as yf

    earliest_buy = min(data["buy_date"] for data in holdings.values())

    closes_by_ticker = {}
    for ticker, data in holdings.items():
        try:
            hist = yf.Ticker(ticker).history(start=data["buy_date"])
            closes = hist["Close"]
            if closes.empty:
                continue
            # Drop intraday/tz info - we only need one price per calendar day.
            closes.index = closes.index.tz_localize(None).normalize()
            closes_by_ticker[ticker] = closes
        except Exception as e:
            print(f"portfolio_history: couldn't fetch history for {ticker}: {e}")

    if not closes_by_ticker:
        return empty

    all_dates = pd.date_range(start=earliest_buy, end=datetime.date.today(), freq='D')
    value = pd.Series(0.0, index=all_dates)
    cost_basis = pd.Series(0.0, index=all_dates)

    for ticker, closes in closes_by_ticker.items():
        data = holdings[ticker]
        shares = data["shares"]
        buy_date = pd.Timestamp(data["buy_date"])

        # ffill covers ordinary gaps (weekends/holidays); bfill covers the
        # rare case where buy_date itself isn't a trading day, so the series
        # still has a price for it instead of a leading NaN.
        aligned = closes.reindex(all_dates).ffill().bfill()

        owned = all_dates >= buy_date
        value += aligned.fillna(0.0) * shares * owned
        cost_basis += (data["buy_price"] * shares) * owned

    # Trim leading days before any holding was actually owned.
    first_owned = min(pd.Timestamp(data["buy_date"]) for data in holdings.values())
    value = value[value.index >= first_owned]
    cost_basis = cost_basis[cost_basis.index >= first_owned]
    profit = value - cost_basis

    return {
        "dates": [d.strftime('%Y-%m-%d') for d in value.index],
        "value": [round(v, 2) for v in value.tolist()],
        "cost_basis": [round(c, 2) for c in cost_basis.tolist()],
        "profit": [round(p, 2) for p in profit.tolist()],
    }
