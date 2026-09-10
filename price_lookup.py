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

# Most tickers to put in a single yfinance call.
#
# The 1-minute chart returns ~960 rows x 6 columns per ticker, and yfinance
# builds every ticker's frame before concatenating them, so one call's peak
# cost scales with the number of symbols in it. On a 1GB VM an unbounded
# batch is what turns a slowly growing watch list into a machine that stops
# answering: the box never OOM-kills anything, it just enters permanent
# reclaim. Chunking caps the peak regardless of how many watches are open,
# at the price of one extra HTTP round trip per chunk.
MAX_BATCH = 25

# Cap on how many symbols the (much more expensive) per-ticker fallback will
# try. Without it, one empty batch download promotes every open watch into
# its own sequential request.
MAX_FALLBACK = 25


def _chunked(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


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

    for batch in _chunked(unique, MAX_BATCH):
        prices.update(_intraday_prices(batch))

    missing = [t for t in unique if prices[t] is None]
    if missing:
        # Class shares: Yahoo spells "BRK.B" as "BRK-B". The dotted form was
        # tried first because the same shape is also an exchange suffix.
        alternates = {t: t.replace('.', '-') for t in missing if _CLASS_SHARE.match(t)}
        if alternates:
            alt_prices = {}
            for batch in _chunked(list(alternates.values()), MAX_BATCH):
                alt_prices.update(_intraday_prices(batch))
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


def fetch_context(ticker, published_at=None):
    """Price context for deciding whether a news move is still ahead of us
    or has already happened. Returns a dict with whichever of these could be
    worked out ({} if none):

      price                     last trade, extended hours included
      ref_close                 last regular-session close before the news
      change_since_close_pct    price vs ref_close (fraction)
      price_at_publish          last trade at/before `published_at`
      change_since_publish_pct  price vs price_at_publish
      change_5d_pct             ref_close vs the close five sessions earlier
      atr_pct                   14-day average true range / ref_close

    Only called for would-be trades - a handful a day - never per article.
    Two single-ticker requests: a month of daily bars (~21 rows) and five
    days of 1-minute bars (a few thousand rows, a few hundred KB). Only the
    columns needed are kept and the frames are dropped before returning, so
    this adds nothing lasting on top of the yfinance import itself.

    `published_at` is an aware datetime or ISO string; naive means UTC.
    """
    ctx = _context_for(ticker, published_at)
    if not ctx and _CLASS_SHARE.match(ticker or ''):
        ctx = _context_for(ticker.replace('.', '-'), published_at)
    return ctx


def _context_for(ticker, published_at):
    import yfinance as yf
    import pandas as pd

    ctx = {}
    try:
        t = yf.Ticker(ticker)
        daily = t.history(period="1mo", interval="1d", auto_adjust=False)
        minute = t.history(period="5d", interval="1m", prepost=True, auto_adjust=False)
    except Exception as e:
        print(f"Price context fetch failed for {ticker}: {e}")
        return ctx

    try:
        closes = None
        if minute is not None and not minute.empty:
            closes = minute["Close"].dropna()
        minute = None
        if closes is not None and not closes.empty:
            ctx['price'] = float(closes.iloc[-1])

        if daily is None or daily.empty:
            return ctx
        daily = daily[["High", "Low", "Close"]].dropna()
        tz = daily.index.tz

        pub = None
        if published_at:
            pub = pd.Timestamp(published_at)
            if pub.tzinfo is None:
                pub = pub.tz_localize("UTC")
        ref_time = pub if pub is not None else pd.Timestamp.now(tz="UTC")
        if tz is not None:
            ref_time = ref_time.tz_convert(tz)

        # The close the news is measured against: that day's if the news
        # came after the 16:00 close (after-hours earnings), otherwise the
        # previous session's. The daily bar for a session still in progress
        # is partial, so it is excluded either way before 16:00.
        ref_date = ref_time.date()
        dates = daily.index.date
        done = daily[dates <= ref_date] if ref_time.hour >= 16 else daily[dates < ref_date]
        if done.empty:
            return ctx

        ref_close = float(done["Close"].iloc[-1])
        price = ctx.get('price') or float(daily["Close"].iloc[-1])
        ctx['price'] = price
        ctx['ref_close'] = ref_close
        ctx['change_since_close_pct'] = (price - ref_close) / ref_close
        if len(done) >= 6:
            earlier = float(done["Close"].iloc[-6])
            ctx['change_5d_pct'] = (ref_close - earlier) / earlier

        prev = done["Close"].shift(1)
        true_range = pd.concat([done["High"] - done["Low"],
                                (done["High"] - prev).abs(),
                                (done["Low"] - prev).abs()], axis=1).max(axis=1)
        atr = float(true_range.tail(14).mean())
        if atr > 0:
            ctx['atr_pct'] = atr / ref_close

        if pub is not None and closes is not None and not closes.empty:
            if closes.index.tz is not None:
                pub = pub.tz_convert(closes.index.tz)
            before = closes[closes.index <= pub]
            if not before.empty:
                at_pub = float(before.iloc[-1])
                ctx['price_at_publish'] = at_pub
                ctx['change_since_publish_pct'] = (price - at_pub) / at_pub
    except Exception as e:
        print(f"Price context for {ticker} incomplete: {e}")
    return ctx


def _regular_session_prices(tickers):
    """Last regular-session close for tickers the intraday chart couldn't
    price. Stale by design - it's this or nothing for those.

    One request per ticker, so this is the expensive path. Two limits keep it
    from running away when the batch download comes back empty (which is what
    Yahoo does when it rate-limits a large request - and then *every* symbol
    lands here at once):

      * At most MAX_FALLBACK symbols are attempted; the rest come back None,
        which callers already handle as "couldn't price it".
      * fast_info only. The .info dict this used to fall back on is the
        heaviest call in yfinance - a full quote-summary fetch per symbol -
        and asking for it once per open watch, every WATCH_CHECK_INTERVAL,
        is what turned a slow price check into a stalled scan loop.
    """
    import yfinance as yf

    prices = {t: None for t in tickers}
    attempt = tickers[:MAX_FALLBACK]
    if len(tickers) > MAX_FALLBACK:
        print(f"Fallback price fetch: {len(tickers)} tickers needed it, "
              f"trying the first {MAX_FALLBACK} only.")
    if not attempt:
        return prices
    try:
        data = yf.Tickers(" ".join(attempt))
    except Exception as e:
        print(f"Fallback price fetch failed: {e}")
        return prices

    for ticker in attempt:
        try:
            price = data.tickers[ticker].fast_info.get('lastPrice')
        except Exception:
            price = None
        prices[ticker] = float(price) if price else None

    return prices
