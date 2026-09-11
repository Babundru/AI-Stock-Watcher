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
    is spent, and chasing it is where reversals are likeliest. Anything
    short of that is the market agreeing with the news, not a reason to
    stay out: stocks with real company news tend to keep drifting the way
    the news points.
  * Unconfirmed: a story from an opinion source (X, Reddit - see
    source_manager.article_trust) needs the price to have confirmed it
    already, by at least CONFIRM_ATR_MULT daily ranges its way.
  * Capped: the news pins the stock to a set price (a cash takeover's
    target), so there is nothing left to ride.
  * Reward vs risk: the stop is sized to the stock's volatility, and the
    story's expected move has to be at least MIN_REWARD_RISK x that stop.

A refused signal is still followed as if it had been traded
(shadow_trades.py), so each rule's refusals can be judged.

Exit - checked on every watch pass, first match wins:

  1. Stop. Starts STOP_ATR_MULT x ATR from entry. Ratchets, never loosens:
     it trails the best price by the initial distance, jumps to break-even
     once BREAKEVEN_AT of the target has been gained, and - with
     LET_WINNERS_RUN - tightens to TRAIL_AFTER_TARGET_MULT x the distance
     once the target is reached.
  2. Target (only when LET_WINNERS_RUN is off for this position).
  3. Time exit, at 15:45 ET on the horizon's last trading day - fired only
     while the US market is open, so it closes on a live price. There is
     no postponing a time exit because the position is at a loss: that
     rule is exactly what kept losers open while winners were being booked.
"""

import datetime

import pytz

import config

# Recorded on every watch and paper trade, so the ledger can compare rule
# sets instead of blending trades made under different ones.
STRATEGY_VERSION = "v3"
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

# Time exits in US trading days after the session the position opened in.
# Weekends are skipped; exchange holidays are not modelled (a time exit that
# lands on one simply fires on the next open day, when the market-open gate
# lets it).
HORIZON_TRADING_DAYS = {
    "INTRADAY": 0,
    "DAYS": 3,
    "WEEKS": 15,
}
DEFAULT_HORIZON = "DAYS"

ET = pytz.timezone("US/Eastern")
TIME_EXIT_ET = datetime.time(15, 45)
# A position opened after this (or at a weekend) counts from the next
# session: an INTRADAY trade opened at 15:30 should not be closed at 15:45.
SESSION_CUTOFF_ET = datetime.time(12, 0)


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


def plan_trade(direction, impact, expected_pct, context, needs_confirmation=False, capped=False):
    """Decide whether a signal is worth trading now, and size its exits.

    `expected_pct` is the analysis's expected total move (a fraction, or None
    to fall back to the impact bucket). `needs_confirmation` is set for a
    story from an opinion source, `capped` when the analysis says the news
    pins the stock to a set price.

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
        return skip('capped', "the news pins the stock to a set price (a cash takeover, say) - "
                              "nothing left to ride")
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
        need = config.CONFIRM_ATR_MULT * daily
        if needs_confirmation and moved < need:
            return skip('unconfirmed', f"opinion source, and the price hasn't confirmed it: "
                                       f"{moved * 100:+.1f}% the news's way so far, needs {need * 100:.1f}%")

    if expected < config.MIN_REWARD_RISK * stop:
        daily_note = f" (normal daily range {atr * 100:.1f}%)" if atr else ""
        return skip('reward_risk', f"expected move ({expected * 100:.1f}%) too small for the "
                                   f"{stop * 100:.1f}% stop this stock needs{daily_note}")

    plan['ok'] = True
    return plan


def time_exit_at(opened_at, horizon):
    """When a position opened at `opened_at` (tz-aware) hits its time exit:
    15:45 ET on the horizon's last trading day, in opened_at's timezone."""
    days = HORIZON_TRADING_DAYS.get(horizon, HORIZON_TRADING_DAYS[DEFAULT_HORIZON])
    et = opened_at.astimezone(ET)
    day = et.date()
    if et.weekday() > 4 or et.time() >= SESSION_CUTOFF_ET:
        day = _next_trading_day(day)
    for _ in range(days):
        day = _next_trading_day(day)
    exit_et = ET.localize(datetime.datetime.combine(day, TIME_EXIT_ET))
    return exit_et.astimezone(opened_at.tzinfo)


def _next_trading_day(day):
    day += datetime.timedelta(days=1)
    while day.weekday() > 4:
        day += datetime.timedelta(days=1)
    return day


def us_market_open(now=None):
    """NYSE/Nasdaq regular session (holidays not modelled)."""
    now = (now or datetime.datetime.now(pytz.utc)).astimezone(ET)
    return now.weekday() < 5 and datetime.time(9, 30) <= now.time() < datetime.time(16, 0)


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
        return ('trailing_stop' if stop >= 0 else 'stop_loss'), target_just_reached
    return None, target_just_reached
