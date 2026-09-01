"""Paper-trading ledger: records what each alert would have earned or lost.

The app already makes a complete round trip on its own - an alert opens a
watch at a live entry price (main.py:_process_article), and the watch check
later closes it at a live exit price (main.py:_check_watches). That *is* a
paper trade. Nothing here changes any of it; this module only writes down
what happened, so that after a few months of running there is a track record
to judge instead of a feeling.

That is a deliberate design choice. A "test mode" that opened positions on
looser rules, or exited on different ones, would measure something other
than the app you would actually run - and the only number worth having is
the one produced by the real thing.

Two things are recorded that the watch itself does not keep:

  * **A benchmark.** Every trade stores the market's move (PAPER_BENCHMARK,
    default SPY) over the identical holding window. Without it a profitable
    month is unreadable: it could be the analyser working, or it could be
    that everything went up. The benchmark price is folded into the price
    call the watch check already makes, so it costs no extra request.

  * **Excursions (MAE/MFE).** On every price check, how far the position has
    run in your favour and against you. Purely observational - it never
    closes anything early - but it is the one thing that cannot be
    reconstructed after the fact, and it is what answers "would a stop loss
    have helped?" retroactively. See stop_loss_study().

Trades are never trimmed. WatchManager caps watches.json at 200 records
because it only needs the live ones; the whole point of this file is the
history, so it grows without bound (a few hundred bytes per trade - years
of alerts amount to well under a megabyte).
"""

import json
import os
import datetime

from local_time import now_local

LEDGER_FILE = "data/paper_trades.json"

LONG = "LONG"
SHORT = "SHORT"


def _pct_move(direction, entry, price):
    """Return the position's gain at `price` as a fraction of entry.

    Positive means the position is up, whichever way it faces: a short earns
    when the price falls, so its sign is inverted. Every percentage in this
    module is in this direction-adjusted form, which is what makes longs and
    shorts averageable together.
    """
    if not entry:
        return 0.0
    if direction == SHORT:
        return (entry - price) / entry
    return (price - entry) / entry


