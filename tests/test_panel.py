"""Tests for the agent panel (panel.py, CHANGE.md C12). No network: the
models are stand-ins that answer from a script.

Run from the project root:  py -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import panel  # noqa: E402


class FakeModel:
    """A provider whose complete() answers by which agent's prompt it got."""

    def __init__(self, answers, calls):
        self.answers = answers
        self.calls = calls

    def complete(self, prompt, system=None):
        for marker, reply in self.answers.items():
            if marker in prompt:
                self.calls.append(marker)
                return None if reply is None else json.dumps(reply)
        return None


# The devil's advocate quotes the others' views, so it is matched first.
ANSWERS = {
    "devil's advocate": {"vote": "PASS", "conviction": 2, "case_against": "already up",
                         "reason": "not enough"},
    "catalyst analyst": {"vote": "TRADE", "conviction": 4, "reason": "big contract"},
    "priced-in": {"vote": "PASS", "conviction": 3, "reason": "moved 3 ranges", "veto": False},
    "specialist in": {"vote": "TRADE", "conviction": 3, "reason": "fine"},
    "historian": {"vote": "TRADE", "conviction": 2, "reason": "contracts did well"},
}


class PanelTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = os.path.join(tmp.name, 'panel_votes.json')
        self.calls = []

    def panel(self, answers=ANSWERS, models=("m1", "m2")):
        providers = [(m, FakeModel(answers, self.calls)) for m in models]
        return panel.Panel(providers, log=lambda m: None, filename=self.file)

    def run_panel(self, p):
        return p.run({'title': 'Acme wins $500M contract', 'url': 'https://x/1', 'content': 'text'},
                     {'event_type': 'CONTRACT', 'explanation': 'contract', 'target_company': 'Acme'},
                     {'direction': 'LONG', 'ticker': 'ACME', 'opened': True, 'reason': None,
                      'watch': {'id': 'w1'}, 'market_cap_usd': 2e9},
                     {'change_since_close_pct': 0.03},
                     {'already_moved_pct': 0.03, 'atr_pct': 0.01, 'expected_move_pct': 0.06},
                     [{'status': 'CLOSED', 'event_type': 'CONTRACT', 'direction': 'LONG',
                       'net_pct': 0.05, 'ticker': 'X', 'headline': 'X wins deal'}])

    def test_every_agent_votes_and_the_rule_decides(self):
        record = self.run_panel(self.panel())
        self.assertEqual(set(record['votes']), {'catalyst', 'skeptic', 'specialist', 'historian',
                                                'devil'})
        self.assertEqual(record['panel'], {'trade': True, 'agree': 3, 'voted': 4, 'vetoed': False})
        self.assertEqual(record['votes']['devil']['case_against'], 'already up')
        self.assertEqual(record['watch_id'], 'w1')
        # Different agents on different models where the list has several.
        self.assertEqual(record['votes']['catalyst']['model'], 'm1')
        self.assertEqual(record['votes']['skeptic']['model'], 'm2')
        self.assertEqual(len(panel.Panel([], filename=self.file).load()), 1)

    def test_a_skeptic_veto_stops_the_panel(self):
        answers = dict(ANSWERS, **{"priced-in": {"vote": "TRADE", "conviction": 5, "reason": "x",
                                                 "veto": True}})
        record = self.run_panel(self.panel(answers))
        self.assertEqual(record['panel']['agree'], 4)
        self.assertTrue(record['panel']['vetoed'])
        self.assertFalse(record['panel']['trade'])

    def test_a_failed_agent_counts_as_no_vote(self):
        answers = dict(ANSWERS, **{"historian": None})
        record = self.run_panel(self.panel(answers))
        self.assertIsNone(record['votes']['historian'])
        self.assertEqual(record['panel']['voted'], 3)
        self.assertEqual(record['panel']['agree'], 2)
        self.assertFalse(record['panel']['trade'])

    def test_no_engine_no_panel(self):
        self.assertIsNone(self.run_panel(panel.Panel([], filename=self.file)))

    def test_the_file_is_bounded(self):
        p = self.panel()
        saved = panel.MAX_RECORDS
        panel.MAX_RECORDS = 2
        try:
            for _ in range(3):
                self.run_panel(p)
        finally:
            panel.MAX_RECORDS = saved
        self.assertEqual(len(p.load()), 2)

    def test_the_skeptic_sees_the_price_context(self):
        prompts = []

        class Recorder:
            def complete(self, prompt, system=None):
                prompts.append(prompt)
                return None

        self.run_panel(panel.Panel([("m", Recorder())], filename=self.file))
        skeptic = next(p for p in prompts if "priced-in" in p)
        self.assertIn("3.0 daily ranges", skeptic)
        self.assertIn("+3.0%", skeptic)


