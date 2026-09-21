"""Tests for which exchange a ticker belongs to, when that exchange trades,
and the ticker normalisation and time exits that follow from it. No network.

Run from the project root:  py -m unittest discover -s tests
"""

import datetime
import os
import sys
import unittest

import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import markets  # noqa: E402
import strategy  # noqa: E402
from price_lookup import normalize_ticker  # noqa: E402


def utc(text):
    return datetime.datetime.fromisoformat(text).replace(tzinfo=pytz.UTC)


class MarketLookupTests(unittest.TestCase):
    def test_a_bare_symbol_is_us(self):
        self.assertEqual(markets.market_key("TSLA"), markets.US)
        self.assertFalse(markets.is_european("TSLA"))

    def test_class_shares_are_not_exchanges(self):
        self.assertEqual(markets.market_key("BRK.B"), markets.US)
        self.assertEqual(markets.market_key("BRK-B"), markets.US)

    def test_european_suffixes(self):
        self.assertEqual(markets.market_key("BMW.DE"), "XETRA")
        self.assertEqual(markets.market_key("SHEL.L"), "LSE")
        self.assertEqual(markets.market_key("ASML.AS"), "EURONEXT")
        self.assertEqual(markets.market_key("NESN.SW"), "SIX")
        self.assertEqual(markets.market_key("NOVO-B.CO"), "COPENHAGEN")
        for ticker in ("BMW.DE", "SHEL.L", "ASML.AS", "NOVO-B.CO", "EQNR.OL"):
            self.assertTrue(markets.is_european(ticker), ticker)

    def test_an_unmodelled_exchange_is_neither(self):
        self.assertIsNone(markets.market_key("RY.TO"))
        self.assertFalse(markets.is_european("RY.TO"))
        self.assertIsNone(markets.session_open("RY.TO"))

    def test_sessions_are_local_to_the_exchange(self):
        # 14:00 UTC on a Thursday: 10:00 in New York, 15:00 in London,
        # 16:00 in Frankfurt - all three trading.
        midday = utc("2026-09-10T14:00:00")
        self.assertTrue(markets.session_open("TSLA", midday))
        self.assertTrue(markets.session_open("SHEL.L", midday))
        self.assertTrue(markets.session_open("BMW.DE", midday))

        # 08:00 UTC: Europe is mid-session, New York is 04:00 and shut.
        european_morning = utc("2026-09-10T08:00:00")
        self.assertFalse(markets.session_open("TSLA", european_morning))
        self.assertTrue(markets.session_open("SHEL.L", european_morning))
        self.assertTrue(markets.session_open("BMW.DE", european_morning))

        # 19:00 UTC: New York's afternoon, every European venue closed.
        new_york_afternoon = utc("2026-09-10T19:00:00")
        self.assertTrue(markets.session_open("TSLA", new_york_afternoon))
        self.assertFalse(markets.session_open("SHEL.L", new_york_afternoon))
        self.assertFalse(markets.session_open("BMW.DE", new_york_afternoon))

    def test_weekends_are_closed_everywhere(self):
        saturday = utc("2026-09-12T12:00:00")
        self.assertFalse(markets.any_open(saturday))
        self.assertEqual(markets.open_markets(saturday), [])

    def test_open_markets_reports_regions(self):
        # 08:00 UTC on a Thursday: Europe trading, New York not.
        self.assertEqual(markets.open_markets(utc("2026-09-10T08:00:00")), ["Europe"])
        # 14:00 UTC: both.
        self.assertEqual(markets.open_markets(utc("2026-09-10T14:00:00")), ["Europe", "US"])
        # 19:00 UTC: New York's afternoon alone.
        self.assertEqual(markets.open_markets(utc("2026-09-10T19:00:00")), ["US"])

    def test_benchmark_follows_the_listing(self):
        self.assertEqual(markets.benchmark_for("TSLA"), config.PAPER_BENCHMARK)
        self.assertEqual(markets.benchmark_for("BMW.DE"), config.PAPER_BENCHMARK_EU)
        self.assertEqual(markets.benchmark_for("SHEL.L"), config.PAPER_BENCHMARK_EU)
        # An exchange we don't model still gets a benchmark rather than None.
        self.assertEqual(markets.benchmark_for("RY.TO"), config.PAPER_BENCHMARK)


