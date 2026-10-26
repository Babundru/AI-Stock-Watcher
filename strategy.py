"""Entry and exit rules for the positions the app opens from its alerts.

Pure functions over plain dicts - no network, no files - so the rules can be
read (and tested) in one place. main.py gathers the inputs (the analysis,
the price context from price_lookup.fetch_context, live prices) and acts on
what these return; watch_manager.py stores the resulting state per position.

Entry - a signal becomes a trade unless the price says otherwise. "Moved"
below is this story's effect alone (see news_move), in multiples of the
stock's normal daily range (14-day ATR):

  * Against the news: skip when the stock has moved against the story by
    more than AGAINST_NEWS_ATR_MULT daily ranges - the market reads it
    differently.
  * Exhausted: skip when it has already moved with the story past both
    EXHAUSTED_ATR_MULT daily ranges and the whole expected move - the move
    is spent, and chasing it is where reversals are likeliest. Past
    EXHAUSTED_MAX_ATR_MULT daily ranges it is spent whatever the expected
    move says - the AI's estimate can just repeat the headline's. Anything
    short of that is the market agreeing with the news, not a reason to
    stay out: stocks with real company news tend to keep drifting the way
    the news points.
  * Unconfirmed: a story from an opinion source (X, Reddit - see
    source_manager.article_trust) needs the price to have confirmed it
    already, by at least CONFIRM_ATR_MULT daily ranges its way.
  * Capped: the news pins the stock to a set price (a cash takeover's
    target), so there is nothing left to ride - unless config.MERGER_ARB,
    which holds a long in the target for a raised or rival bid instead
    (_merger_arb).
  * Reported: the headline itself reports the move ("jumps 30%") by at
    least REPORTED_MOVE_MAX_PCT - by the time a feed says so the move is
    done. Not applied to M&A, where a bid can still be raised or topped.
  * Too small (rule 'reward_risk'): the story has to be expected to move
    the stock clearly more than an ordinary day does - at least
    MIN_MOVE_ATR_MULT daily ranges. Until v4 it had to reach
    MIN_REWARD_RISK x the stop, i.e. 1.8 daily ranges, which turned down
    most HIGH-impact stories on anything livelier than a mega-cap.

A refused signal is still followed as if it had been traded
(shadow_trades.py), so each rule's refusals can be judged.

Exit - checked on every watch pass, first match wins. Every one of them
waits for the regular session on the position's own exchange - New York's
for a US listing, Frankfurt's for a German one (markets.py,
config.EXITS_REGULAR_HOURS_ONLY): the price feed includes extended hours,
and a stop "hit" on a thin 3am print is a fill nobody could have got. An
overnight gap is still there at the open, so waiting costs nothing and the
exit is priced where it could have traded.

  1. Stop. Starts STOP_ATR_MULT x ATR from entry. Ratchets, never loosens:
     it trails the best price by the initial distance, jumps to break-even
     once BREAKEVEN_AT of the target has been gained, and - with
     LET_WINNERS_RUN - tightens to TRAIL_AFTER_TARGET_MULT x the distance
     once the target is reached. The ratchet only reads in-session prices
     too, so an after-hours spike cannot pull the trailing stop up to a
     level that never traded.
  2. Target (only when LET_WINNERS_RUN is off for this position).
  3. Time exit, a quarter of an hour before that exchange's close on the
     horizon's last trading day (15:45 ET in New York, 17:15 CET in
     Frankfurt). There is no postponing a time exit because the position is
     at a loss: that rule is exactly what kept losers open while winners
     were being booked.
"""

import datetime
import re

import pytz

import config
import markets

