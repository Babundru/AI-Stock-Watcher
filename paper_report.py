"""Print the paper-trading track record.

    py paper_report.py            summary
    py paper_report.py --trades   every closed trade, oldest first
    py paper_report.py --stops    what a stop loss would have done

Reads data/paper_trades.json, written by the app as it runs (paper_trader.py).
Nothing here touches the network or the app's state - it is safe to run while
the watcher is running.
"""

import sys

from config import PAPER_COST_PCT, PAPER_BENCHMARK, PAPER_BENCHMARK_EU
from panel import Panel, agent_scores
from paper_trader import PaperTrader
from shadow_trades import RULE_LABELS, ShadowBook

# Below this many closed trades, every figure in the report is noise: a run
# of five lucky alerts looks identical to an edge. Stated up front rather
# than buried, because the temptation to read a 6-trade report is the main
# way a forward test goes wrong.
MIN_MEANINGFUL_TRADES = 30


def pct(value, places=2):
    return "n/a" if value is None else f"{value * 100:+.{places}f}%"


def print_skipped():
    """Signals the entry rules turned down, followed as if traded
    (shadow_trades.py), split by the rule that turned them down."""
    summary = ShadowBook(cost_pct=PAPER_COST_PCT).summary()
    if not summary['open'] and not summary['closed']:
        return
    print("\n  Skipped trades - signals the entry rules turned down, followed as if traded:")
    print(f"    {summary['closed']} closed, {summary['open']} still being followed")
    for rule, row in summary['by_rule'].items():
        print(f"    {RULE_LABELS.get(rule, rule):<32} {row['trades']:>3} trades  "
              f"{row['win_rate'] * 100:>5.1f}% win  {pct(row['expectancy'])} exp")
    if summary['closed']:
        print("    (a rule whose skipped trades show a positive expectancy is keeping\n"
              "     the app out of winners - same small-sample caveat as above)")


def print_panel(paper):
    """How the agent panel's votes (panel.py, shadow mode) did against the
    results of the trades and skipped trades they were about."""
    records = Panel([]).load()
    if not records:
        return
    outcomes = {t.get('url'): t['net_pct'] for t in paper.closed() if t.get('url')}
    outcomes.update({r.get('url'): r['net_pct'] for r in ShadowBook(cost_pct=PAPER_COST_PCT).closed()
                     if r.get('url') and r.get('url') not in outcomes})
    scores = agent_scores(records, outcomes)
    print(f"\n  Agent panel (shadow) - {len(records)} alerts voted on, "
          f"{sum(1 for r in records if r.get('url') in outcomes)} with a result:")
    if not scores:
        print("    no results yet")
        return
    for agent, row in scores.items():
        print(f"    {agent:<12} TRADE {row['TRADE']['n']:>3} → {pct(row['TRADE']['avg'])}   "
              f"PASS {row['PASS']['n']:>3} → {pct(row['PASS']['avg'])}")
    print("    (an agent worth listening to has TRADE well above PASS - same\n"
          "     small-sample caveat as above)")