class PaperTrader:
    def __init__(self, filename=LEDGER_FILE, cost_pct=0.0, benchmark="SPY"):
        self.filename = filename
        # Round-trip trading cost as a fraction (spread + commission +
        # financing), subtracted from every trade's gross return. Defaults to
        # zero so an unconfigured ledger reports raw price moves rather than
        # silently applying a made-up cost.
        self.cost_pct = cost_pct
        self.benchmark = benchmark
        self.trades = self._load()
        # watch_id -> trade. The ledger is never trimmed, and mark_price is
        # called for every open watch on every check, so a linear scan of
        # the whole history each time would grow with the record's age.
        self._by_id = {t['watch_id']: t for t in self.trades if 'watch_id' in t}

    # --- storage -------------------------------------------------------

    def _load(self):
        if not os.path.exists(self.filename):
            return []
        try:
            with open(self.filename, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, list):
                return []
            return [t for t in data if isinstance(t, dict)]
        except (json.JSONDecodeError, OSError):
            # A corrupt ledger is worth surfacing rather than silently
            # restarting from empty - that would look like "no trades yet".
            print(f"Warning: could not read {self.filename}; starting a new ledger")
            return []

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.filename) or '.', exist_ok=True)
            with open(self.filename, 'w', encoding='utf-8') as f:
                json.dump(self.trades, f, indent=2)
        except OSError as e:
            print(f"Error saving paper trades: {e}")

    def reload(self):
        """Re-read the ledger from disk.

        The desktop GUI holds its own PaperTrader for display while the
        backend's instance does the writing, so the viewing copy has to pick
        up trades closed since it last looked.
        """
        self.trades = self._load()
        self._by_id = {t['watch_id']: t for t in self.trades if 'watch_id' in t}

    def _find(self, watch_id):
        return self._by_id.get(watch_id)

    def discard_trade(self, watch_id):
        """Drop a still-open trade whose watch the user dismissed by hand.

        No exit ever fired, so there is nothing to record: leaving it OPEN
        would keep it in the positions table (and in every price call)
        forever. Closed trades are never touched - they are the record.
        """
        trade = self._find(watch_id)
        if not trade or trade['status'] != 'OPEN':
            return False
        self.trades.remove(trade)
        del self._by_id[watch_id]
        self.save()
        return True

    # --- recording -----------------------------------------------------

    def open_trade(self, watch, benchmark_price=None):
        """Record the entry side of a watch that was just opened."""
        if self._find(watch['id']):
            return None

        trade = {
            "watch_id": watch['id'],
            "ticker": watch['ticker'],
            "company": watch['company'],
            "direction": watch.get('direction', LONG),
            "impact": watch.get('impact'),
            "horizon": watch.get('horizon'),
            "headline": watch.get('article_headline'),
            "url": watch.get('article_url'),
            "entry_price": watch['entry_price'],
            "target_price": watch.get('target_price'),
            "target_pct": watch.get('target_pct'),
            "opened_at": watch['opened_at'],
            "expires_at": watch.get('expires_at'),
            "benchmark_entry": benchmark_price,
            # Excursions, updated on every price check while open. Seeded at
            # zero: at the entry price the position is flat by definition.
            "mae_pct": 0.0,
            "mfe_pct": 0.0,
            "price_checks": 0,
            "status": "OPEN",
            "exit_price": None,
            "closed_at": None,
            "reason": None,
            "benchmark_exit": None,
            "gross_pct": None,
            "net_pct": None,
            "benchmark_pct": None,
            "alpha_pct": None,
            "holding_hours": None,
        }
        self.trades.append(trade)
        self._by_id[trade['watch_id']] = trade
        self.save()
        return trade

    def mark_price(self, watch_id, price):
        """Note a price observed while the position is open, updating the
        best and worst it has been. Called from the watch check for every
        open watch, including the ones that are not resolving.

        Deliberately does not close anything. The ledger observes the app's
        strategy; it does not overlay a different one.
        """
        trade = self._find(watch_id)
        if not trade or trade['status'] != 'OPEN' or not price:
            return
        move = _pct_move(trade['direction'], trade['entry_price'], price)
        trade['mfe_pct'] = max(trade['mfe_pct'], move)
        trade['mae_pct'] = min(trade['mae_pct'], move)
        trade['price_checks'] += 1
        # Not saved here - the caller saves once after marking every watch,
        # rather than rewriting the file once per open position per check.

    def close_trade(self, watch, exit_price, benchmark_price=None):
        """Finalise a trade whose watch has just closed."""
        trade = self._find(watch['id'])
        if not trade:
            # The ledger was enabled after this watch opened; record it now
            # so it is not lost, with the entry data the watch still holds.
            trade = self.open_trade(watch)
            if not trade:
                return None

        if trade['status'] != 'OPEN':
            return None

        direction = trade['direction']
        gross = _pct_move(direction, trade['entry_price'], exit_price)

        trade['status'] = 'CLOSED'
        trade['exit_price'] = exit_price
        trade['closed_at'] = watch.get('closed_at') or now_local().isoformat()
        trade['reason'] = watch.get('reason')
        trade['gross_pct'] = round(gross, 6)
        trade['net_pct'] = round(gross - self.cost_pct, 6)
        trade['benchmark_exit'] = benchmark_price

        if trade.get('benchmark_entry') and benchmark_price:
            bench = (benchmark_price - trade['benchmark_entry']) / trade['benchmark_entry']
            trade['benchmark_pct'] = round(bench, 6)
            # Alpha against a market-neutral baseline: a long is expected to
            # earn the market's move, a short to earn its negative, so that
            # expectation is what gets subtracted. A short that made 3% while
            # the market rose 2% beat its baseline by 5%, not by 1%.
            expected = bench if direction == LONG else -bench
            trade['alpha_pct'] = round(trade['net_pct'] - expected, 6)

        opened = _parse_time(trade['opened_at'])
        closed = _parse_time(trade['closed_at'])
        if opened and closed:
            trade['holding_hours'] = round((closed - opened).total_seconds() / 3600, 2)

        # Excursions must bracket the realised result even if the closing
        # price was never seen by mark_price (the check that closes a watch
        # marks it in the same pass, but a ledger enabled mid-flight, or a
        # restart, can leave gaps).
        trade['mfe_pct'] = max(trade['mfe_pct'], gross)
        trade['mae_pct'] = min(trade['mae_pct'], gross)

        self.save()
        return trade

    # --- reporting -----------------------------------------------------

    def closed(self):
        return [t for t in self.trades if t['status'] == 'CLOSED']

    def open_trades(self):
        return [t for t in self.trades if t['status'] == 'OPEN']

    def stats(self, start_capital=10000.0, position_pct=0.10):
        """Summarise the ledger. Returns None until something has closed.

        The equity curve compounds a fixed fraction of current capital into
        each trade, in the order the trades closed. That is a modelling
        choice, not something the app decides - it has no notion of position
        size - but some sizing rule is needed before "max drawdown" means
        anything, and fixed-fraction is the least arbitrary one.
        """
        trades = sorted(self.closed(), key=lambda t: t['closed_at'] or '')
        if not trades:
            return None

        rets = [t['net_pct'] for t in trades]
        wins = [r for r in rets if r > 0]
        losses = [r for r in rets if r <= 0]

        equity = start_capital
        peak = start_capital
        max_dd = 0.0
        curve = []
        for t in trades:
            equity *= (1 + t['net_pct'] * position_pct)
            peak = max(peak, equity)
            max_dd = max(max_dd, (peak - equity) / peak)
            curve.append({"closed_at": t['closed_at'], "equity": round(equity, 2)})

        alphas = [t['alpha_pct'] for t in trades if t.get('alpha_pct') is not None]
        benches = [t['benchmark_pct'] for t in trades if t.get('benchmark_pct') is not None]

        win_rate = len(wins) / len(rets)
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0

        return {
            "trades": len(trades),
            "open": len(self.open_trades()),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            # How many times bigger the average win is than the average loss.
            # With a 50% win rate anything below 1.0 loses money.
            "payoff_ratio": (avg_win / abs(avg_loss)) if avg_loss else None,
            # The number that decides profitability: expected return per
            # trade. Positive means the edge survives the losers.
            "expectancy": sum(rets) / len(rets),
            "best": max(rets),
            "worst": min(rets),
            "total_return": (equity - start_capital) / start_capital,
            "final_equity": equity,
            "max_drawdown": max_dd,
            "avg_benchmark": (sum(benches) / len(benches)) if benches else None,
            "avg_alpha": (sum(alphas) / len(alphas)) if alphas else None,
            "avg_holding_hours": _avg([t['holding_hours'] for t in trades
                                       if t.get('holding_hours') is not None]),
            "by_direction": _group(trades, lambda t: t['direction']),
            "by_impact": _group(trades, lambda t: t.get('impact') or '?'),
            "by_horizon": _group(trades, lambda t: t.get('horizon') or '?'),
            "by_reason": _group(trades, lambda t: t.get('reason') or '?'),
            "equity_curve": curve,
        }

    def live_positions(self, prices=None):
        """Open positions marked to the prices given, for the Portfolio view.

        `prices` is {ticker: price or None} as returned by
        price_lookup.fetch_prices. A position whose ticker could not be priced
        still comes back, with its unrealised figures left as None, so the UI
        can show the position rather than dropping it silently.
        """
        prices = prices or {}
        out = []
        for t in self.open_trades():
            price = prices.get(t['ticker'])
            row = dict(t)
            if price:
                gross = _pct_move(t['direction'], t['entry_price'], price)
                row['current_price'] = price
                row['unrealised_pct'] = round(gross - self.cost_pct, 6)
                # How far this position has come toward the exit that would
                # close it - the "am I nearly there" number the watch card
                # cannot show.
                target_move = t.get('target_pct') or 0
                row['progress'] = round(gross / target_move, 4) if target_move else None
            else:
                row['current_price'] = None
                row['unrealised_pct'] = None
                row['progress'] = None
            out.append(row)
        # Biggest movers first, unpriced ones last.
        out.sort(key=lambda r: (r['unrealised_pct'] is None,
                                -(r['unrealised_pct'] or 0)))
        return out

    def tickers_open(self):
        """Tickers with an open position, for a single batched price call."""
        return list({t['ticker'] for t in self.open_trades()})

    def overview(self, prices=None, start_capital=10000.0, position_pct=0.10):
        """Everything the Portfolio view needs in one object: the realised
        record, the open positions marked to market, and the two totals.

        Realised and unrealised are kept apart deliberately. Only closed
        trades are evidence of anything - an open position's paper gain can
        evaporate before its signal fires, and blending the two produces a
        number that flatters whatever the market did this week.
        """
        stats = self.stats(start_capital=start_capital, position_pct=position_pct)
        positions = self.live_positions(prices)

        marked = [p['unrealised_pct'] for p in positions
                  if p['unrealised_pct'] is not None]
        return {
            "enabled": True,
            "cost_pct": self.cost_pct,
            "benchmark": self.benchmark,
            "stats": stats,
            "positions": positions,
            "open_count": len(positions),
            "open_avg_pct": (sum(marked) / len(marked)) if marked else None,
            "closed_recent": sorted(self.closed(),
                                    key=lambda t: t['closed_at'] or '',
                                    reverse=True)[:50],
        }

    def stop_loss_study(self, levels=(0.02, 0.03, 0.05, 0.08, 0.10)):
        """What each stop-loss level would have done, from recorded MAE.

        A trade whose worst excursion was beyond the stop is assumed to have
        exited there; every other trade keeps its actual result. This is an
        approximation in one known direction: it cannot tell whether a stop
        was touched before or after the target on trades that hit both, so it
        charges the stop to some trades that would really have won. Treat a
        level that still improves expectancy under that pessimism as a
        genuine signal, and a marginal one as noise.
        """
        trades = self.closed()
        if not trades:
            return []

        base = sum(t['net_pct'] for t in trades) / len(trades)
        rows = [{"stop": None, "expectancy": base, "stopped_out": 0,
                 "win_rate": sum(1 for t in trades if t['net_pct'] > 0) / len(trades)}]

        for level in levels:
            rets, hit = [], 0
            for t in trades:
                if t['mae_pct'] <= -level:
                    rets.append(-level - self.cost_pct)
                    hit += 1
                else:
                    rets.append(t['net_pct'])
            rows.append({
                "stop": level,
                "expectancy": sum(rets) / len(rets),
                "stopped_out": hit,
                "win_rate": sum(1 for r in rets if r > 0) / len(rets),
            })
        return rows


def _parse_time(value):
    try:
        return datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _avg(values):
    return (sum(values) / len(values)) if values else None


def _group(trades, key):
    """Per-bucket trade count, win rate and expectancy - the breakdown that
    shows whether an edge lives in one corner of the alerts (say CRITICAL
    longs) rather than across all of them."""
    out = {}
    for t in trades:
        out.setdefault(key(t), []).append(t['net_pct'])
    return {
        k: {
            "trades": len(v),
            "win_rate": sum(1 for r in v if r > 0) / len(v),
            "expectancy": sum(v) / len(v),
        }
        for k, v in sorted(out.items())
    }