# Recorded on every watch and paper trade, so the ledger can compare rule
# sets instead of blending trades made under different ones. v4: a story
# has to be big for the stock in daily ranges, not in stops (see
# config.MIN_MOVE_ATR_MULT). v5: exits (and the stop's ratchet) only read
# prices from the regular session - before it, an extended-hours print
# could close a position at a price that never traded, so v4 and earlier
# results are not comparable with v5 and should not be pooled with them.
# v6: positions open at the ask (a long) or the bid (a short) and close at
# the other side, and only on a fresh price that postdates the story with a
# spread no wider than config.ENTRY_MAX_SPREAD_PCT (fills.py). v5 and
# earlier filled at the last trade, however old, so their returns are
# flattered by the spread and by stale entries - compare, don't pool.
# v7: positions open only in the stock's regular session, like the exits
# (a pre-market entry had no working stop until the open), and a move past
# config.EXHAUSTED_MAX_ATR_MULT daily ranges is exhausted whatever the
# expected move - v6 shorted QURE after a 60% pre-market crash and lost 55%.
# v8: stops (and targets) are checked against every 1-minute bar between two
# watch checks and fill at their own level, or at the open past it after a
# gap - v7 saw one last trade every five minutes, missing dips a real stop
# order would have caught and filling the ones it did catch wherever the
# price had got to by then. Exits only ever act on a bar from the session,
# never on the stale close the price lookup falls back to. Each trade also
# pays a fixed fee per order and a currency-conversion fee (costs.py) on
# top of PAPER_COST_PCT, and positions open only on an exchange whose
# session is known (markets.py, learned from Yahoo for ones it doesn't model).
# v9 (Oct 2026 review, CHANGE.md): a headline that reports the move is
# refused ('reported', not for M&A), EXHAUSTED_MAX_ATR_MULT 6 -> 4, and a
# CRITICAL rating shrinks a position instead of growing it. A takeover target
# ('capped') is held as merger arbitrage (mode 'merger_arb') rather than
# refused, when config.MERGER_ARB is on.
STRATEGY_VERSION = "v9"
LEGACY_STRATEGY = "v1"

# Normal daily range assumed for a stock whose ATR couldn't be worked out.
FALLBACK_ATR_PCT = 0.02

LONG = "LONG"
SHORT = "SHORT"

# Fallback target by impact, for an analysis with no expected move (the
# keyword engine, or a model that left the field out).
TARGET_PCT = {
    "CRITICAL": 0.10,
    "HIGH": 0.05,
    "MEDIUM": 0.03,
    "LOW": 0.02,
}
DEFAULT_TARGET_PCT = 0.05
# Bounds on a target derived from the model's own estimate - it can say 0.3%
# or 60%, and neither is a usable exit.
TARGET_MIN_PCT = 0.015
TARGET_MAX_PCT = 0.20

# Time exits in trading days after the session the position opened in - the
# position's own exchange's sessions, not New York's. Weekends are skipped;
# exchange holidays are not modelled (a time exit that lands on one simply
# fires on the next open day, when the market-open gate lets it).
HORIZON_TRADING_DAYS = {
    "INTRADAY": 0,
    "DAYS": 3,
    "WEEKS": 15,
}
DEFAULT_HORIZON = "DAYS"

ET = pytz.timezone("US/Eastern")

# A position held as merger arbitrage (config.MERGER_ARB) carries this as
# its 'mode', and is held over this horizon.
MERGER_ARB_MODE = "merger_arb"
MERGER_ARB_HORIZON = "WEEKS"

# The event_type the prompt gives a takeover story: a continuation setup,
# where a reported move is the start (a bid can be raised or topped) rather
# than the end.
MA_EVENT = "M&A"

# A move a headline reports: "shares jump 30%", "sinks LQDA 48%", "stock is
# down more than 20%", "9.7% stock surge". The backup for the model's own
# reported_move_pct, which it may leave out.
_MOVE_VERBS = (r"jump|surg|soar|rall(?:y|ies|ied)|climb|spik|rocket|skyrocket|leap|gain|rise|rose|"
               r"plung|tumbl|sink|sank|slump|crash|fall|fell|drop|slid|plummet|tank|div|dove|"
               r"crater|skid|shed|los[est]|add|pop|declin|sour|jolt|sell-?off")
