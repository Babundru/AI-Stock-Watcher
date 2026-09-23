"""The paper record run as an account: a budget, position sizes, cash.

The ledger (paper_trader.py) records each trade as a percentage - what the
price did between the entry and the exit signal. This module turns those
percentages into money: it replays the ledger through an account that starts
with config.PAPER_BUDGET, sizes every trade by risk, ties the cash up while
the position is open, and refuses a trade the cash can't cover.

Everything here is computed from the ledger on request rather than stored,
which is what makes it cheap to run the same trades through several accounts
at once (config.PAPER_RISK_LEVELS - one graph each) and to re-size the old
trades under the current rules: nothing about an account is written down
anywhere except the trades themselves and their price marks.

Sizing (position_size)
----------------------
    size = account x risk_pct x conviction / stop distance

- "account" is the budget plus realised P/L - not marked to market, so a
  position's size doesn't swing with other positions' open gains.
- The stop distance is the trade's own (strategy.stop_pct_for: 1.5x the
  stock's daily range), so volatility is priced in: a trade that goes wrong
  costs about risk_pct of the account whichever stock it was.
- conviction is a nudge by the AI's confidence (60 -> x0.75 .. 100 -> x1.25)
  and the story's impact (CRITICAL x1.15). Deliberately small: nothing yet
  shows the model's confidence is calibrated. The ledger keeps confidence
  per trade so that can be checked, and the nudge widened if it earns it.
- Capped at PAPER_MAX_POSITION_PCT of the account and at the cash free; a
  position that would come out under PAPER_MIN_POSITION is refused.

Costs are charged at the entry (the spread is paid the moment a position
opens), so an open position is valued at size x (1 + move - cost) and a
closed one at size x (1 + net_pct) - the same figure once it closes.
"""

import datetime

import config
import strategy

LONG = strategy.LONG
SHORT = strategy.SHORT

# Conviction nudge. MIN_CONFIDENCE (60 by default) is the lowest confidence
# that raises an alert at all, so that is where the scale starts.
CONFIDENCE_LOW, CONFIDENCE_HIGH = 60, 100
CONVICTION_LOW, CONVICTION_HIGH = 0.75, 1.25
IMPACT_CONVICTION = {"CRITICAL": 1.15}


def conviction(confidence, impact):
    """Multiplier on the risk a trade is given, from its confidence and
    impact. 1.0 when neither is known (the keyword engine has no confidence)."""
    mult = 1.0
    if confidence is not None:
        c = min(max(float(confidence), CONFIDENCE_LOW), CONFIDENCE_HIGH)
        f = (c - CONFIDENCE_LOW) / (CONFIDENCE_HIGH - CONFIDENCE_LOW)
        mult = CONVICTION_LOW + f * (CONVICTION_HIGH - CONVICTION_LOW)
    return mult * IMPACT_CONVICTION.get((impact or '').upper(), 1.0)


def settings(risk_pct=None):
    """The account settings, read live from config."""
    return {
        'budget': float(config.PAPER_BUDGET),
        'risk_pct': float(config.PAPER_RISK_PCT if risk_pct is None else risk_pct),
        'max_position_pct': float(config.PAPER_MAX_POSITION_PCT),
        'min_position': float(config.PAPER_MIN_POSITION),
    }


def risk_levels():
    """The risk levels drawn as graphs: PAPER_RISK_LEVELS plus the one that
    trades, lowest first."""
    return sorted({float(r) for r in config.PAPER_RISK_LEVELS} | {float(config.PAPER_RISK_PCT)})


def position_size(account, cash, stop_pct, confidence, impact, s):
    """How much to put into a new trade: {'size', 'risk_usd', 'conviction'},
    or {'size': None, 'reason'} when it can't be opened. `s` is settings()."""
    stop = stop_pct if stop_pct and stop_pct > 0 else strategy.max_stop_pct()
    conv = conviction(confidence, impact)
    wanted = account * s['risk_pct'] * conv / stop
    size = min(wanted, account * s['max_position_pct'], cash)
    if size < s['min_position']:
        return {'size': None, 'conviction': conv,
                'reason': (f"not enough cash in the ${s['budget']:,.0f} paper account "
                           f"(${max(cash, 0):,.2f} free, ${s['min_position']:,.0f} minimum per position)")}
    return {'size': round(size, 2), 'risk_usd': round(size * stop, 2), 'conviction': round(conv, 3)}


