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


def fixed_fees_pct(size):
    """Both orders' fixed fees as a fraction of a position of `size`
    dollars; 0 with no fee or no size."""
    fee = float(config.PAPER_FEE_PER_ORDER or 0)
    if fee <= 0 or not size or size <= 0:
        return 0.0
    return 2 * fee / size


def round_trip_pct(trade, size=None, base_pct=None):
    """Everything a trade pays besides the spread, as a fraction of its
    position: `base_pct` (PAPER_COST_PCT when None), the fixed fees on
    `size` dollars (the trade's own position_usd when None) and, for a
    foreign listing, the conversion both ways."""
    base = float(config.PAPER_COST_PCT if base_pct is None else base_pct)
    size = size if size is not None else trade.get('position_usd')
    fx = 2 * float(config.PAPER_FX_FEE_PCT or 0) if is_foreign(trade.get('ticker')) else 0.0
    return base + fixed_fees_pct(size) + fx