_MOVE_PATTERNS = [
    # verb, up to two words (a ticker, "as much as"), the figure
    re.compile(r"\b(?:%s)\w*\b(?:\s+(?!to\b)\S+){0,2}?\s+(\d+(?:\.\d+)?)\s?%%" % _MOVE_VERBS, re.I),
    re.compile(r"\b(?:shares?|stocks?)\s+(?:is |are |was |were )?(?:up|down)\s+"
               r"(?:(?:more than|over|nearly|almost|about|as much as)\s+)?(\d+(?:\.\d+)?)\s?%", re.I),
    re.compile(r"(\d+(?:\.\d+)?)\s?%%\s+(?:(?:stock|share)s?\s+)?(?:%s)\w*\b" % _MOVE_VERBS, re.I),
]
# A figure that follows one of these is the business's number, not the
# stock's: "revenue rises 20%", "stake rises to 10%".
_NOT_THE_STOCK = re.compile(
    r"\b(?:revenues?|sales|profits?|earnings|income|eps|margins?|dividends?|orders|bookings|"
    r"volumes?|production|output|traffic|deliveries|rates?|yields?|inflation|unemployment|"
    r"stake|holdings?|interest|costs?|prices?|users|subscribers|guidance|forecast)\W*$", re.I)


def headline_move_pct(title):
    """The stock move a headline reports, as a positive fraction - 0.30 for
    "New Era jumps 30% after ..." - or None when it reports none."""
    for pattern in _MOVE_PATTERNS:
        for m in pattern.finditer(title or ""):
            if _NOT_THE_STOCK.search(title[max(0, m.start() - 30):m.start()]):
                continue
            return float(m.group(1)) / 100.0
    return None


EVENT_TYPES = ("M&A", "EARNINGS", "GUIDANCE", "CLINICAL", "REGULATORY", "CONTRACT", "OFFERING",
               "LEGAL", "MANAGEMENT", "ANALYST", "PRODUCT", "RESTRUCTURING", "MACRO", "OTHER")
_EVENT_SYNONYMS = {"MERGER": "M&A", "ACQUISITION": "M&A", "TAKEOVER": "M&A", "MA": "M&A",
                   "M_AND_A": "M&A", "RESULTS": "EARNINGS", "FDA": "REGULATORY",
                   "TRIAL": "CLINICAL", "LAWSUIT": "LEGAL", "LITIGATION": "LEGAL"}


def parse_event_type(value):
    """The analysis's event_type as one of EVENT_TYPES; None when the engine
    gave none (the keyword engine, a p1 prompt), OTHER when unrecognised."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip().upper().replace(" ", "_")
    v = _EVENT_SYNONYMS.get(v, v).replace("_", " ")
    return v if v in EVENT_TYPES else "OTHER"


def reported_move(model_pct, title):
    """The move a story's headline reports: the model's reported_move_pct
    or the headline's own figure, whichever is larger. None if neither."""
    found = [v for v in (parse_pct(model_pct), headline_move_pct(title)) if v]
    return max(found) if found else None


# --- parsing the model's numbers ------------------------------------------

def parse_pct(value):
    """A model's percentage as a positive fraction: 6.5, "6.5", "+6.5%" ->
    0.065. None when missing, unparseable or zero. The sign is dropped -
    direction comes from the sentiment, not from this number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        v = abs(float(str(value).strip().rstrip('%').strip()))
    except ValueError:
        return None
    if v == 0:
        return None
    return min(v, 100.0) / 100.0


def parse_confidence(value):
    """0-100 integer, or None. A 0-1 fraction is scaled up."""
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(str(value).strip().rstrip('%').strip())
    except ValueError:
        return None
    if 0 < v <= 1.0:
        v *= 100
    return int(max(0, min(100, round(v))))


def parse_flag(value):
    """True/False from a JSON bool or a "yes"/"false"-style string; None
    when absent or unclear (an absent flag must not reject an alert)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ('true', 'yes', 'y', '1'):
            return True
        if v in ('false', 'no', 'n', '0'):
            return False
    return None


# --- shared arithmetic -----------------------------------------------------

def pct_move(direction, entry, price):
    """The position's gain at `price` as a fraction of entry - positive
    means in the position's favour, whichever way it faces."""
    if not entry:
        return 0.0
    if direction == SHORT:
        return (entry - price) / entry
    return (price - entry) / entry


