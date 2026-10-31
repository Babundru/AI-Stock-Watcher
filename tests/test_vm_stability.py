"""Tests for the fixes behind the October 2026 VM stalls: no threaded yfinance
downloads, and the scan-loop stall check the systemd watchdog relies on.
No network.

Run from the project root:  py -m unittest discover -s tests
"""

import os
import sys
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(os.path.dirname(__file__))))

import main  # noqa: E402
import price_lookup  # noqa: E402


class NoThreadedDownloadsTest(unittest.TestCase):
    """Threaded yf.download leaks a thread stack per ticker (handoff/incidents.md)."""

    def _download_kwargs(self, call):
        download = mock.Mock(return_value=None)
        with mock.patch.dict(sys.modules, {"yfinance": types.SimpleNamespace(download=download)}):
            call(["AAPL", "MSFT"])
        download.assert_called_once()
        return download.call_args.kwargs

    def test_price_lookup_is_not_threaded(self):
        self.assertIs(self._download_kwargs(price_lookup._intraday_prices)["threads"], False)

    def test_bar_lookup_is_not_threaded(self):
        self.assertIs(self._download_kwargs(price_lookup._intraday_bars)["threads"], False)


class LoopStalledTest(unittest.TestCase):
    def setUp(self):
        self.backend = main.StockAppBackend.__new__(main.StockAppBackend)
        self.backend.running = True
        self.backend._generation = 1

    def test_a_recent_beat_is_not_a_stall(self):
        self.backend._last_beat = time.time() - 60
        self.assertFalse(self.backend.loop_stalled(1800))

    def test_no_beat_past_the_limit_is_a_stall(self):
        self.backend._last_beat = time.time() - 1801
        self.assertTrue(self.backend.loop_stalled(1800))

    def test_stopped_monitoring_is_not_a_stall(self):
        self.backend.running = False
        self.backend._last_beat = 0.0
        self.assertFalse(self.backend.loop_stalled(1800))

    def test_sleeping_counts_as_progress(self):
        self.backend._last_beat = 0.0
        with mock.patch.object(main.time, "sleep"):
            self.backend._sleep(1, 1)
        self.assertFalse(self.backend.loop_stalled(1800))


if __name__ == "__main__":
    unittest.main()
