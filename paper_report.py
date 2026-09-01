"""Print the paper-trading track record.

    py paper_report.py            summary
    py paper_report.py --trades   every closed trade, oldest first
    py paper_report.py --stops    what a stop loss would have done

Reads data/paper_trades.json, written by the app as it runs (paper_trader.py).
Nothing here touches the network or the app's state - it is safe to run while
the watcher is running.
"""

import sys

from config import (PAPER_COST_PCT, PAPER_BENCHMARK,
                    PAPER_START_CAPITAL, PAPER_POSITION_PCT)
from paper_trader import PaperTrader

# Below this many closed trades, every figure in the report is noise: a run
# of five lucky alerts looks identical to an edge. Stated up front rather
# than buried, because the temptation to read a 6-trade report is the main
# way a forward test goes wrong.
MIN_MEANINGFUL_TRADES = 30


def pct(value, places=2):
    return "n/a" if value is None else f"{value * 100:+.{places}f}%"


def main():
    args = set(sys.argv[1:])
    paper = PaperTrader(cost_pct=PAPER_COST_PCT, benchmark=PAPER_BENCHMARK)

    stats = paper.stats(start_capital=PAPER_START_CAPITAL,
                        position_pct=PAPER_POSITION_PCT)
    if not stats:
        n_open = len(paper.open_trades())
        print("No closed paper trades yet.")
        if n_open:
            print(f"{n_open} position(s) still open - a trade is only recorded "
                  f"once its sell/cover signal fires.")
        else:
            print("Nothing recorded at all yet. The ledger fills up as alerts "
                  "fire; with the HIGH/CRITICAL filter that can be a slow drip.")
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
    if stats['avg_holding_hours'] is not None:
        print(f"  Avg hold         {stats['avg_holding_hours']:.1f}h")

    # The verdict line. Everything above is detail; this is the number that
    # says whether acting on the alerts made or lost money per trade.
    print(f"\n  EXPECTANCY       {pct(stats['expectancy'])} per trade, "
          f"after {PAPER_COST_PCT * 100:.2f}% costs")
    verdict = "PROFITABLE" if stats['expectancy'] > 0 else "LOSING"
    print(f"  → {verdict} on this sample")

    print(f"\n  vs {PAPER_BENCHMARK}:")
    print(f"    Market move over the same windows   {pct(stats['avg_benchmark'])}")
    print(f"    Alpha (market-neutral baseline)     {pct(stats['avg_alpha'])}")
    if stats['avg_alpha'] is not None and stats['avg_alpha'] <= 0 < stats['expectancy']:
        print("    ⚠  Profitable, but not beating the market - the gains look "
              "like\n       drift rather than the analyser picking winners.")

    print(f"\n  Equity curve ({PAPER_POSITION_PCT * 100:.0f}% of capital per trade):")
    print(f"    {PAPER_START_CAPITAL:,.0f} → {stats['final_equity']:,.0f}   "
          f"({pct(stats['total_return'])})")
    print(f"    Max drawdown  -{stats['max_drawdown'] * 100:.2f}%")

    for label, key in (("By direction", "by_direction"),
                       ("By impact", "by_impact"),
                       ("By horizon", "by_horizon"),
                       ("By exit reason", "by_reason")):
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

    if '--stops' not in args and '--stop' not in args:
        print("\n  (--trades for the full list, --stops for the stop-loss study)")
    print()


if __name__ == "__main__":
    main()
