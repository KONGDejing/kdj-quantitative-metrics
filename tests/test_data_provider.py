from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import Mock, patch

import pandas as pd

from src.data_provider import _fetch_tencent_daily, filter_confirmed_daily


class DataProviderTests(unittest.TestCase):
    def test_partial_current_daily_bar_is_removed_before_close(self) -> None:
        data = pd.DataFrame([
            {"date": "2026-08-25", "close": 10},
            {"date": "2026-08-26", "close": 11},
        ])
        filtered = filter_confirmed_daily(data, datetime(2026, 8, 26, 11, 30))
        self.assertEqual(filtered["date"].tolist(), ["2026-08-25"])

    def test_current_daily_bar_is_kept_after_close(self) -> None:
        data = pd.DataFrame([{"date": "2026-08-26", "close": 11}])
        filtered = filter_confirmed_daily(data, datetime(2026, 8, 26, 15, 2))
        self.assertEqual(filtered["date"].tolist(), ["2026-08-26"])

    @patch("src.data_provider.requests.get")
    def test_tencent_daily_fallback_parses_current_bar(self, mocked_get: Mock) -> None:
        response = Mock()
        response.json.return_value = {
            "data": {
                "sz002179": {
                    "day": [["2026-08-26", "33.48", "33.37", "33.68", "33.25", "149315"]]
                }
            }
        }
        mocked_get.return_value = response

        data = _fetch_tencent_daily("002179")

        self.assertEqual(data.iloc[-1]["date"], "2026-08-26")
        self.assertEqual(float(data.iloc[-1]["close"]), 33.37)
        self.assertEqual(data.attrs["data_source"], "tencent_daily")