def price_at_gain(direction, entry, gain):
    """The price at which the position shows `gain` (inverse of pct_move)."""
    if direction == SHORT:
        return entry * (1 - gain)
    return entry * (1 + gain)


def _clamp(value, low, high):
    return max(low, min(high, value))


def max_stop_pct():
    pct = config.STOP_LOSS_PCT
    return pct if pct and pct > 0 else config.DEFAULT_STOP_LOSS_PCT


def stop_pct_for(atr_pct):
    """Initial stop distance for a stock whose normal daily range is
    `atr_pct` (a fraction), within [STOP_MIN_PCT, max_stop_pct()]."""
    cap = max_stop_pct()
    if atr_pct and atr_pct > 0 and config.STOP_ATR_MULT > 0:
        return _clamp(config.STOP_ATR_MULT * atr_pct, min(config.STOP_MIN_PCT, cap), cap)
    return cap


# --- entry -----------------------------------------------------------------

def news_move(direction, context):
    """How far this story has moved the stock so far, in the trade's
    direction and net of the market: a fraction, positive when the price has
    gone the way the news points. None without price data.

    Returns {'pct', 'from', 'market_pct'}: 'from' is 'publish' or 'close',
    'market_pct' the benchmark's raw move over the same window (None when it
    couldn't be priced - then nothing is subtracted).

    News that came out during the regular session is measured from the price
    at publication, so a day that was already up or down for other reasons
    isn't counted as the story's doing. News that came out while the market
    was shut is measured from the previous close: its reaction happens in
    thin pre/after-hours trading and at the open, often ahead of the feed's
    timestamp, so the gap against the close is the reaction.
    """
    ctx = context or {}
    if ctx.get('published_in_session') and ctx.get('change_since_publish_pct') is not None:
        stock, market, since = (ctx['change_since_publish_pct'],
                                ctx.get('market_since_publish_pct'), 'publish')
    elif ctx.get('change_since_close_pct') is not None:
        stock, market, since = (ctx['change_since_close_pct'],
                                ctx.get('market_since_close_pct'), 'close')
    else:
        return None
    move = stock - (market or 0.0)
    return {'pct': -move if direction == SHORT else move, 'from': since, 'market_pct': market}


