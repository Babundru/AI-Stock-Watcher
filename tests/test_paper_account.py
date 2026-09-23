"""Tests for the paper account: position sizing, the cash limit, and the
replay that turns the ledger into money at each risk level.

Run from the project root:  py -m unittest discover -s tests
"""

import datetime
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import paper_account  # noqa: E402
from paper_trader import PaperTrader  # noqa: E402

T0 = datetime.datetime(2026, 9, 1, 15, 0, tzinfo=datetime.timezone.utc)


def at(hours):
    return (T0 + datetime.timedelta(hours=hours)).isoformat()


def trade(wid, opened, closed=None, net=None, stop=0.04, confidence=80, impact="HIGH",
          ticker="AAA", direction="LONG", entry=100.0):
    t = {"watch_id": wid, "ticker": ticker, "direction": direction, "entry_price": entry,
         "opened_at": at(opened), "stop_pct": stop, "confidence": confidence, "impact": impact,
         "status": "OPEN", "closed_at": None, "net_pct": None}
    if closed is not None:
        t.update(status="CLOSED", closed_at=at(closed), net_pct=net)
    return t


SETTINGS = dict(PAPER_BUDGET=1000.0, PAPER_RISK_PCT=0.01,
                PAPER_MAX_POSITION_PCT=0.25, PAPER_MIN_POSITION=20.0)


class SizingTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.multiple(config, **SETTINGS)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.s = paper_account.settings()

    def test_conviction(self):
        self.assertAlmostEqual(paper_account.conviction(60, "HIGH"), 0.75)
        self.assertAlmostEqual(paper_account.conviction(100, "HIGH"), 1.25)
        self.assertAlmostEqual(paper_account.conviction(80, "CRITICAL"), 1.0 * 1.15)
        self.assertAlmostEqual(paper_account.conviction(None, None), 1.0)

    def test_volatile_stock_gets_a_smaller_position(self):
        calm = paper_account.position_size(1000, 1000, 0.04, 80, "HIGH", self.s)
        jumpy = paper_account.position_size(1000, 1000, 0.08, 80, "HIGH", self.s)
        self.assertEqual(calm['size'], 250.0)     # 1000 x 1% / 4%
        self.assertEqual(jumpy['size'], 125.0)
        # Either way a stopped-out trade costs about 1% of the account.
        self.assertAlmostEqual(calm['risk_usd'], 10.0)
        self.assertAlmostEqual(jumpy['risk_usd'], 10.0)

    def test_capped_at_max_position_and_cash(self):
        self.assertEqual(paper_account.position_size(1000, 1000, 0.02, 80, "HIGH", self.s)['size'], 250.0)
        self.assertEqual(paper_account.position_size(1000, 90, 0.04, 80, "HIGH", self.s)['size'], 90.0)

    def test_too_little_cash_is_refused(self):
        got = paper_account.position_size(1000, 15, 0.04, 80, "HIGH", self.s)
        self.assertIsNone(got['size'])
        self.assertIn("not enough cash", got['reason'])


class ReplayTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.multiple(config, **SETTINGS)
        patcher.start()
        self.addCleanup(patcher.stop)

    def replay(self, trades, risk=0.01, **kw):
        return paper_account.replay(trades, 0.002, risk_pct=risk,
                                    now=T0 + datetime.timedelta(days=5), **kw)

    def test_a_closed_trade_in_money(self):
        acct = self.replay([trade("a", 0, 2, 0.05)])
        self.assertEqual(acct['sizes']['a'], 250.0)
        self.assertAlmostEqual(acct['realised_pnl'], 12.5)
        self.assertAlmostEqual(acct['equity'], 1012.5)
        self.assertAlmostEqual(acct['cash'], 1012.5)
        self.assertEqual(acct['curve'][0]['equity'], 1000.0)
        self.assertEqual(acct['curve'][-1]['kind'], 'now')

    def test_cash_runs_out_sooner_at_higher_risk(self):
        # Five overlapping trades with 8% stops: $125 each at 1% risk, so all
        # five fit in $1000; $250 each at 2%, so four use up the cash and the
        # fifth is refused.
        trades = [trade(str(i), i * 0.1, 10, 0.01, stop=0.08) for i in range(5)]
        one = self.replay(trades, 0.01)
        two = self.replay(trades, 0.02)
        self.assertEqual(one['taken'], 5)            # 125 each
        self.assertEqual(two['taken'], 4)            # 250 each - the fifth has no cash
        self.assertEqual(two['skipped_cash'], 1)

    def test_a_close_frees_cash_for_an_open_at_the_same_moment(self):
        trades = [trade(str(i), 0, 5, 0.0, stop=0.02) for i in range(4)]   # 250 each, all cash
        trades.append(trade("late", 5, None))
        acct = self.replay(trades)
        self.assertIn("late", acct['sizes'])

    def test_open_positions_are_marked(self):
        t = trade("a", 0)
        acct = self.replay([t], marks=[(datetime.datetime.fromisoformat(at(1)).timestamp(),
                                        {"a": 110.0})])
        # 250 in, +10% less 0.2% cost.
        self.assertAlmostEqual(acct['equity'], 1000 + 250 * (0.10 - 0.002))
        self.assertAlmostEqual(acct['unrealised_pnl'], 250 * (0.10 - 0.002))
        acct = self.replay([t], prices={"AAA": 90.0})
        self.assertAlmostEqual(acct['unrealised_pnl'], 250 * (-0.10 - 0.002))

    def test_losses_shrink_later_positions(self):
        acct = self.replay([trade("a", 0, 1, -0.5), trade("b", 2, None)])
        # 1000 - 125 lost = 875 -> 875 x 1% / 4% = 218.75
        self.assertAlmostEqual(acct['sizes']['b'], 218.75)


class LedgerTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.multiple(config, **SETTINGS)
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.paper = PaperTrader(filename=os.path.join(tmp.name, 'paper_trades.json'), cost_pct=0.002)

    def test_only_the_trading_account(self):
        self.paper.trades = [trade("a", 0, 2, 0.05)]
        accts = self.paper.accounts()
        self.assertEqual([a['risk_pct'] for a in accts], [0.01])
        self.assertEqual([a['primary'] for a in accts], [True])

    def test_open_entries_flagged_on_the_curve(self):
        self.paper.trades = [trade("a", 0, 2, 0.05, ticker="A"), trade("b", 1, ticker="B")]
        opens = [p for p in self.paper.accounts()[0]['curve'] if p['kind'] == 'open']
        self.assertEqual([p['ticker'] for p in opens if p.get('still_open')], ["B"])
        self.assertIn('now_pct', [p for p in opens if p.get('still_open')][0])

    def test_new_trade_sized_from_the_account_as_it_stands(self):
        self.paper.trades = [trade(str(i), 0, None, stop=0.02) for i in range(3)]   # 750 in
        got = self.paper.size_new_trade(0.02, 80, "HIGH")
        self.assertEqual(got['size'], 250.0)
        self.paper.trades.append(trade("x", 0, None, stop=0.02))                    # all 1000 in
        self.assertIsNone(self.paper.size_new_trade(0.02, 80, "HIGH")['size'])

    def test_marks_are_thinned_and_saved(self):
        self.paper.trades = [trade("a", 0)]
        self.paper._by_id = {"a": self.paper.trades[0]}
        start = T0
        for i in range(60 * 24 * 3 // 5):      # three days of 5-minute checks
            self.paper.record_marks({"a": 100.0 + i % 7},
                                    start + datetime.timedelta(minutes=5 * i))
        # A day at full resolution (289 incl. both ends), two days hourly.
        self.assertLess(len(self.paper.marks), 289 + 2 * 24 + 2)
        self.assertGreater(len(self.paper.marks), 289)
        self.paper.save_marks()
        self.assertEqual(len(self.paper._load_marks()), len(self.paper.marks))

    def test_marks_skip_closed_and_unknown_positions(self):
        self.paper.trades = [trade("a", 0, 1, 0.01)]
        self.paper._by_id = {"a": self.paper.trades[0]}
        self.assertFalse(self.paper.record_marks({"a": 101.0, "zz": 5.0}, T0))

    def test_overview_puts_money_on_open_positions(self):
        self.paper.trades = [trade("a", 0)]
        self.paper._by_id = {"a": self.paper.trades[0]}
        self.paper.trades[0].update(mae_pct=0.0, mfe_pct=0.0, target_pct=0.05)
        data = self.paper.overview({"AAA": 104.0})
        pos = data['positions'][0]
        self.assertEqual(pos['position_usd'], 250.0)
        self.assertAlmostEqual(pos['unrealised_usd'], round(250 * (0.04 - 0.002), 2))
        self.assertEqual(len(data['accounts']), 1)
        self.assertNotIn('sizes', data['accounts'][0])




class DecideTradeTest(unittest.TestCase):
    """main._decide_trade sizes a new trade from the trading account, and
    turns one away - following it as a skipped trade - when the cash is gone."""

    def setUp(self):
        patcher = mock.patch.multiple(config, **SETTINGS)
        patcher.start()
        self.addCleanup(patcher.stop)
        import main
        from watch_manager import WatchManager
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        b = main.StockAppBackend.__new__(main.StockAppBackend)
        b.status_callback = b.log_callback = None
        b.paper = PaperTrader(filename=os.path.join(tmp.name, 'paper_trades.json'), cost_pct=0.002)
        b.watch_mgr = WatchManager(filename=os.path.join(tmp.name, 'watches.json'))
        b.shadows = mock.Mock()
        self.b = b
        # A calm, unmoved stock: ATR 2% -> a 3% stop.
        ctx = {'price': 100.0, 'atr_pct': 0.02, 'change_since_close_pct': 0.0,
               'market_price': 500.0, 'market_since_close_pct': 0.0}
        for name, value in (('fetch_context', ctx), ('fetch_prices', {})):
            p = mock.patch.object(main.price_lookup, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def decide(self, ticker="AAA"):
        return self.b._decide_trade(ticker, "Acme", "LONG", "HIGH", "RALLY", 0.08, 80,
                                    {'horizon': 'DAYS'}, {'source': 'Custom/X'}, "u", "t")

    def test_a_trade_carries_its_size(self):
        d = self.decide()
        self.assertTrue(d['opened'])
        self.assertEqual(d['position_usd'], 250.0)      # 1% of $1000 / 3% stop -> capped at 25%
        self.assertEqual(d['watch']['position_usd'], 250.0)
        self.assertEqual(self.b.paper.trades[0]['position_usd'], 250.0)

    def test_no_cash_no_trade(self):
        for t in ("A", "B", "C", "D"):
            self.assertTrue(self.decide(t)['opened'])
        d = self.decide("E")
        self.assertFalse(d['opened'])
        self.assertIn("not enough cash", d['reason'])
        plan = self.b.shadows.track.call_args[0][4]
        self.assertEqual(plan['rule'], 'cash')


if __name__ == "__main__":
    unittest.main()
