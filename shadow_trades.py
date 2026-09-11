"""Signals the entry rules turned down, followed as if they had been traded.

The paper ledger (paper_trader.py) says how the trades the app took went.
It can't say whether a rule that kept the app out of a trade was right to,
and the entry rules in strategy.plan_trade are guesses until something
measures them. This does: a signal a rule refuses is opened here at the
price it was refused at, run under the same exits a real position gets
(strategy.update_exit and the time exit, applied by main.py:_check_watches),
and closed when one of them fires. Grouped by the rule that refused it, the
result says what each rule is saving the app from - or costing it.

Kept out of paper_trades.json on purpose: that file is what the app did,
and mixing in what it didn't do would make its record say something else.
Nothing here is ever notified.

Bounded, because every open one is priced on every watch check on a 1GB VM
(see handoff/architecture.md): at most config.MAX_SHADOW_POSITIONS open, one
per ticker, and only the newest MAX_STORED_CLOSED closed ones are kept.
"""

import json
import os
import uuid

import config
import strategy
from local_time import now_local

SHADOW_FILE = "data/shadow_trades.json"

# Closed records kept. Plenty for any per-rule comparison worth making;
# unlike the real ledger this isn't the record of anything that happened, so
# it can forget its oldest.
MAX_STORED_CLOSED = 1000

# Readable names for strategy.plan_trade's refusal rules, for the reports.
RULE_LABELS = {
    'against': "moved against the news",
    'exhausted': "move already spent",
    'unconfirmed': "opinion source, unconfirmed",
    'capped': "price capped by the news",
    'reward_risk': "story too small for the stock",
}