def _epoch(value):
    try:
        return datetime.datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def _iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()


def replay(trades, cost_pct, risk_pct=None, marks=(), prices=None, now=None, curve=True):
    """Run the ledger's `trades` through a fresh account at `risk_pct`.

    `marks` are (epoch seconds, {watch_id: price}) snapshots of the open
    positions (PaperTrader.marks) and `prices` today's {ticker: price}, for
    valuing open positions along the way and now. Without them an open
    position is valued at its entry, less the cost.

    Returns the account: equity (marked to market), cash, what is invested,
    realised and unrealised P/L, max drawdown, the position each trade got
    (`sizes`, by watch id) or why it got none (`skipped`), and - unless
    curve=False - the points of its equity curve.
    """
    s = settings(risk_pct)
    now_ts = (now or datetime.datetime.now(datetime.timezone.utc)).timestamp()

    # Closes before opens at the same moment (the cash comes back first),
    # and marks last.
    events = []
    for t in trades:
        opened = _epoch(t.get('opened_at'))
        if opened is None or t.get('entry_price') in (None, 0):
            continue
        events.append((opened, 1, 'open', t))
        if t.get('status') == 'CLOSED' and t.get('net_pct') is not None:
            closed = _epoch(t.get('closed_at'))
            if closed is not None:
                events.append((max(closed, opened), 0, 'close', t))
    for ts, snapshot in marks:
        events.append((ts, 2, 'mark', snapshot))
    events.sort(key=lambda e: (e[0], e[1]))

    cash = s['budget']
    realised = 0.0
    held = {}          # watch_id -> {'size', 'trade', 'pct'}
    sizes, skipped = {}, {}
    points = []
    peak = s['budget']
    max_dd = 0.0

    def equity():
        return cash + sum(p['size'] * (1 + p['pct']) for p in held.values())

    def point(ts, kind, **extra):
        nonlocal peak, max_dd
        value = equity()
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)
        if curve:
            points.append(dict(t=_iso(ts), equity=round(value, 2), kind=kind, **extra))

    if events:
        point(events[0][0], 'start')

    for ts, _, kind, item in events:
        if kind == 'mark':
            moved = False
            for wid, price in item.items():
                p = held.get(wid)
                if p and price:
                    t = p['trade']
                    p['pct'] = strategy.pct_move(t['direction'], t['entry_price'], price) - cost_pct
                    moved = True
            if moved:
                point(ts, 'mark', open=len(held))
            continue

        t = item
        wid = t.get('watch_id')
        if kind == 'open':
            got = position_size(s['budget'] + realised, cash, t.get('stop_pct'),
                                t.get('confidence'), t.get('impact'), s)
            if got['size'] is None:
                skipped[wid] = got['reason']
                continue
            sizes[wid] = got['size']
            cash -= got['size']
            held[wid] = {'size': got['size'], 'trade': t, 'pct': -cost_pct}
            point(ts, 'open', ticker=t.get('ticker'), dir=t.get('direction'), size=got['size'])
        elif wid in held:
            p = held.pop(wid)
            pnl = p['size'] * t['net_pct']
            realised += pnl
            cash += p['size'] + pnl
            point(ts, 'close', ticker=t.get('ticker'), dir=t.get('direction'),
                  size=p['size'], net_pct=t['net_pct'], pnl=round(pnl, 2))

    # Now: open positions at today's prices, where there are any.
    for p in held.values():
        price = (prices or {}).get(p['trade']['ticker'])
        if price:
            t = p['trade']
            p['pct'] = strategy.pct_move(t['direction'], t['entry_price'], price) - cost_pct
    if events:
        point(max(now_ts, events[-1][0]), 'now', open=len(held))

    invested = sum(p['size'] for p in held.values())
    unrealised = sum(p['size'] * p['pct'] for p in held.values())
    value = equity()
    return {
        'risk_pct': s['risk_pct'],
        'budget': s['budget'],
        'equity': round(value, 2),
        'cash': round(cash, 2),
        'invested': round(invested, 2),
        'realised_pnl': round(realised, 2),
        'unrealised_pnl': round(unrealised, 2),
        'total_return': (value - s['budget']) / s['budget'] if s['budget'] else 0.0,
        'max_drawdown': max_dd,
        'taken': len(sizes),
        'open': len(held),
        'skipped_cash': len(skipped),
        'sizes': sizes,
        'skipped': skipped,
        'curve': points,
    }