def main():
    args = set(sys.argv[1:])
    paper = PaperTrader(cost_pct=PAPER_COST_PCT, benchmark=PAPER_BENCHMARK)

    stats = paper.stats()
    if not stats:
        n_open = len(paper.open_trades())
        print("No closed paper trades yet.")
        if n_open:
            print(f"{n_open} position(s) still open - a trade is only recorded "
                  f"once its sell/cover signal fires.")
        else:
            print("Nothing recorded at all yet. The ledger fills up as alerts "
                  "fire; with the HIGH/CRITICAL filter that can be a slow drip.")
        print_skipped()
        return

    n = stats['trades']
    print()
    print("=" * 62)
    print(f"  PAPER TRADING RECORD - {n} closed, {stats['open']} open")
    print("=" * 62)

    if n < MIN_MEANINGFUL_TRADES:
        print(f"\n  ⚠  Only {n} trades. Below ~{MIN_MEANINGFUL_TRADES} these "
              f"numbers are noise -\n     keep running before drawing any "
              f"conclusion from them.")

    print(f"\n  Win rate         {stats['win_rate'] * 100:.1f}%  "
          f"({stats['wins']}W / {stats['losses']}L)")
    print(f"  Average win      {pct(stats['avg_win'])}")
    print(f"  Average loss     {pct(stats['avg_loss'])}")
    if stats['payoff_ratio'] is not None:
        print(f"  Payoff ratio     {stats['payoff_ratio']:.2f}x  "
              f"(avg win vs avg loss)")
    print(f"  Best / worst     {pct(stats['best'])} / {pct(stats['worst'])}")
    if stats['avg_spread'] is not None:
        print(f"  Spread paid      {pct(stats['avg_spread'])} per round trip, already in "
              f"every return ({stats['spread_trades']} of {n} trades filled at bid/ask)")
    if stats['avg_holding_hours'] is not None:
        print(f"  Avg hold         {stats['avg_holding_hours']:.1f}h")
    if stats['latency_opened_min'] is not None:
        # The speed a news strategy lives on: how old the story was by the
        # time the app had a verdict, and by the time it was in the trade.
        analysed = stats['latency_analysed_min']
        print(f"  Detection        median {analysed:.0f} min published → analysed, "
              f"{stats['latency_opened_min']:.0f} min → opened"
              if analysed is not None else
              f"  Detection        median {stats['latency_opened_min']:.0f} min published → opened")

    # The verdict line. Everything above is detail; this is the number that
    # says whether acting on the alerts made or lost money per trade.
    print(f"\n  EXPECTANCY       {pct(stats['expectancy'])} per trade, "
          f"after {PAPER_COST_PCT * 100:.2f}% costs")
    verdict = "PROFITABLE" if stats['expectancy'] > 0 else "LOSING"
    print(f"  → {verdict} on this sample")

    # Each trade was measured against its own market's index (markets.
    # benchmark_for: SPY for a US listing, the European one for a European
    # listing), so this average blends them when the record spans both.
    print(f"\n  vs {PAPER_BENCHMARK} / {PAPER_BENCHMARK_EU}:")
    print(f"    Market move over the same windows   {pct(stats['avg_benchmark'])}")
    print(f"    Alpha (market-neutral baseline)     {pct(stats['avg_alpha'])}")
    if stats['avg_alpha'] is not None and stats['avg_alpha'] <= 0 < stats['expectancy']:
        print("    ⚠  Profitable, but not beating the market - the gains look "
              "like\n       drift rather than the analyser picking winners.")

    # Open positions valued at their entry here (no price call): this
    # report never touches the network.
    print("\n  Paper accounts (sized by risk, open positions at entry):")
    for acct in paper.accounts(curve=False):
        flag = "  <- trades" if acct['primary'] else ""
        print(f"    {acct['risk_pct'] * 100:.1f}% risk   ${acct['budget']:,.0f} → "
              f"${acct['equity']:,.2f}  ({pct(acct['total_return'])}), "
              f"max drawdown -{acct['max_drawdown'] * 100:.2f}%, "
              f"{acct['taken']} taken, {acct['skipped_cash']} skipped for cash{flag}")

    for label, key in (("By strategy version", "by_strategy"),
                       ("By mode", "by_mode"),
                       ("By direction", "by_direction"),
                       ("By impact", "by_impact"),
                       ("By horizon", "by_horizon"),
                       ("By exit reason", "by_reason"),
                       ("By AI confidence", "by_confidence"),
                       ("By model", "by_model"),
                       ("By prompt version", "by_prompt"),
                       ("By event type", "by_event_type"),
                       ("By news source", "by_source"),
                       ("By screener verdict", "by_screener")):
        print(f"\n  {label}:")
        for name, row in stats[key].items():
            print(f"    {name:<18} {row['trades']:>3} trades  "
                  f"{row['win_rate'] * 100:>5.1f}% win  "
                  f"{pct(row['expectancy'])} exp")

    if '--stops' in args or '--stop' in args:
        print("\n  Stop-loss study (from recorded worst excursions):")
        print(f"    {'stop':<8} {'expectancy':>12} {'win rate':>10} {'stopped out':>13}")
        for row in paper.stop_loss_study():
            name = "none" if row['stop'] is None else f"{row['stop'] * 100:.0f}%"
            print(f"    {name:<8} {pct(row['expectancy']):>12} "
                  f"{row['win_rate'] * 100:>9.1f}% {row['stopped_out']:>13}")
        print("    (pessimistic: assumes a touched stop always fired first)")

    if '--trades' in args:
        print("\n  Trades, oldest first:")
        for t in sorted(paper.closed(), key=lambda x: x['closed_at'] or ''):
            when = (t['closed_at'] or '')[:16].replace('T', ' ')
            print(f"    {when}  {t['direction']:<5} {t['ticker']:<6} "
                  f"{t['entry_price']:>9.2f} → {t['exit_price']:>9.2f}  "
                  f"{pct(t['net_pct']):>9}  {t['reason']}")

    print_skipped()
    print_panel(paper)

    if '--stops' not in args and '--stop' not in args:
        print("\n  (--trades for the full list, --stops for the stop-loss study)")
    print()


if __name__ == "__main__":
    main()