def plan_trade(direction, impact, expected_pct, context, needs_confirmation=False, capped=False,
               reported_pct=None, event_type=None):
    """Decide whether a signal is worth trading now, and size its exits.

    `expected_pct` is the analysis's expected total move (a fraction, or None
    to fall back to the impact bucket). `needs_confirmation` is set for a
    story from an opinion source, `capped` when the analysis says the news
    pins the stock to a set price. `reported_pct` is a move the headline
    itself reports (reported_move), `event_type` the analysis's category.

    Returns a dict with 'ok'; when not ok, the 'rule' that refused it and a
    readable 'reason'. The sizing is filled in either way, so a refused
    signal can still be followed as a skipped trade (shadow_trades.py).
    """
    atr = (context or {}).get('atr_pct')
    daily = atr or FALLBACK_ATR_PCT
    expected = expected_pct or TARGET_PCT.get(impact, DEFAULT_TARGET_PCT)
    move = news_move(direction, context)
    moved = move['pct'] if move else None
    stop = stop_pct_for(atr)
    # Whatever the expected move still has to go - but never less than the
    # reward the entry rule asks for. A stock already past the AI's estimate
    # is more likely the estimate being low than the move being over, and
    # with LET_WINNERS_RUN the target is where the stop starts trailing
    # closely, not an exit.
    remaining = expected - max(moved or 0.0, 0.0)
    target = _clamp(max(remaining, config.MIN_REWARD_RISK * stop), TARGET_MIN_PCT, TARGET_MAX_PCT)
    plan = {
        'ok': False,
        'rule': None,
        'reason': None,
        'expected_move_pct': round(expected, 6),
        'already_moved_pct': round(moved, 6) if moved is not None else None,
        'moved_from': move['from'] if move else None,
        'market_move_pct': round(move['market_pct'], 6) if move and move['market_pct'] is not None else None,
        'atr_pct': round(atr, 6) if atr else None,
        'stop_pct': round(stop, 6),
        'target_pct': round(target, 6),
    }

    def skip(rule, reason):
        plan['rule'] = rule
        plan['reason'] = reason
        return plan

    if capped:
        if direction == LONG and config.MERGER_ARB:
            return _merger_arb(plan, moved, daily, atr)
        return skip('capped', "the news pins the stock to a set price (a cash takeover, say) - "
                              "nothing left to ride")
    # Before any price check: a feed only writes "surges 22%" once the 22%
    # has happened. Closed trades on such headlines averaged -2.2% against
    # +7.6% for the rest (Oct 2026 review). A takeover is the exception - the
    # first bid is often not the last.
    if (reported_pct and config.REPORTED_MOVE_MAX_PCT
            and reported_pct >= config.REPORTED_MOVE_MAX_PCT
            and (event_type or '').upper() != MA_EVENT):
        return skip('reported', f"the headline already reports a {reported_pct * 100:.0f}% move - "
                                f"the news is in the price")
    if moved is None:
        if needs_confirmation:
            return skip('unconfirmed', "opinion source, and no price data to confirm it")
    else:
        unit = "its normal daily range" if atr else f"a typical {daily * 100:.0f}% daily range"
        if moved <= -config.AGAINST_NEWS_ATR_MULT * daily:
            return skip('against', f"moved {abs(moved) * 100:.1f}% against the news "
                                   f"({abs(moved) / daily:.1f}x {unit}) - the market reads it differently")
        # Both, so a big story on a calm stock isn't called spent after a
        # few ordinary days' range, nor a small one on a jumpy stock after
        # less than one.
        if moved >= max(config.EXHAUSTED_ATR_MULT * daily, expected):
            return skip('exhausted', f"already moved {moved * 100:.1f}% on the news - past the "
                                     f"{expected * 100:.1f}% expected and {moved / daily:.1f}x {unit}; "
                                     f"too late to chase")
        # However big the AI says the story is: its estimate can just repeat
        # a move the headline already reports.
        if moved >= config.EXHAUSTED_MAX_ATR_MULT * daily:
            return skip('exhausted', f"already moved {moved * 100:.1f}% on the news - "
                                     f"{moved / daily:.1f}x {unit}; too late to chase")
        need = config.CONFIRM_ATR_MULT * daily
        if needs_confirmation and moved < need:
            return skip('unconfirmed', f"opinion source, and the price hasn't confirmed it: "
                                       f"{moved * 100:+.1f}% the news's way so far, needs {need * 100:.1f}%")

    if atr:
        need = config.MIN_MOVE_ATR_MULT * atr
        why = f"a stock whose normal daily range is {atr * 100:.1f}%"
    else:
        # No daily range to compare with. The stop is then the widest one
        # there is, and a story smaller than it isn't worth taking blind.
        need = config.MIN_REWARD_RISK * stop
        why = f"the {stop * 100:.1f}% stop used without volatility data"
    if expected < need:
        return skip('reward_risk', f"expected move ({expected * 100:.1f}%) too small for {why} "
                                   f"- needs {need * 100:.1f}%")

    plan['ok'] = True
    return plan


def _merger_arb(plan, moved, daily, atr):
    """plan_trade for the target of an agreed takeover (config.MERGER_ARB):
    held for a raised or rival bid rather than refused as 'capped'.

    The price has jumped to just under the offer by now, so the usual entry
    rules don't apply: the move being "spent" is the point, and the expected
    move is the chance of a better bid, not the story's size. What still
    applies is a fall against the news - the market doubting the deal. The
    stop is fixed (MERGER_ARB_STOP_PCT, a calm post-bid stock's ATR would
    put it inside the spread to the offer), the target is a typical bump
    (MERGER_ARB_TARGET_PCT, the trailing stop takes over there), and the
    horizon is WEEKS. A deal that breaks is news against the position and
    closes it (main._close_on_reversal), or the stop does."""
    if moved is not None and moved <= -config.AGAINST_NEWS_ATR_MULT * daily:
        unit = "its normal daily range" if atr else f"a typical {daily * 100:.0f}% daily range"
        plan['rule'] = 'against'
        plan['reason'] = (f"takeover target moved {abs(moved) * 100:.1f}% against the bid "
                          f"({abs(moved) / daily:.1f}x {unit}) - the market doubts the deal")
        return plan
    plan.update(ok=True, mode=MERGER_ARB_MODE, horizon=MERGER_ARB_HORIZON,
                stop_pct=round(config.MERGER_ARB_STOP_PCT, 6),
                target_pct=round(config.MERGER_ARB_TARGET_PCT, 6))
    return plan


