from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from src import runner


class SessionCloseTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 29, 12)
        self.state = SimpleNamespace(
            config={"timeframes": ["1d", "10m"], "kdj": {"n": 9, "m1": 3, "m2": 3}},
            symbols=[{"code": "002179", "name": "中航光电"}],
            latest={"002179": {"10m": {"close": 33.03, "complete": False}}},
            series={"002179": {"1d": [
                {"timestamp": "2026-09-28", "open": 34.24, "high": 34.25, "low": 32.87, "close": 33.27}
            ]}},
        )
        self.state.update_latest = lambda c, tf, v: self.state.latest.setdefault(c, {}).__setitem__(tf, v)
        self.state.update_series = lambda c, tf, v: self.state.series.setdefault(c, {}).__setitem__(tf, v)
        self.data = pd.DataFrame([
            {"datetime": "2026-09-29 11:20:00", "open": 33, "high": 33.1, "low": 32.78, "close": 32.9},
            {"datetime": "2026-09-29 11:30:00", "open": 32.9, "high": 33.04, "low": 32.8, "close": 32.96},
        ])
        self.quote = {"002179": {"price": 32.96, "previous_close": 33.27, "timestamp": "20260929113000"}}

    def refresh(self):
        with patch.object(runner, "state", self.state), patch.object(runner, "fetch_realtime_quotes", return_value=self.quote), patch.object(runner, "safe_fetch_kline", return_value=self.data), patch.object(runner, "_best_thresholds", return_value={}), patch.object(runner, "notify") as alerts:
            result = runner._refresh_session_close(self.now)
            alerts.assert_not_called()
            return result

    def test_lunch_refresh_finalizes_candle_and_estimated_daily_without_alert(self):
        self.assertTrue(self.refresh())
        view = self.state.latest["002179"]["10m"]
        self.assertEqual(view["close"], 32.96)
        self.assertTrue(view["complete"])
        self.assertEqual(view["finalized_session"], "2026-09-29 11:30:00")
        self.assertEqual(self.state.latest["002179"]["1d_est"]["close"], 32.96)
        self.assertTrue(self.state.series["002179"]["10m"][-1]["complete"])
        with patch.object(runner, "state", self.state), patch.object(runner, "fetch_realtime_quotes") as fetch:
            self.assertTrue(runner._refresh_session_close(self.now))
            fetch.assert_not_called()

    def test_lagging_minute_close_is_retried_not_declared_final(self):
        self.data.loc[1, "close"] = 33.03
        self.assertFalse(self.refresh())
        self.assertNotIn("finalized_session", self.state.latest["002179"]["10m"])
        self.data.loc[1, "close"] = 32.96
        self.assertTrue(self.refresh())

    def test_missing_or_preclose_quote_cannot_confirm_candle(self):
        self.quote = {}
        self.assertFalse(self.refresh())
        self.quote = {"002179": {"price": 32.96, "timestamp": "20260929112815"}}
        self.assertFalse(self.refresh())

    def test_wrong_day_or_missing_bar_is_retried(self):
        self.data.loc[1, "datetime"] = "2026-09-28 11:30:00"
        self.assertFalse(self.refresh())
        self.data = None
        self.assertFalse(self.refresh())

    def test_invalid_quote_cannot_confirm_final_candle(self):
        for price in [float("nan"), float("inf"), -1, "bad"]:
            self.quote["002179"]["price"] = price
            self.assertFalse(self.refresh())

    def test_afternoon_close_has_its_own_finalization(self):
        self.now = datetime(2026, 9, 29, 15, 2)
        self.data.loc[1, "datetime"] = "2026-09-29 15:00:00"
        self.quote["002179"]["timestamp"] = "20260929150000"
        self.assertTrue(self.refresh())
        self.assertEqual(self.state.latest["002179"]["10m"]["finalized_session"], "2026-09-29 15:00:00")

    def test_no_finalization_during_live_bar_grace_weekend_or_afternoon_trading(self):
        for now in [datetime(2026, 9, 29, 11, 29), datetime(2026, 9, 29, 11, 30, 10), datetime(2026, 9, 29, 13, 5), datetime(2026, 9, 27, 12)]:
            with patch.object(runner, "state", self.state):
                self.assertIsNone(runner._session_close_boundary(now))


if __name__ == "__main__":
    unittest.main()