class NormalizeTickerTests(unittest.TestCase):
    def test_us_forms_are_unchanged(self):
        self.assertEqual(normalize_ticker("NASDAQ: TSLA"), "TSLA")
        self.assertEqual(normalize_ticker("$tsla"), "TSLA")
        self.assertEqual(normalize_ticker("BRK.B"), "BRK.B")

    def test_european_suffixes_survive(self):
        self.assertEqual(normalize_ticker("BMW.DE"), "BMW.DE")
        self.assertEqual(normalize_ticker("shel.l"), "SHEL.L")
        self.assertEqual(normalize_ticker("NOVO-B.CO"), "NOVO-B.CO")
        self.assertEqual(normalize_ticker("ERIC-B.ST"), "ERIC-B.ST")

    def test_an_exchange_prefix_supplies_the_suffix(self):
        self.assertEqual(normalize_ticker("ETR: BMW"), "BMW.DE")
        self.assertEqual(normalize_ticker("LON:VOD"), "VOD.L")
        self.assertEqual(normalize_ticker("EPA: AIR"), "AIR.PA")
        self.assertEqual(normalize_ticker("BIT:ISP"), "ISP.MI")
        # Already suffixed: nothing to add.
        self.assertEqual(normalize_ticker("ETR: BMW.DE"), "BMW.DE")

    def test_rubbish_is_still_rejected(self):
        for value in ("N/A", "", None, "Apple Inc", "the German carmaker"):
            self.assertIsNone(normalize_ticker(value), value)


class TimeExitTests(unittest.TestCase):
    def test_us_time_exit_is_unchanged(self):
        # Thursday 10:00 ET, INTRADAY -> 15:45 ET the same day.
        opened = utc("2026-09-10T14:00:00")
        exit_at = strategy.time_exit_at(opened, "INTRADAY", "TSLA")
        et = exit_at.astimezone(pytz.timezone("US/Eastern"))
        self.assertEqual((et.date().isoformat(), et.strftime("%H:%M")),
                         ("2026-09-10", "15:45"))

    def test_no_ticker_still_means_us(self):
        opened = utc("2026-09-10T14:00:00")
        self.assertEqual(strategy.time_exit_at(opened, "INTRADAY"),
                         strategy.time_exit_at(opened, "INTRADAY", "TSLA"))

    def test_frankfurt_exits_on_its_own_clock(self):
        # Thursday 10:00 CEST (08:00 UTC), INTRADAY -> 17:15 CEST that day,
        # a quarter of an hour before XETRA's close.
        opened = utc("2026-09-10T08:00:00")
        exit_at = strategy.time_exit_at(opened, "INTRADAY", "BMW.DE")
        local = exit_at.astimezone(pytz.timezone("Europe/Berlin"))
        self.assertEqual((local.date().isoformat(), local.strftime("%H:%M")),
                         ("2026-09-10", "17:15"))

    def test_london_exits_before_its_own_close(self):
        opened = utc("2026-09-10T08:00:00")
        exit_at = strategy.time_exit_at(opened, "INTRADAY", "SHEL.L")
        local = exit_at.astimezone(pytz.timezone("Europe/London"))
        self.assertEqual(local.strftime("%H:%M"), "16:15")

    def test_a_late_entry_counts_from_the_next_session(self):
        # 17:00 CEST is inside XETRA's session but past its cutoff, so an
        # INTRADAY position opened then is given the next day.
        opened = utc("2026-09-10T15:00:00")
        exit_at = strategy.time_exit_at(opened, "INTRADAY", "BMW.DE")
        local = exit_at.astimezone(pytz.timezone("Europe/Berlin"))
        self.assertEqual(local.date().isoformat(), "2026-09-11")

    def test_horizons_count_trading_days(self):
        # Thursday + 3 trading days = Tuesday (the weekend is skipped).
        opened = utc("2026-09-10T08:00:00")
        exit_at = strategy.time_exit_at(opened, "DAYS", "BMW.DE")
        local = exit_at.astimezone(pytz.timezone("Europe/Berlin"))
        self.assertEqual(local.date().isoformat(), "2026-09-15")


class PublishedInSessionTests(unittest.TestCase):
    def test_session_is_judged_on_the_stocks_own_exchange(self):
        try:
            from price_lookup import _in_regular_session
            import pandas  # noqa: F401
        except ImportError as e:
            self.skipTest(f"pandas not installed here: {e}")
        # Thursday 08:00 UTC: Frankfurt and London trading, New York not.
        self.assertTrue(_in_regular_session("2026-09-10T08:00:00+00:00", "BMW.DE"))
        self.assertTrue(_in_regular_session("2026-09-10T08:00:00+00:00", "SHEL.L"))
        self.assertFalse(_in_regular_session("2026-09-10T08:00:00+00:00", "TSLA"))
        # No ticker at all still means the US session, as it always did.
        self.assertTrue(_in_regular_session("2026-09-10T14:00:00+00:00"))
        # An exchange we don't model: no answer rather than a wrong one.
        self.assertIsNone(_in_regular_session("2026-09-10T14:00:00+00:00", "RY.TO"))


if __name__ == "__main__":
    unittest.main()