def time_exit_at(opened_at, horizon, ticker=None):
    """When a position opened at `opened_at` (tz-aware) hits its time exit:
    a quarter of an hour before the close on the horizon's last trading day,
    on `ticker`'s own exchange, returned in opened_at's timezone.

    For a US listing that is the 15:45 ET it has always been; for a Frankfurt
    one it is 17:15 CET, which is the last moment that position can actually
    be closed - counting its horizon in New York sessions would have held it
    past two more Frankfurt closes.
    """
    market = markets.market(ticker) or markets.MARKETS[markets.US]
    days = HORIZON_TRADING_DAYS.get(horizon, HORIZON_TRADING_DAYS[DEFAULT_HORIZON])
    local = opened_at.astimezone(market.tz)
    day = local.date()
    if local.weekday() > 4 or local.time() >= market.session_cutoff:
        day = _next_trading_day(day)
    for _ in range(days):
        day = _next_trading_day(day)
    exit_local = market.tz.localize(datetime.datetime.combine(day, market.time_exit_at))
    return exit_local.astimezone(opened_at.tzinfo)


def sessions_without_price(ticker, last_price_at, now):
    """How many of `ticker`'s exchange's sessions have closed since the day
    of its last price (`last_price_at`, tz-aware) without one - weekdays
    whose close is past by `now`. Holidays count, not being modelled, which
    is why config.NO_PRICE_CLOSE_SESSIONS is more than one."""
    market = markets.market(ticker) or markets.MARKETS[markets.US]
    day = last_price_at.astimezone(market.tz).date()
    today = now.astimezone(market.tz).date()
    count = 0
    while day < today:
        day += datetime.timedelta(days=1)
        if day.weekday() > 4:
            continue
        close = market.tz.localize(datetime.datetime.combine(day, market.close_at))
        if close <= now:
            count += 1
    return count


def session_started(ticker, when, now):
    """Whether `when` falls in today's session on `ticker`'s exchange or
    later - a price from today, not one left over from an earlier day."""
    market = markets.market(ticker) or markets.MARKETS[markets.US]
    local_now = now.astimezone(market.tz)
    opens = market.tz.localize(datetime.datetime.combine(local_now.date(), market.open_at))
    return when >= opens


def _next_trading_day(day):
    day += datetime.timedelta(days=1)
    while day.weekday() > 4:
        day += datetime.timedelta(days=1)
    return day


def us_market_open(now=None):
    """NYSE/Nasdaq regular session (holidays not modelled)."""
    now = (now or datetime.datetime.now(pytz.utc)).astimezone(ET)
    return now.weekday() < 5 and datetime.time(9, 30) <= now.time() < datetime.time(16, 0)


def market_open(ticker, now=None):
    """Whether `ticker`'s own exchange is in its regular session: True,
    False, or None when the exchange isn't one markets.py models.

    The exit gate (config.EXITS_REGULAR_HOURS_ONLY) reads this rather than
    the US session, so a London position is managed between 08:00 and 16:30
    London time - during which New York is shut for more than half - and a
    New York one is still managed 09:30-16:00 ET exactly as before.
    """
    return markets.session_open(ticker, now)


# --- exit ------------------------------------------------------------------