class ScoringTests(unittest.TestCase):

    def test_base_rates(self):
        trades = [{'status': 'CLOSED', 'event_type': 'M&A', 'direction': 'LONG', 'net_pct': 0.10},
                  {'status': 'CLOSED', 'event_type': 'M&A', 'direction': 'LONG', 'net_pct': -0.02},
                  {'status': 'CLOSED', 'event_type': 'EARNINGS', 'direction': 'SHORT', 'net_pct': -0.05},
                  {'status': 'OPEN', 'event_type': 'M&A', 'direction': 'LONG', 'net_pct': None}]
        rates, examples = panel.base_rates(trades, 'M&A', 'LONG')
        self.assertIn("M&A LONGs: 2 trades, +4.0% average, 50% won", rates)
        self.assertIn("All: 3 trades", rates)
        self.assertEqual(examples.count("\n"), 1)

    def test_agent_scores(self):
        records = [
            {'url': 'a', 'votes': {'catalyst': {'vote': 'TRADE'}, 'skeptic': {'vote': 'PASS'}},
             'panel': {'trade': False}},
            {'url': 'b', 'votes': {'catalyst': {'vote': 'PASS'}, 'skeptic': {'vote': 'PASS'}},
             'panel': {'trade': False}},
            {'url': 'c', 'votes': {'catalyst': {'vote': 'TRADE'}}, 'panel': {'trade': True}},
        ]
        scores = panel.agent_scores(records, {'a': 0.10, 'b': -0.04})
        self.assertEqual(scores['catalyst']['TRADE'], {'n': 1, 'avg': 0.10})
        self.assertEqual(scores['catalyst']['PASS'], {'n': 1, 'avg': -0.04})
        self.assertAlmostEqual(scores['skeptic']['PASS']['avg'], 0.03)
        self.assertEqual(scores['panel rule']['PASS']['n'], 2)


class WeeklyReviewTests(unittest.TestCase):

    def setUp(self):
        import weekly_review
        self.wr = weekly_review
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.saved = weekly_review.REVIEW_DIR
        weekly_review.REVIEW_DIR = os.path.join(tmp.name, 'reviews')
        self.addCleanup(setattr, weekly_review, 'REVIEW_DIR', self.saved)
        from paper_trader import PaperTrader
        from shadow_trades import ShadowBook
        self.paper = PaperTrader(filename=os.path.join(tmp.name, 'p.json'), cost_pct=0.0)
        self.shadows = ShadowBook(filename=os.path.join(tmp.name, 's.json'), cost_pct=0.0)

    def test_prompt_and_review_file(self):
        prompt = self.wr.build_prompt(self.paper, self.shadows, {})
        self.assertIn("EXHAUSTED_MAX_ATR_MULT =", prompt)
        self.assertIn("none closed yet", prompt)
        self.assertTrue(self.wr.due())

        class Model:
            def complete(self, prompt, system=None):
                return "## What the data says\nToo few trades."

        path = self.wr.write_review([("m", Model())], self.paper, self.shadows, {},
                                    log=lambda m: None)
        self.assertIn("Too few trades.", open(path, encoding='utf-8').read())
        self.assertFalse(self.wr.due())


if __name__ == '__main__':
    unittest.main()
