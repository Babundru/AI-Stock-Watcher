"""What a broker charges for a paper trade, on top of the bid-ask spread.

The spread is paid in the fill itself (fills.py). Everything else is here,
as one round-trip fraction of the position subtracted from its return:

  * PAPER_COST_PCT - a percentage of the position: commission charged as a
    percentage, overnight financing, a CFD broker's markup.
  * PAPER_FEE_PER_ORDER - a fixed amount per order, paid opening and again
    closing. As a fraction it depends on the position's size, which is the
    point: $1 is nothing on $1,000 and 4% on $25.
  * PAPER_FX_FEE_PCT - converting dollars into the stock's currency and
    back, for anything not listed in the US.

Pure: no network, no files.
"""

import config
import markets


def is_foreign(ticker):
    """Whether buying `ticker` means converting the account's dollars - any
    listing outside the US."""
    return markets.market_key(ticker) != markets.US


# Rough dollars per unit of the currencies the app trades in (Oct 2026), for
# the liquidity floors in main._universe_problem - a floor needs the order of
# magnitude, not today's rate, so no live FX lookup.
APPROX_USD_PER_UNIT = {
    "USD": 1.0, "EUR": 1.08, "GBP": 1.27, "CHF": 1.12, "SEK": 0.095, "NOK": 0.093,
    "DKK": 0.145, "PLN": 0.25, "CZK": 0.043, "HUF": 0.0028, "CAD": 0.73, "AUD": 0.66,
    "JPY": 0.0067, "HKD": 0.128, "ILS": 0.27, "ZAR": 0.055,
}
# Yahoo quotes some listings in the minor unit - London in pence ("GBp").
# Their prices, and so their turnover, are in it; their market cap is not.
MINOR_UNITS = {"GBp": ("GBP", 100), "GBX": ("GBP", 100), "ILA": ("ILS", 100), "ZAc": ("ZAR", 100)}


def approx_usd(amount, currency, minor_units=True):
    """`amount` in `currency` as rough dollars, or None for an unknown
    currency. `minor_units`: the amount is in the quote's own unit (a price
    or turnover - pence for "GBp"); False for Yahoo's market cap, which is
    in the major unit."""
    if amount is None:
        return None
    currency = currency or "USD"
    if currency in MINOR_UNITS:
        major, per = MINOR_UNITS[currency]
        amount = amount / per if minor_units else amount
        currency = major
    rate = APPROX_USD_PER_UNIT.get(currency.upper())
    return amount * rate if rate else None


def fixed_fees_pct(size, orders=2):
    """`orders` orders' fixed fees - both of a round trip by default - as a
    fraction of a position of `size` dollars; 0 with no fee or no size."""
    fee = float(config.PAPER_FEE_PER_ORDER or 0)
    if fee <= 0 or not size or size <= 0:
        return 0.0
    return orders * fee / size


def round_trip_pct(trade, size=None, base_pct=None):
    """Everything a trade pays besides the spread, as a fraction of its
    position: `base_pct` (PAPER_COST_PCT when None), the fixed fees on
    `size` dollars (the trade's own position_usd when None) and, for a
    foreign listing, the conversion both ways. A trade that sold half at
    its target (half_exit_gain) placed a third order."""
    base = float(config.PAPER_COST_PCT if base_pct is None else base_pct)
    size = size if size is not None else trade.get('position_usd')
    fx = 2 * float(config.PAPER_FX_FEE_PCT or 0) if is_foreign(trade.get('ticker')) else 0.0
    orders = 3 if trade.get('half_exit_gain') is not None else 2
    return base + fixed_fees_pct(size, orders) + fx