def update_exit(watch, price, cost_pct=0.0):
    """Ratchet an open position's stop from the price just seen, and say
    whether a price-based exit fires. Mutates `watch` (peak_gain, stop_gain,
    stop_loss_price, trailing) - the caller saves it.

    Returns (reason or None, target_just_reached). Time exits are not
    decided here; see main.py:_check_watches.
    """
    direction = watch.get('direction', LONG)
    entry = watch['entry_price']
    gain = pct_move(direction, entry, price)
    peak = max(watch.get('peak_gain') or 0.0, gain)
    target = watch.get('target_pct') or DEFAULT_TARGET_PCT
    distance = watch.get('stop_pct') or max_stop_pct()
    stop = watch.get('stop_gain')
    if stop is None:
        stop = -distance

    target_just_reached = False
    if watch.get('let_run'):
        if peak >= target and not watch.get('trailing'):
            watch['trailing'] = True
            target_just_reached = True
    elif gain >= target:
        watch['peak_gain'] = round(peak, 6)
        return 'target_hit', False

    # Ratchet: each rule can only raise the stop.
    stop = max(stop, peak - distance)
    if peak >= config.BREAKEVEN_AT * target:
        stop = max(stop, cost_pct)
    if watch.get('trailing'):
        stop = max(stop, peak - max(config.TRAIL_AFTER_TARGET_MULT * distance, 0.005))

    watch['peak_gain'] = round(peak, 6)
    watch['stop_gain'] = round(stop, 6)
    watch['stop_loss_price'] = round(price_at_gain(direction, entry, stop), 4)

    if gain <= stop:
        # A stop at or above break-even is protecting a profit, not
        # cutting a loss - reported separately so the record shows which.
        return _stop_reason(stop), target_just_reached
    return None, target_just_reached


def _stop_reason(stop):
    return 'trailing_stop' if stop >= 0 else 'stop_loss'


def update_exit_bar(watch, bar, cost_pct=0.0):
    """update_exit for one 1-minute bar (open, high, low, close) instead of
    one price: what a resting stop order (and, without LET_WINNERS_RUN, a
    target limit order) would have done during that minute. Mutates `watch`
    like update_exit.

    Returns (reason or None, exit price or None, target_just_reached). The
    exit price is the level the order was triggered at - the stop's or the
    target's price - or the bar's open when the price gapped straight past
    it, as a stop does fill at the first trade beyond it. It is a last-trade
    price; the bid/ask side is applied by the caller (fills.exit_fill).

    Within a bar the order of the high and the low is unknown, so the stop
    is checked against the bar's worst price before the bar's best price is
    allowed to ratchet it - the pessimistic reading. A stop raised by the
    bar's best price is then checked against its close, which came after.
    """
    o, h, l, c = bar
    direction = watch.get('direction', LONG)
    entry = watch['entry_price']
    worst_px, best_px = (h, l) if direction == SHORT else (l, h)
    g_open = pct_move(direction, entry, o)
    g_worst = pct_move(direction, entry, worst_px)
    distance = watch.get('stop_pct') or max_stop_pct()
    stop = watch.get('stop_gain')
    if stop is None:
        stop = -distance
    target = watch.get('target_pct') or DEFAULT_TARGET_PCT

    def done(reason, gain):
        return reason, round(price_at_gain(direction, entry, gain), 6), False

    if g_open <= stop:
        return done(_stop_reason(stop), g_open)
    if not watch.get('let_run') and g_open >= target:
        return done('target_hit', g_open)
    if g_worst <= stop:
        return done(_stop_reason(stop), stop)
    if not watch.get('let_run') and pct_move(direction, entry, best_px) >= target:
        watch['peak_gain'] = round(max(watch.get('peak_gain') or 0.0, target), 6)
        return done('target_hit', target)

    reason, reached = update_exit(watch, best_px, cost_pct)
    if reason:
        # Only possible through the ratchet just applied - the best price
        # itself is above any stop it sets.
        stop = watch['stop_gain']
        return reason, round(price_at_gain(direction, entry, stop), 6), reached
    stop = watch['stop_gain']
    if pct_move(direction, entry, c) <= stop:
        return _stop_reason(stop), round(price_at_gain(direction, entry, stop), 6), reached
    return None, None, reached
