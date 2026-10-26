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

  * **A benchmark.** Every trade stores its own market's move over the
    identical holding window - SPY for a US listing, PAPER_BENCHMARK_EU for a
    European one (markets.benchmark_for), with the symbol used written into
    the trade. Without it a profitable month is unreadable: it could be the
    analyser working, or it could be that everything went up. The benchmark
    price is folded into the price call the watch check already makes, so it
    costs no extra request.

  * **Excursions (MAE/MFE).** On every price check, how far the position has
    run in your favour and against you. Purely observational - it never
    closes anything early - but it is the one thing that cannot be
    reconstructed after the fact, and it is what answers "would a stop loss
    have helped?" retroactively. See stop_loss_study().

Excluding a trade (the dashboard's x) keeps it in the ledger, flagged
'excluded', and leaves it out of every figure: the record can't quietly get
better, the dashboard still lists it, and restoring it is one click.

Trades are never trimmed. WatchManager caps watches.json at 200 records
because it only needs the live ones; the whole point of this file is the
history, so it grows without bound (a few hundred bytes per trade - years
of alerts amount to well under a megabyte).
"""

import json
import os
import datetime

import costs
import fills
import paper_account
import strategy
from local_time import now_local

LEDGER_FILE = "data/paper_trades.json"

# What every trade and skipped trade records about the signal behind it, on
# top of the price context (main._signal_info builds them):
#   model, prompt_version  - which model of the CLOUD_AI_MODEL list judged the
#                            story, under which llm_prompts.PROMPT_VERSION
#   news_source            - the feed or custom source it came from
#   published_at           - the article's publish time (UTC), when the feed
#                            gave one
#   analysed_at            - when the verdict came back; with opened_at, the
#                            detection latency the strategy lives on
#   event_type             - the model's category for the story (M&A,
#                            earnings, FDA, ...), what results are split by
#   reported_move_pct      - a move the headline itself reports ("jumps 30%")
# Absent (None) on records from before they were kept.
SIGNAL_FIELDS = ('model', 'prompt_version', 'news_source', 'published_at', 'analysed_at',
                 'event_type', 'reported_move_pct')

# Where deleted trades used to be archived, before an x kept them in the
# ledger flagged 'excluded' (PaperTrader.exclude_trade). Read once on load,
# its trades put back as excluded, and renamed out of the way.
DELETED_FILE = "data/paper_trades_deleted.json"

# Prices of the open positions, snapshotted on every watch check, so the
# account can be drawn marked to market over time and not only at the
# moments trades close. Kept apart from the ledger: it is a chart's raw
# material, not the record. Thinned as it ages - every check for a day,
# hourly for a month, daily after that - which keeps a year of it to a few
# thousand snapshots of at most MAX_OPEN_POSITIONS prices each.
MARKS_FILE = "data/paper_marks.json"
MARKS_FULL_SECONDS = 24 * 3600
MARKS_HOURLY_SECONDS = 30 * 24 * 3600

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
    def __init__(self, filename=LEDGER_FILE, cost_pct=0.0, benchmark="SPY", marks_file=None):
        self.filename = filename
        # Beside the ledger, wherever that is (a test's temp dir included).
        self.marks_file = marks_file or os.path.join(os.path.dirname(filename) or '.',
                                                     os.path.basename(MARKS_FILE))
        # Round-trip trading cost as a fraction (spread + commission +
        # financing), subtracted from every trade's gross return. Defaults to
        # zero so an unconfigured ledger reports raw price moves rather than
        # silently applying a made-up cost.
        self.cost_pct = cost_pct
        self.benchmark = benchmark
        self.trades = self._load()
        self._fold_in_deleted()
        # watch_id -> trade. The ledger is never trimmed, and mark_price is
        # called for every open watch on every check, so a linear scan of
        # the whole history each time would grow with the record's age.
        self._by_id = {t['watch_id']: t for t in self.trades if 'watch_id' in t}
        # [(epoch seconds, {watch_id: price})], oldest first.
        self.marks = self._load_marks()

    # --- storage -------------------------------------------------------

    def _load_marks(self):
        try:
            with open(self.marks_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return [(float(ts), m) for ts, m in data if isinstance(m, dict)]
        except (OSError, ValueError, TypeError):
            return []

    def save_marks(self):
        try:
            os.makedirs(os.path.dirname(self.marks_file) or '.', exist_ok=True)
            tmp = self.marks_file + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                # Compact: this is rewritten every watch check.
                json.dump([[int(ts), m] for ts, m in self.marks], f, separators=(',', ':'))
            os.replace(tmp, self.marks_file)
        except OSError as e:
            print(f"Error saving paper marks: {e}")

    def record_marks(self, prices_by_watch, now=None):
        """Snapshot the open positions' prices ({watch_id: price}) - once per
        watch check, only for positions whose exchange is in its regular
        session (an out-of-hours print is not a price the position could be
        valued at, which is also why exits wait for the session). Not saved
        here; the watch check calls save_marks() once per pass."""
        marks = {wid: round(float(p), 4) for wid, p in (prices_by_watch or {}).items()
                 if p and wid in self._by_id and self._by_id[wid]['status'] == 'OPEN'}
        if not marks:
            return False
        ts = (now or now_local()).timestamp()
        self.marks.append((ts, marks))
        self.marks = _thin_marks(self.marks, ts)
        return True

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
        self.marks = self._load_marks()

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

    def exclude_trade(self, watch_id, excluded=True):
        """Leave a closed trade out of the record at the user's request (a
        data glitch, a rule since fixed) - or, with excluded=False, put it
        back. Every figure is replayed from the counted trades, so it drops
        out of (or back into) all of them at once.

        The trade stays in the ledger, flagged, rather than being removed:
        the dashboard keeps listing it, with what leaving it out changed, so
        the record can't improve without that showing. An open trade can't
        be excluded - it is still live, and closes through its exits or the
        Sell button.
        """
        trade = self._find(watch_id)
        if not trade or trade['status'] != 'CLOSED':
            return False
        if excluded:
            trade['excluded'] = True
            trade['excluded_at'] = now_local().isoformat()
        else:
            trade.pop('excluded', None)
            trade.pop('excluded_at', None)
        self.save()
        return True

    def _fold_in_deleted(self):
        """Trades deleted before exclusion existed sit in DELETED_FILE beside
        the ledger: put them back, excluded, and move the file aside."""
        archive = os.path.join(os.path.dirname(self.filename) or '.',
                               os.path.basename(DELETED_FILE))
        if not os.path.exists(archive):
            return
        try:
            with open(archive, 'r', encoding='utf-8') as f:
                deleted = json.load(f)
        except (OSError, ValueError):
            return
        known = {t.get('watch_id') for t in self.trades}
        added = 0
        for t in deleted if isinstance(deleted, list) else []:
            if isinstance(t, dict) and t.get('watch_id') not in known:
                t = dict(t, excluded=True)
                t['excluded_at'] = t.pop('deleted_at', None) or now_local().isoformat()
                self.trades.append(t)
                known.add(t['watch_id'])
                added += 1
        if added:
            self.trades.sort(key=lambda t: t.get('opened_at') or '')
            self.save()
        try:
            os.replace(archive, archive + '.merged')
        except OSError:
            pass

    # --- recording -----------------------------------------------------

    def open_trade(self, watch, benchmark_price=None, benchmark=None):
        """Record the entry side of a watch that was just opened.

        `benchmark` names the index this trade's move is compared against -
        the US one for a US listing, the European one for a European listing
        (markets.benchmark_for). Stored per trade because the ledger spans
        both, and a column of alpha figures measured against two different
        indices is unreadable without knowing which was which.
        """
        if self._find(watch['id']):
            return None

        trade = {
            "watch_id": watch['id'],
            "ticker": watch['ticker'],
            "company": watch['company'],
            "direction": watch.get('direction', LONG),
            # Which rule set opened it (strategy.STRATEGY_VERSION), plus what
            # the decision was based on - so the record can be split by the
            # rules in force and by how confident the AI was.
            "strategy": watch.get('strategy', 'v1'),
            # Which model judged the story and on which prompt, where it came
            # from, and when it was published and analysed - so detection
            # latency (published -> analysed -> opened_at) can be measured.
            **{key: watch.get(key) for key in SIGNAL_FIELDS},
            # 'merger_arb' for a takeover target held for a better bid
            # (strategy._merger_arb); None for an ordinary news trade.
            "mode": watch.get('mode'),
            # Size and liquidity in rough dollars (main._universe_problem).
            "market_cap_usd": watch.get('market_cap_usd'),
            "turnover_usd": watch.get('turnover_usd'),
            "confidence": watch.get('confidence'),
            "source_trust": watch.get('source_trust'),
            "expected_move_pct": watch.get('expected_move_pct'),
            # v3: the story's own move (net of the market) and whether it
            # was measured from publication or the previous close. v2 stored
            # the larger raw move of the two here.
            "already_moved_pct": watch.get('already_moved_pct'),
            "moved_from": watch.get('moved_from'),
            "market_move_pct": watch.get('market_move_pct'),
            "atr_pct": watch.get('atr_pct'),
            "stop_pct": watch.get('stop_pct'),
            "impact": watch.get('impact'),
            "horizon": watch.get('horizon'),
            "headline": watch.get('article_headline'),
            "url": watch.get('article_url'),
            "entry_price": watch['entry_price'],
            # The fill above is the ask (a long) or the bid (a short); the
            # last trade it was worked out from, and the spread paid, are
            # kept beside it (fills.py). Absent on trades recorded before
            # fills were modelled, which opened at the last trade.
            "entry_last": watch.get('entry_last'),
            "spread_pct": watch.get('spread_pct'),
            "spread_source": watch.get('spread_source'),
            # What the trading account put in when it opened (informational:
            # the accounts re-size every trade from the ledger on replay).
            "position_usd": watch.get('position_usd'),
            "target_price": watch.get('target_price'),
            "target_pct": watch.get('target_pct'),
            "opened_at": watch['opened_at'],
            "expires_at": watch.get('expires_at'),
            "benchmark": benchmark or self.benchmark,
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

    def note_half_exit(self, watch_id, gain):
        """Record that an open trade sold half at its target, at `gain`
        (strategy.update_exit's half_exit_gain), so the account values what
        is left of it as what it is (paper_account)."""
        trade = self._find(watch_id)
        if trade and trade['status'] == 'OPEN' and trade.get('half_exit_gain') is None:
            trade['half_exit_gain'] = gain
            self.save()

    def mark_price(self, watch_id, price, low=None, high=None):
        """Note a price observed while the position is open, updating the
        best and worst it has been - from `low` and `high` too when given
        (the range of the minute bars since the last check). Called from the
        watch check for every open watch, including the ones that are not
        resolving.

        Deliberately does not close anything. The ledger observes the app's
        strategy; it does not overlay a different one.
        """
        trade = self._find(watch_id)
        if not trade or trade['status'] != 'OPEN' or not price:
            return
        for p in (price, low, high):
            if p:
                move = _pct_move(trade['direction'], trade['entry_price'], p)
                trade['mfe_pct'] = max(trade['mfe_pct'], move)
                trade['mae_pct'] = min(trade['mae_pct'], move)
        trade['price_checks'] += 1
        # Not saved here - the caller saves once after marking every watch,
        # rather than rewriting the file once per open position per check.

    def close_trade(self, watch, exit_price, benchmark_price=None, fill=None):
        """Finalise a trade whose watch has just closed. `exit_price` is the
        fill - the bid for a long, the ask for a short - and `fill` the
        fills.exit_fill it came from, whose last trade and spread are kept."""
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
        if watch.get('half_exit_gain') is not None:
            trade['half_exit_gain'] = watch['half_exit_gain']
        # With half sold at the target, the whole position's result
        # (strategy.blended_gain); the exit price is the second half's.
        gross = strategy.blended_gain(trade, _pct_move(direction, trade['entry_price'], exit_price))

        trade['status'] = 'CLOSED'
        trade['exit_price'] = exit_price
        if fill:
            trade['exit_last'] = fill['last']
            trade['exit_spread_pct'] = fill['spread_pct']
            trade['exit_spread_source'] = fill['spread_source']
        trade['closed_at'] = watch.get('closed_at') or now_local().isoformat()
        trade['reason'] = watch.get('reason')
        trade['gross_pct'] = round(gross, 6)
        # Fees and currency conversion (costs.py), on the size this
        # position was opened with.
        trade['cost_pct'] = round(costs.round_trip_pct(trade, base_pct=self.cost_pct), 6)
        trade['net_pct'] = round(gross - trade['cost_pct'], 6)
        if watch.get('exit_price_stale'):
            trade['exit_price_stale'] = True
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

    def counted(self):
        """The trades every figure is built from: all but the excluded."""
        return [t for t in self.trades if not t.get('excluded')]

    def closed(self):
        return [t for t in self.counted() if t['status'] == 'CLOSED']

    def excluded(self):
        return [t for t in self.trades if t.get('excluded')]

    def last_mark(self, watch_id):
        """(epoch seconds, price) of the newest mark of an open position,
        or (None, None)."""
        for ts, m in reversed(self.marks):
            if watch_id in m:
                return ts, m[watch_id]
        return None, None

    def open_trades(self):
        return [t for t in self.trades if t['status'] == 'OPEN']

    def stats(self):
        """Summarise the ledger, trade by trade, in percentages. Returns None
        until something has closed. What the trades did to an account - in
        money, with sizes and a cash limit - is accounts()."""
        trades = sorted(self.closed(), key=lambda t: t['closed_at'] or '')
        if not trades:
            return None

        rets = [t['net_pct'] for t in trades]
        wins = [r for r in rets if r > 0]
        losses = [r for r in rets if r <= 0]

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
            "avg_benchmark": (sum(benches) / len(benches)) if benches else None,
            "avg_alpha": (sum(alphas) / len(alphas)) if alphas else None,
            # Bid-ask spread paid per round trip, on the trades that
            # recorded one (both sides); it is already inside every return.
            "avg_spread": _avg([t['spread_pct'] / 2 + t['exit_spread_pct'] / 2 for t in trades
                                if t.get('spread_pct') is not None
                                and t.get('exit_spread_pct') is not None]),
            "spread_trades": sum(1 for t in trades if t.get('spread_pct') is not None
                                 and t.get('exit_spread_pct') is not None),
            "avg_holding_hours": _avg([t['holding_hours'] for t in trades
                                       if t.get('holding_hours') is not None]),
            "by_strategy": _group(trades, lambda t: t.get('strategy') or 'v1'),
            "by_mode": _group(trades, lambda t: t.get('mode') or 'news'),
            "by_direction": _group(trades, lambda t: t['direction']),
            "by_impact": _group(trades, lambda t: t.get('impact') or '?'),
            "by_horizon": _group(trades, lambda t: t.get('horizon') or '?'),
            "by_reason": _group(trades, lambda t: t.get('reason') or '?'),
            "by_confidence": _group(trades, _confidence_bucket),
            # Recorded from Oct 2026 (SIGNAL_FIELDS); older trades show '?'.
            "by_model": _group(trades, lambda t: t.get('model') or '?'),
            "by_prompt": _group(trades, lambda t: t.get('prompt_version') or '?'),
            "by_event_type": _group(trades, lambda t: t.get('event_type') or '?'),
            "by_source": _group(trades, lambda t: (t.get('news_source') or '?').replace('Custom/', '')),
            # Minutes from publication to the verdict and to the position,
            # median over the trades that recorded both ends.
            "latency_analysed_min": _median([_minutes_between(t.get('published_at'), t.get('analysed_at'))
                                             for t in trades]),
            "latency_opened_min": _median([_minutes_between(t.get('published_at'), t.get('opened_at'))
                                           for t in trades]),
        }

    # --- the account ---------------------------------------------------

    def accounts(self, prices=None, curve=True):
        """The ledger run through the paper account (paper_account.risk_levels:
        the trading level, config.PAPER_RISK_PCT), marked to `prices` now -
        the dashboard's graph. The trading level is flagged 'primary'."""
        primary = paper_account.settings()['risk_pct']
        out = []
        for risk in paper_account.risk_levels():
            acct = paper_account.replay(self.counted(), self.cost_pct, risk_pct=risk,
                                        marks=self.marks, prices=prices, curve=curve)
            acct['primary'] = abs(risk - primary) < 1e-12
            out.append(acct)
        return out

    def size_new_trade(self, stop_pct, confidence, impact):
        """Size a trade about to open, in the trading account as it stands:
        {'size', 'risk_usd', 'conviction', 'account', 'cash', 'budget'}, or
        {'size': None, 'reason', ...} when the cash can't cover it."""
        acct = paper_account.replay(self.counted(), self.cost_pct, curve=False)
        s = paper_account.settings()
        got = paper_account.position_size(s['budget'] + acct['realised_pnl'], acct['cash'],
                                          stop_pct, confidence, impact, s)
        got.update(account=round(s['budget'] + acct['realised_pnl'], 2), cash=acct['cash'],
                   budget=s['budget'])
        return got

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
                # Valued at what closing would get: the last trade less half
                # the spread it opened across.
                row['unrealised_pct'] = round(gross - costs.round_trip_pct(t, base_pct=self.cost_pct)
                                              - fills.exit_haircut(t), 6)
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

    def overview(self, prices=None):
        """Everything the Portfolio view needs in one object: the realised
        record, the open positions marked to market, and the two totals.

        Realised and unrealised are kept apart deliberately. Only closed
        trades are evidence of anything - an open position's paper gain can
        evaporate before its signal fires, and blending the two produces a
        number that flatters whatever the market did this week.
        """
        stats = self.stats()
        positions = self.live_positions(prices)
        accounts = self.accounts(prices)
        # Each open position's size in the trading account, and what that
        # is worth now - or why that account couldn't take it.
        primary = next((a for a in accounts if a['primary']), None)
        for p in positions:
            size = primary['sizes'].get(p['watch_id']) if primary else None
            p['position_usd'] = size
            p['skipped_reason'] = primary['skipped'].get(p['watch_id']) if primary else None
            p['unrealised_usd'] = (round(size * p['unrealised_pct'], 2)
                                   if size and p['unrealised_pct'] is not None else None)
        # The same for closed trades: what each made or lost in money.
        closed_recent = []
        for t in sorted(self.closed(), key=lambda t: t['closed_at'] or '', reverse=True)[:50]:
            size = primary['sizes'].get(t['watch_id']) if primary else None
            net = primary['nets'].get(t['watch_id'], t['net_pct']) if primary else t['net_pct']
            closed_recent.append(dict(t, position_usd=size, net_pct=net,
                                      pnl_usd=round(size * net, 2) if size else None))
        # Excluded trades are listed too, flagged, with what leaving them out
        # did to the account: the same replay with them counted, so the
        # difference covers knock-on effects (cash they would have tied up).
        excluded = self.excluded()
        excluded_effect = None
        if excluded and primary:
            everything = paper_account.replay(self.trades, self.cost_pct,
                                              risk_pct=primary['risk_pct'], marks=self.marks,
                                              prices=prices, curve=False)
            excluded_effect = round(primary['equity'] - everything['equity'], 2)
            for t in excluded:
                size = everything['sizes'].get(t['watch_id'])
                net = everything['nets'].get(t['watch_id'], t['net_pct'])
                closed_recent.append(dict(t, position_usd=size, net_pct=net,
                                          pnl_usd=round(size * net, 2) if size else None))
            closed_recent.sort(key=lambda t: t.get('closed_at') or '', reverse=True)
        for a in accounts:
            # Per-trade detail only the rows above need.
            a.pop('skipped', None)
            a.pop('sizes', None)
            a.pop('nets', None)

        marked = [p['unrealised_pct'] for p in positions
                  if p['unrealised_pct'] is not None]
        return {
            "enabled": True,
            "cost_pct": self.cost_pct,
            "benchmark": self.benchmark,
            "stats": stats,
            "accounts": accounts,
            "positions": positions,
            "open_count": len(positions),
            "open_avg_pct": (sum(marked) / len(marked)) if marked else None,
            "closed_recent": closed_recent,
            "excluded_count": len(excluded),
            # How much higher (+) or lower the account stands for leaving
            # the excluded trades out.
            "excluded_effect_usd": excluded_effect,
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


def _thin_marks(marks, now_ts):
    """Keep every snapshot from the last day, the last of each hour for the
    month before, and the last of each day before that."""
    kept, seen = [], set()
    for ts, m in reversed(marks):
        age = now_ts - ts
        if age <= MARKS_FULL_SECONDS:
            kept.append((ts, m))
            continue
        bucket = ('h', int(ts // 3600)) if age <= MARKS_HOURLY_SECONDS else ('d', int(ts // 86400))
        if bucket not in seen:
            seen.add(bucket)
            kept.append((ts, m))
    kept.reverse()
    return kept


def _confidence_bucket(trade):
    """"60-69", "70-79", ... - for checking whether the model's confidence
    means anything before the position sizing leans on it harder."""
    c = trade.get('confidence')
    if c is None:
        return '?'
    low = min(int(c) // 10 * 10, 90)
    return f"{low}-{low + 9 if low < 90 else 100}"


def _avg(values):
    return (sum(values) / len(values)) if values else None


def _median(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def _minutes_between(start, end):
    """Minutes from one ISO timestamp to another, or None if either is
    missing or unreadable. A naive timestamp is taken as UTC."""
    try:
        a = datetime.datetime.fromisoformat(start)
        b = datetime.datetime.fromisoformat(end)
    except (TypeError, ValueError):
        return None
    if a.tzinfo is None:
        a = a.replace(tzinfo=datetime.timezone.utc)
    if b.tzinfo is None:
        b = b.replace(tzinfo=datetime.timezone.utc)
    return (b - a).total_seconds() / 60


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
