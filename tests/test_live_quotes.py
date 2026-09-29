from __future__ import annotations

import unittest
from datetime import datetime
from threading import Lock
from unittest.mock import patch

from src.live_quotes import market_session, poll_quotes_once, quote_view
from src.state import AppState
from src.market_risk import quote_fresh


def quote(stamp="20260929113000", price=32.96):
    return {"price": price, "previous_close": 33.27, "change_ratio": -0.0093, "timestamp": stamp}


class QuoteDisplayTests(unittest.TestCase):
    def view(self, stamp, hour, minute, **kwargs):
        return quote_view(quote(stamp, **kwargs), datetime(2026, 9, 29, hour, minute), {})

    def test_live_quote_is_fresh_and_old_quote_is_stale(self):
        self.assertFalse(self.view("20260929102530", 10, 26)["stale"])
        self.assertTrue(self.view("20260929102530", 10, 28)["stale"])

    def test_lunch_requires_final_snapshot_not_last_preclose_tick(self):
        self.assertEqual(self.view("20260929113000", 12, 0)["status"], "session_close")
        self.assertEqual(self.view("20260929112815", 12, 0)["status"], "stale")
        self.assertTrue(self.view("20260929113000", 13, 0)["stale"])

    def test_close_snapshot_is_valid_that_evening_but_not_tomorrow(self):
        self.assertFalse(self.view("20260929150000", 18, 0)["stale"])
        self.assertTrue(self.view("20260928150000", 10, 0)["stale"])
        self.assertTrue(self.view("20260929145900", 18, 0)["stale"])

    def test_missing_invalid_and_future_are_not_displayed_as_live_prices(self):
        now = datetime(2026, 9, 29, 12)
        for value in (None, {}, quote("bad"), quote("20260930113000"), quote(price=float("nan")), quote(price=0)):
            result = quote_view(value, now, {})
            self.assertEqual(result["status"], "missing")
            self.assertNotIn("price", result)

    def test_session_label_is_honest(self):
        for hour, minute, phase in ((9, 0, "preopen"), (10, 0, "trading"), (12, 0, "lunch"), (13, 0, "trading"), (16, 0, "closed")):
            self.assertEqual(market_session(datetime(2026, 9, 29, hour, minute), {})["phase"], phase)
        self.assertEqual(market_session(datetime(2026, 9, 27, 10), {})["phase"], "closed")

    def test_risk_quote_accepts_lunch_final_only_until_reopen(self):
        self.assertTrue(quote_fresh(quote(), datetime(2026, 9, 29, 12)))
        self.assertFalse(quote_fresh(quote("20260929112800"), datetime(2026, 9, 29, 12)))
        self.assertFalse(quote_fresh(quote(), datetime(2026, 9, 29, 13)))


class QuoteStorageTests(unittest.TestCase):
    def setUp(self):
        self.state = AppState.__new__(AppState)
        self.state._lock = Lock()
        self.state.symbols = [{"code": "002179"}]
        self.state.config = {}
        self.state.quotes = {}

    def update(self, quotes, error=None):
        with patch("src.state.datetime") as clock:
            clock.now.return_value = datetime(2026, 9, 29, 12)
            self.state.update_quotes(quotes, error=error)

    def test_separate_prices_cannot_overwrite_candles(self):
        self.state.latest = {"002179": {"10m": {"close": 33.03, "k": 34.35}}}
        self.update({"002179": quote()})
        self.assertEqual(self.state.quotes["002179"]["price"], 32.96)
        self.assertEqual(self.state.latest["002179"]["10m"]["close"], 33.03)

    def test_older_missing_invalid_or_removed_symbol_does_not_replace_good_quote(self):
        self.update({"002179": quote()})
        for value in (quote("20260929112800", 33.03), quote("bad"), quote(price=float("nan")), quote("20260930113000")):
            self.update({"002179": value})
            self.assertEqual(self.state.quotes["002179"]["price"], 32.96)
        self.update({}, error="failed")
        self.assertEqual(self.state.quote_monitor["error"], "failed")
        self.assertEqual(self.state.quotes["002179"]["timestamp"], "20260929113000")
        self.state.symbols = []
        self.update({"600498": quote()})
        self.assertNotIn("600498", self.state.quotes)

    def test_independent_poll_survives_source_failure(self):
        with patch("src.state.state", self.state), patch("src.live_quotes.fetch_realtime_quotes", side_effect=RuntimeError("offline")):
            poll_quotes_once()
        self.assertEqual(self.state.quote_monitor["error"], "实时报价拉取失败")

    def test_independent_poll_does_not_run_kline_or_alert_pipeline(self):
        with patch("src.state.state", self.state), patch("src.live_quotes.fetch_realtime_quotes", return_value={"002179": quote()}), patch("src.market_risk.refresh_market_risk") as risk, patch("src.runner.run_once") as candles:
            poll_quotes_once()
        risk.assert_called_once()
        candles.assert_not_called()


if __name__ == "__main__":
    unittest.main()