class ShadowBook:
    def __init__(self, filename=SHADOW_FILE, cost_pct=0.0):
        self.filename = filename
        # The same round-trip cost the paper ledger charges, so the two
        # records compare like for like.
        self.cost_pct = cost_pct
        self.records = self._load()

    # --- storage -------------------------------------------------------

    def _load(self):
        if not os.path.exists(self.filename):
            return []
        try:
            with open(self.filename, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            print(f"Warning: could not read {self.filename}; starting with no skipped trades")
            return []
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.filename) or '.', exist_ok=True)
            with open(self.filename, 'w', encoding='utf-8') as f:
                json.dump(self.records, f, indent=2)
        except OSError as e:
            print(f"Error saving skipped trades: {e}")

    def reload(self):
        """Re-read the file - for the desktop GUI's viewing copy, which
        doesn't do the writing."""
        self.records = self._load()

    # --- following -----------------------------------------------------

    def open_records(self):
        return [r for r in self.records if r['status'] == 'OPEN']

    def closed(self):
        return [r for r in self.records if r['status'] == 'CLOSED']

    def has_open(self, ticker):
        return any(r['ticker'] == ticker and r['status'] == 'OPEN' for r in self.records)

    def is_full(self):
        return len(self.open_records()) >= config.MAX_SHADOW_POSITIONS

    def track(self, ticker, company, direction, entry_price, plan, horizon=None,
              impact=None, confidence=None, source_trust=None, headline=None,
              url=None, benchmark_price=None):
        """Start following a signal strategy.plan_trade refused. Returns the
        record, or None when there is no price, the ticker is already being
        followed (one story tends to arrive as several articles), or
        MAX_SHADOW_POSITIONS are open already."""
        if not ticker or not entry_price or self.has_open(ticker) or self.is_full():
            return None
        horizon = (horizon or '').upper()
        if horizon not in strategy.HORIZON_TRADING_DAYS:
            horizon = strategy.DEFAULT_HORIZON
        stop_pct = plan['stop_pct']
        opened_at = now_local()
        record = {
            "id": uuid.uuid4().hex[:12],
            "ticker": ticker,
            "company": company or ticker,
            "direction": direction,
            "strategy": strategy.STRATEGY_VERSION,
            # Which rule refused it, and why - what the results are split by.
            "skip_rule": plan.get('rule'),
            "skip_reason": plan.get('reason'),
            "source_trust": source_trust,
            "impact": impact,
            "horizon": horizon,
            "confidence": confidence,
            "expected_move_pct": plan.get('expected_move_pct'),
            "already_moved_pct": plan.get('already_moved_pct'),
            "moved_from": plan.get('moved_from'),
            "market_move_pct": plan.get('market_move_pct'),
            "atr_pct": plan.get('atr_pct'),
            "headline": headline,
            "url": url,
            "entry_price": entry_price,
            # The exit state strategy.update_exit reads and ratchets - the
            # same fields a real watch carries (watch_manager.add_watch).
            "target_pct": plan['target_pct'],
            "stop_pct": stop_pct,
            "stop_gain": round(-stop_pct, 6),
            "stop_loss_price": round(strategy.price_at_gain(direction, entry_price, -stop_pct), 4),
            "peak_gain": 0.0,
            "let_run": bool(config.LET_WINNERS_RUN),
            "trailing": False,
            "opened_at": opened_at.isoformat(),
            "expires_at": strategy.time_exit_at(opened_at, horizon).isoformat(),
            "benchmark_entry": benchmark_price,
            "mae_pct": 0.0,
            "mfe_pct": 0.0,
            "last_price": entry_price,
            "status": "OPEN",
            "exit_price": None,
            "closed_at": None,
            "reason": None,
            "benchmark_exit": None,
            "gross_pct": None,
            "net_pct": None,
            "benchmark_pct": None,
            "alpha_pct": None,
        }
        self.records.append(record)
        self._trim()
        self.save()
        return record

    def mark_price(self, record, price):
        """Note a price seen while open. Not saved here - the watch check
        saves once per pass."""
        move = strategy.pct_move(record['direction'], record['entry_price'], price)
        record['mfe_pct'] = round(max(record['mfe_pct'], move), 6)
        record['mae_pct'] = round(min(record['mae_pct'], move), 6)
        record['last_price'] = price

    def close(self, record, reason, price, benchmark_price=None, now=None):
        """Settle a record whose exit fired. Not saved here, like mark_price."""
        gross = strategy.pct_move(record['direction'], record['entry_price'], price)
        record.update(
            status='CLOSED', reason=reason, exit_price=price,
            closed_at=(now or now_local()).isoformat(),
            gross_pct=round(gross, 6), net_pct=round(gross - self.cost_pct, 6),
            benchmark_exit=benchmark_price,
            mfe_pct=round(max(record['mfe_pct'], gross), 6),
            mae_pct=round(min(record['mae_pct'], gross), 6),
        )
        start = record.get('benchmark_entry')
        if start and benchmark_price:
            bench = (benchmark_price - start) / start
            record['benchmark_pct'] = round(bench, 6)
            # The same market-neutral baseline as paper_trader.close_trade.
            baseline = -bench if record['direction'] == strategy.SHORT else bench
            record['alpha_pct'] = round(record['net_pct'] - baseline, 6)
        return record

    # --- reporting -----------------------------------------------------

    def summary(self):
        """How the refused signals did, per refusing rule: count, win rate,
        expectancy after costs, average alpha. A rule whose refusals would
        have made money is keeping the app out of winners."""
        closed = self.closed()
        groups = {}
        for r in closed:
            groups.setdefault(r.get('skip_rule') or '?', []).append(r)
        return {
            "open": len(self.open_records()),
            "closed": len(closed),
            "expectancy": _avg([r['net_pct'] for r in closed]),
            "by_rule": {rule: _stats(rs) for rule, rs in sorted(groups.items())},
        }

    def overview(self, recent=30):
        """Everything the Paper trading views show about skipped trades."""
        return {
            "summary": self.summary(),
            "open": sorted(self.open_records(), key=lambda r: r['opened_at'], reverse=True),
            "closed_recent": sorted(self.closed(), key=lambda r: r['closed_at'] or '',
                                    reverse=True)[:recent],
        }

    def _trim(self):
        closed = self.closed()
        excess = len(closed) - MAX_STORED_CLOSED
        if excess <= 0:
            return
        oldest = sorted(closed, key=lambda r: r['closed_at'] or '')[:excess]
        drop = {r['id'] for r in oldest}
        self.records = [r for r in self.records if r['id'] not in drop]


def _avg(values):
    return (sum(values) / len(values)) if values else None


def _stats(records):
    rets = [r['net_pct'] for r in records]
    alphas = [r['alpha_pct'] for r in records if r.get('alpha_pct') is not None]
    return {
        "trades": len(rets),
        "win_rate": sum(1 for x in rets if x > 0) / len(rets),
        "expectancy": _avg(rets),
        "avg_alpha": _avg(alphas),
    }
